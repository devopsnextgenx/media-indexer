"""LLM-metadata-driven duplicate detection.

Only files that already have a parsed row in `llm_parsed_metadata` take part:
the song title / movie-or-album / artists extracted by `llm_parser` are the
sole signal. Files without that metadata are skipped entirely (no filename
heuristics, no fallback ranking) so they can simply be picked up on a later
run once the LLM backfill job has reached them.

Grouping is scoped to a *folder scope* rather than the whole mount. For the
songs layout (`<language>/<quality>/<artist folder>/<file>`) the quality
segment (xhd / hd / sd ...) is stripped from the relative path, so the same
song stored under `hindi/xhd/Gracy Singh` and `hindi/sd/Gracy Singh` lands in
one group and can be ranked by resolution.
"""
import hashlib
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import jellyfish
from rapidfuzz import fuzz

from media_indexer.config import settings
from media_indexer.database import mysql_db_instance

logger = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")

MIN_TOKEN_LEN = 2
TOKEN_FUZZY_MATCH_THRESHOLD = 82

# Path segments that denote a quality tier rather than a real folder. They are
# removed when computing a group's scope so the same song across xhd/hd/sd
# folders collapses into a single group.
QUALITY_RANK = {
    "uhd": 5, "4k": 5, "2160p": 5,
    "xhd": 4, "fhd": 4, "1080p": 4,
    "hd": 3, "720p": 3,
    "sd": 2, "480p": 2, "360p": 2,
    "lq": 1,
}

# Title dominates; movie/artist agreement only adds confidence on top.
TITLE_WEIGHT = 85.0
MOVIE_BONUS_CAP = 8.0
ARTIST_BONUS_CAP = 7.0

# Gates a pair must clear before it can be considered the same song at all.
TITLE_COVERAGE_MIN = 0.75
TITLE_SIMILARITY_MIN = 0.62

DEFAULT_SCORE_THRESHOLD = 72.0
CONFIDENCE_HIGH_MIN = 88.0


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------
def tokenize(text: Optional[str]) -> List[str]:
    """LLM fields are already noise-free, so this only splits/lowercases."""
    if not text:
        return []
    return [w for w in (t.lower() for t in _SPLIT_RE.split(text)) if len(w) >= MIN_TOKEN_LEN]


def phonetic_code(word: str) -> str:
    try:
        return jellyfish.metaphone(word) or word
    except Exception:
        return word


def _codes(tokens: List[str]) -> List[str]:
    return [phonetic_code(t) for t in tokens]


def tokens_match(a: str, b: str, code_a: str, code_b: str) -> bool:
    if a == b or code_a == code_b:
        return True
    return fuzz.ratio(a, b) >= TOKEN_FUZZY_MATCH_THRESHOLD


def _greedy_overlap(a: List[Tuple[str, str]], b: List[Tuple[str, str]]) -> Tuple[int, int, int]:
    """Returns (matched, shorter_len, longer_len)."""
    if not a or not b:
        return 0, 0, 0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    used = [False] * len(longer)
    matched = 0
    for tok_s, code_s in shorter:
        for j, (tok_l, code_l) in enumerate(longer):
            if used[j]:
                continue
            if tokens_match(tok_s, tok_l, code_s, code_l):
                used[j] = True
                matched += 1
                break
    return matched, len(shorter), len(longer)


def _overlap_fraction(a: List[Tuple[str, str]], b: List[Tuple[str, str]]) -> float:
    matched, shorter_len, _ = _greedy_overlap(a, b)
    return (matched / shorter_len) if shorter_len else 0.0


# ---------------------------------------------------------------------------
# Path scoping (quality-tier folders collapse into one scope)
# ---------------------------------------------------------------------------
def split_scope(relative_path: str) -> Tuple[str, str, int]:
    """Returns (scope_key, resolution, quality_rank) for a mount-relative path.

    `scope_key` is the directory path with any quality segment removed, so
    `hindi/xhd/Gracy Singh/x.mp4` and `hindi/sd/Gracy Singh/x.mp4` share the
    scope `hindi/gracy singh`.
    """
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p][:-1]
    resolution = ""
    kept = []
    for part in parts:
        key = part.strip().lower()
        if not resolution and key in QUALITY_RANK:
            resolution = key
            continue
        kept.append(key)
    return "/".join(kept), resolution, QUALITY_RANK.get(resolution, 0)


# ---------------------------------------------------------------------------
# File representation (LLM metadata only)
# ---------------------------------------------------------------------------
@dataclass
class TrackEntry:
    file_id: str
    full_path: str
    file_name: str
    scope_key: str
    folder_path: str
    resolution: str
    quality_rank: int
    file_size: int
    song_title: str
    movie_or_album: Optional[str]
    artists: List[str]
    title: List[Tuple[str, str]] = field(default_factory=list)
    movie: List[Tuple[str, str]] = field(default_factory=list)
    artist: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def title_key(self) -> str:
        return " ".join(t for t, _ in self.title)


def build_entry(file_id: str, full_path: str, relative_path: str,
                file_size: int, llm_meta: dict) -> Optional[TrackEntry]:
    song_title = (llm_meta.get("song_title") or "").strip()
    title_tokens = tokenize(song_title)
    if not title_tokens:
        return None

    scope_key, resolution, quality_rank = split_scope(relative_path)
    movie_tokens = tokenize(llm_meta.get("movie_or_album"))
    artist_tokens: List[str] = []
    for artist in (llm_meta.get("artists") or []):
        artist_tokens.extend(tokenize(artist))

    return TrackEntry(
        file_id=file_id,
        full_path=full_path,
        file_name=os.path.basename(full_path),
        scope_key=scope_key,
        folder_path=os.path.dirname(full_path),
        resolution=resolution,
        quality_rank=quality_rank,
        file_size=file_size or 0,
        song_title=song_title,
        movie_or_album=llm_meta.get("movie_or_album"),
        artists=list(llm_meta.get("artists") or []),
        title=list(zip(title_tokens, _codes(title_tokens))),
        movie=list(zip(movie_tokens, _codes(movie_tokens))),
        artist=list(zip(artist_tokens, _codes(artist_tokens))),
    )


# ---------------------------------------------------------------------------
# Pairwise scoring
# ---------------------------------------------------------------------------
def pair_score(a: TrackEntry, b: TrackEntry) -> Tuple[float, dict]:
    matched, shorter_len, longer_len = _greedy_overlap(a.title, b.title)
    coverage_short = (matched / shorter_len) if shorter_len else 0.0
    coverage_long = (matched / longer_len) if longer_len else 0.0

    codes_a = " ".join(c for _, c in a.title)
    codes_b = " ".join(c for _, c in b.title)
    token_set_ratio = fuzz.token_set_ratio(codes_a, codes_b) / 100.0

    title_similarity = 0.45 * coverage_short + 0.35 * coverage_long + 0.20 * token_set_ratio

    # A one-word title only counts when that word actually matches; otherwise
    # short titles would pair up on fuzzy noise alone.
    gate_passed = (
        coverage_short >= TITLE_COVERAGE_MIN
        and title_similarity >= TITLE_SIMILARITY_MIN
        and (shorter_len >= 2 or coverage_short >= 1.0)
    )

    movie_similarity = _overlap_fraction(a.movie, b.movie)
    artist_similarity = _overlap_fraction(a.artist, b.artist)

    score = title_similarity * TITLE_WEIGHT
    score += movie_similarity * MOVIE_BONUS_CAP
    score += artist_similarity * ARTIST_BONUS_CAP
    score = min(100.0, score)
    if not gate_passed:
        score = min(score, 35.0)

    debug = {
        "title_tokens_a": [t for t, _ in a.title],
        "title_tokens_b": [t for t, _ in b.title],
        "movie_tokens_a": [t for t, _ in a.movie],
        "movie_tokens_b": [t for t, _ in b.movie],
        "artist_tokens_a": [t for t, _ in a.artist],
        "artist_tokens_b": [t for t, _ in b.artist],
        "title_matched": matched,
        "title_coverage_short": round(coverage_short, 3),
        "title_coverage_long": round(coverage_long, 3),
        "title_token_set_ratio": round(token_set_ratio, 3),
        "title_similarity": round(title_similarity, 3),
        "movie_similarity": round(movie_similarity, 3),
        "artist_similarity": round(artist_similarity, 3),
        "gate_passed": gate_passed,
        "final": round(score, 1),
    }
    return score, debug


def confidence_for(score: float, threshold: float) -> str:
    if score >= CONFIDENCE_HIGH_MIN:
        return "HIGH"
    if score >= threshold:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------
class UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


# ---------------------------------------------------------------------------
# Group identity
# ---------------------------------------------------------------------------
def _slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:max_len] or "group"


def _group_id_for(scope_key: str, title_key: str, member_ids: List[str]) -> str:
    digest = hashlib.sha1(f"{scope_key}|{','.join(sorted(member_ids))}".encode()).hexdigest()[:8]
    return f"{_slugify(title_key)}-{digest}"


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
class DuplicateDetector:
    def __init__(self, qdrant_client=None, collection_name: str = "",
                 similarity_threshold: float = DEFAULT_SCORE_THRESHOLD,
                 media_type: str = "auto"):
        self.qdrant = qdrant_client
        self.collection_name = collection_name
        self.media_type = media_type
        # Config carries the threshold as a 0-1 fraction; scores are 0-100.
        self.similarity_threshold = (
            similarity_threshold * 100 if similarity_threshold <= 1 else similarity_threshold
        )

    def _mount_enabled(self, mount_name: str) -> bool:
        allowed = getattr(settings.duplicates, "mounts", None)
        return not allowed or mount_name in allowed

    def _load_entries(self, mount_name: str, mount_path: str) -> List[TrackEntry]:
        files_map = mysql_db_instance.get_tracked_files_map(mount_name)
        if not files_map:
            return []

        # Files a reviewer has already marked "not a duplicate" are still
        # loaded as entries (rather than excluded outright) so a cluster that
        # still legitimately includes them keeps the *same* membership -
        # and therefore the same group_id - run over run. `_persist_group`
        # is what actually protects the reviewer's decision, by refusing to
        # write that candidate's status back to PENDING when it re-persists
        # the group. Excluding the file here instead would shrink the
        # cluster, mint a new group_id, and leave the reviewer's decision
        # stranded in an orphaned group of its own.
        names_by_rel = {
            rel_path: os.path.basename(rel_path)
            for rel_path, rec in files_map.items()
            if rec.get("id") or rec.get("vector_id")
        }
        metadata = mysql_db_instance.get_llm_metadata_bulk(list(names_by_rel.values()))

        entries: List[TrackEntry] = []
        skipped = 0
        for rel_path, file_name in names_by_rel.items():
            full_path = os.path.join(mount_path, rel_path).replace("\\", "/")
            meta = metadata.get(mysql_db_instance._normalize_file_key(file_name))
            if not meta or not (meta.get("song_title") or "").strip():
                skipped += 1
                continue
            rec = files_map[rel_path]
            entry = build_entry(
                file_id=rec.get("id") or rec.get("vector_id"),
                full_path=full_path,
                relative_path=rel_path,
                file_size=rec.get("file_size") or 0,
                llm_meta=meta,
            )
            if entry:
                entries.append(entry)
            else:
                skipped += 1

        if skipped:
            logger.info(f"Mount {mount_name}: skipped {skipped} files without usable LLM metadata")
        return entries

    def detect_for_mount(self, mount_name: str, mount_path: str):
        if not self._mount_enabled(mount_name):
            logger.info(f"Duplicate detection is not enabled for mount '{mount_name}'; skipping")
            return

        entries = self._load_entries(mount_name, mount_path)
        if len(entries) < 2:
            logger.info(f"Mount {mount_name}: fewer than 2 LLM-parsed files, nothing to compare")
            return

        # Clear only the still-active (PENDING/DUPLICATE) candidate rows ahead
        # of recomputation; groups/candidates a reviewer already marked
        # NOT_DUPLICATE (or the legacy REJECTED) are left untouched so that
        # decision — and the group it belongs to — survives across runs.
        cleared = mysql_db_instance.reset_active_duplicate_groups_for_mount(mount_name)
        if cleared:
            logger.info(f"Cleared {cleared} pending/duplicate candidate rows for mount {mount_name}")

        scopes: Dict[str, List[TrackEntry]] = defaultdict(list)
        for entry in entries:
            scopes[entry.scope_key].append(entry)

        total_groups = 0
        for scope_key, scope_entries in scopes.items():
            total_groups += self._detect_in_scope(mount_name, scope_key, scope_entries)

        logger.info(
            f"Duplicate detection complete for mount {mount_name}: "
            f"{total_groups} groups across {len(scopes)} folder scopes"
        )

    # -- per-scope work ---------------------------------------------------
    def _detect_in_scope(self, mount_name: str, scope_key: str,
                         entries: List[TrackEntry]) -> int:
        if len(entries) < 2:
            return 0

        by_id = {e.file_id: e for e in entries}

        # Block on title phonetic codes so a big folder isn't compared O(n^2).
        blocks: Dict[str, set] = defaultdict(set)
        for entry in entries:
            for _, code in entry.title:
                blocks[code].add(entry.file_id)

        pairs: Dict[Tuple[str, str], Tuple[float, dict]] = {}
        uf = UnionFind(by_id.keys())
        seen = set()
        for ids in blocks.values():
            if len(ids) < 2:
                continue
            ordered = sorted(ids)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    if (a, b) in seen:
                        continue
                    seen.add((a, b))
                    score, debug = pair_score(by_id[a], by_id[b])
                    if score >= self.similarity_threshold:
                        pairs[(a, b)] = (score, debug)
                        uf.union(a, b)

        if not pairs:
            return 0

        clusters: Dict[str, List[str]] = defaultdict(list)
        for file_id in by_id:
            clusters[uf.find(file_id)].append(file_id)

        matches: Dict[str, Dict[str, Tuple[float, dict]]] = defaultdict(dict)
        for (a, b), result in pairs.items():
            matches[a][b] = result
            matches[b][a] = result

        written = 0
        for members in clusters.values():
            if len(members) < 2:
                continue
            if self._persist_group(mount_name, scope_key, members, by_id, matches):
                written += 1
        return written

    def _persist_group(self, mount_name: str, scope_key: str, members: List[str],
                       by_id: Dict[str, TrackEntry],
                       matches: Dict[str, Dict[str, Tuple[float, dict]]]) -> bool:
        representative = max(members, key=lambda m: (len(by_id[m].title), by_id[m].quality_rank))
        title_key = by_id[representative].title_key

        # Before minting a fresh hash-derived group_id, check whether these
        # members already belong to a group from a previous run. Membership
        # can shift slightly between runs (a file added/removed from the
        # scope), which would otherwise change the hash and spawn a new,
        # disconnected group even though it's the same logical duplicate
        # cluster - stranding any prior reviewer decisions in the old one.
        member_paths = [by_id[m].full_path for m in members]
        existing_group_id = mysql_db_instance.find_existing_group_id_for_paths(mount_name, member_paths)
        group_id = existing_group_id or _group_id_for(scope_key, title_key, members)

        # Candidates a reviewer already resolved (NOT_DUPLICATE/REJECTED) for
        # this group must keep that status rather than being written back to
        # PENDING just because the detector re-clustered them here.
        existing_statuses = mysql_db_instance.get_group_candidate_statuses(group_id) if existing_group_id else {}
        resolved_statuses = {"NOT_DUPLICATE", "REJECTED"}

        rows = []
        for file_id in members:
            entry = by_id[file_id]
            partners = matches.get(file_id, {})
            if partners:
                score, debug = max(partners.values(), key=lambda result: result[0])
            else:
                score, debug = 0.0, {}
            stats = dict(debug)
            stats.update({
                "song_title": entry.song_title,
                "movie_or_album": entry.movie_or_album,
                "artists": entry.artists,
                "resolution": entry.resolution,
                # Direct matches only, so the per-file API can return the files
                # that actually matched this one rather than the whole cluster.
                "matched_file_ids": sorted(partners.keys()),
            })
            rows.append({
                "entry": entry,
                "overall_score": round(score, 1),
                "title_score": round((debug.get("title_similarity") or 0.0) * 100, 1),
                "movie_score": round((debug.get("movie_similarity") or 0.0) * 100, 1),
                "artist_score": round((debug.get("artist_similarity") or 0.0) * 100, 1),
                "confidence": confidence_for(score, self.similarity_threshold),
                "stats": stats,
            })

        # Best copy first: highest quality tier, then largest file, then score.
        rows.sort(key=lambda r: (r["entry"].quality_rank, r["entry"].file_size, r["overall_score"]),
                  reverse=True)

        inserted = mysql_db_instance.insert_duplicate_group(
            group_id=group_id,
            title_key=title_key,
            member_count=len(rows),
            mount=mount_name,
            folder_path=rows[0]["entry"].folder_path,
            scope_key=scope_key,
        )
        if not inserted:
            return False

        skipped_resolved = 0
        for rank, row in enumerate(rows, start=1):
            entry = row["entry"]
            if existing_statuses.get(entry.file_id) in resolved_statuses:
                # Leave this candidate's row untouched - a reviewer already
                # decided it's not a duplicate of this group.
                skipped_resolved += 1
                continue
            mysql_db_instance.insert_candidate(
                group_id=group_id,
                file_id=entry.file_id,
                full_path=entry.full_path,
                mount=mount_name,
                title_score=row["title_score"],
                movie_score=row["movie_score"],
                artist_score=row["artist_score"],
                overall_score=row["overall_score"],
                confidence=row["confidence"],
                status="PENDING",
                stats_json=row["stats"],
                file_name=entry.file_name,
                resolution=entry.resolution,
                rank_in_group=rank,
                is_primary=(rank == 1),
                song_title=entry.song_title,
                movie_or_album=entry.movie_or_album,
                artists=entry.artists,
            )
        if skipped_resolved:
            logger.info(
                f"Group {group_id}: kept {skipped_resolved} previously-reviewed "
                "candidate(s) as-is (not re-marked)"
            )
        return True