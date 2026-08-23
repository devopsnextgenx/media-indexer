import os
import re
import json
import hashlib
import itertools
import logging
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import jellyfish
from rapidfuzz import fuzz

from media_indexer.database import mysql_db_instance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (tune these for your collection)
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v"}

# Noise patterns (quality, upload boilerplate)
NOISE_PATTERNS = [
    r"\b\d{3,4}p\b", r"\b[48]k\b", r"\bfull\s*hd\b", r"\bhd\b", r"\buhd\b",
    r"\bblu\s*-?\s*ray\b", r"\bbrrip\b", r"\bdvdrip\b", r"\bdvdscr\b",
    r"\bwebrip\b", r"\bweb-?dl\b", r"\bhdrip\b", r"\bhdtv\b", r"\bcamrip\b",
    r"\bx\.?264\b", r"\bx\.?265\b", r"\bhevc\b", r"\baac\b",
    r"\bofficial\b", r"\bvideo\s*song\b", r"\bfull\s*video\b",
    r"\bfull\s*song\b", r"\blyrical\b", r"\bwith\s*lyrics\b",
    r"\baudio\b", r"\bsong\b", r"\bvideo\b",
    r"\bnew\b", r"\blatest\b", r"\bremix\b", r"\bremaster(ed)?\b",
    r"\bexclusive\b", r"\bjukebox\b", r"\bslowed\s*(and|&)?\s*reverb\b",
    r"\blofi\b", r"\bstatus\b", r"\bwhatsapp\s*status\b", r"\bshorts?\b",
    r"\breels?\b", r"\btrending\b", r"\bviral\b", r"\bfull\s*movie\b",
    r"\bpart\s*\d+\b",
]

PRODUCTION_NOISE = [
    r"\bt-?series\b", r"\bzee\s*music(\s*company)?\b", r"\bsony\s*music(\s*india)?\b",
    r"\bsaregama\b", r"\beros\s*now\b", r"\btips\b", r"\bvenus\b",
    r"\bshemaroo\b", r"\btimes\s*music\b", r"\bspeed\s*records\b",
    r"\bwhite\s*hill\s*music\b", r"\bvyrl(\s*originals?)?\b",
    r"\bdesi\s*music\s*factory\b", r"\bgeet\s*mp3\b", r"\bultra\s*bollywood\b",
    r"\byrf\b", r"\bmovies\b", r"\bmusic\b", r"\bproduction(s)?\b",
    r"\bfilms?\b", r"\brecords?\b", r"\bentertainment\b", r"\bstudios?\b",
    r"\bindustries\b",
]
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS + PRODUCTION_NOISE), flags=re.IGNORECASE)
_SPLIT_RE = re.compile(r"[|｜_\-\(\)\[\]{}.,:;/\\!?~]+")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

MIN_TOKEN_LEN = 2
TOKEN_FUZZY_MATCH_THRESHOLD = 82
PAIR_SCORE_THRESHOLD = 72

# Artist name hints (common Indian film names)
ARTIST_NAME_HINTS = {
    "khan", "kapoor", "kaif", "chopra", "roy", "devgn", "devgan", "hashmi",
    "shah", "warsi", "nigam", "yagnik", "mukherjee", "singh", "kumar",
    "sharma", "shetty", "kaur", "bharuccha", "banerjee", "vashisth",
    "tiwari", "sikandar", "mohan", "chandra", "cruz", "dcruz", "tandon",
    "leone", "gupta", "vishwakarma", "thadani", "bagchi", "trivedi",
    "bhattacharya", "martis", "singhania", "rathod", "chauhan", "malik",
    "dutt", "kirkire", "mukerji", "padukone", "ranbir", "arijit", "sonu",
    "alka", "udit", "narayan", "sadhana", "sargam", "sanu", "kumar sanu",
    "amitabh", "abbas", "sridevi", "kajol", "katrina", "shraddha",
    "parineeti", "priyanka", "deepika", "alia", "kareena", "karisma",
    "madhuri", "juhi", "rani", "preity", "bipasha", "esha", "sunny",
    "amrita", "ayesha", "neha", "kakkar", "honey", "nushrratt", "mouni",
    "ileana", "shilpa", "shetty", "gracy", "sonam", "elif", "rimal",
    "adil", "geeta", "basara", "neetu", "mayuri", "sonal", "devraj",
    "malvika", "neeti", "simran", "priya", "vani", "montrose", "astitwa",
    "salman", "vikram", "shekhar", "akshay", "emraan", "varun", "dhawan",
    "pritam", "ajay", "arshad", "naseeruddin",
}

# Corpus frequency thresholds
NAME_TIER_MIN_DF = 5
NGRAM_MIN_DF = 2
NGRAM_SIZES = (2, 3)

# Scoring weights
TITLE_OVERLAP_WEIGHT = 0.80
TITLE_TOKEN_SET_RATIO_WEIGHT = 0.20
NAME_OVERLAP_BONUS_CAP = 8.0
MIN_TITLE_TOKENS_FOR_CONFIDENT_MATCH = 1
MIN_MATCHED_TITLE_TOKENS_ABS = 2
UNMATCHED_TOLERANCE_ABS = 1
MIN_SHORTER_MATCH_FRACTION = 0.6

CONFIDENCE_HIGH_MIN = 85
CONFIDENCE_MEDIUM_MIN = PAIR_SCORE_THRESHOLD

# ---------------------------------------------------------------------------
# Tokenization & phonetic normalisation
# ---------------------------------------------------------------------------
def clean_and_tokenize(filename: str) -> List[str]:
    name = os.path.splitext(filename)[0]
    name = _SPLIT_RE.sub(" ", name)
    name = _NOISE_RE.sub(" ", name)
    tokens = []
    for w in name.split():
        w = w.strip().lower()
        if len(w) < MIN_TOKEN_LEN:
            continue
        if w.isdigit() and not _YEAR_RE.match(w):
            continue
        tokens.append(w)
    return tokens

def phonetic_code(word: str) -> str:
    try:
        code = jellyfish.metaphone(word)
        return code if code else word
    except Exception:
        return word

def tokens_match(a: str, b: str, code_a: str, code_b: str) -> bool:
    if code_a == code_b:
        return True
    return fuzz.ratio(a, b) >= TOKEN_FUZZY_MATCH_THRESHOLD

# ---------------------------------------------------------------------------
# File representation
# ---------------------------------------------------------------------------
@dataclass
class SongFile:
    file_id: str                     # point_id from processed_files
    full_path: str
    tokens: List[str] = field(default_factory=list)
    codes: List[str] = field(default_factory=list)
    tiers: List[str] = field(default_factory=list)   # 'title', 'movie', 'artist'
    folder_tokens: Set[str] = field(default_factory=set)

    def tier_tokens(self, tier: str) -> List[Tuple[str, str]]:
        return [(t, c) for t, c, tr in zip(self.tokens, self.codes, self.tiers) if tr == tier]

    def tokens_str(self, tier: str) -> str:
        return ", ".join(t for t, _ in self.tier_tokens(tier))

# ---------------------------------------------------------------------------
# Corpus statistics (for tier classification)
# ---------------------------------------------------------------------------
class CorpusStats:
    def __init__(self, files: List[SongFile]):
        self.n_files = len(files)
        code_to_file_ids = defaultdict(set)
        code_example = {}
        ngram_to_file_ids = defaultdict(set)
        ngram_example = {}

        for f in files:
            for tok, code in zip(f.tokens, f.codes):
                code_to_file_ids[code].add(f.file_id)
                code_example.setdefault(code, tok)

            for n in NGRAM_SIZES:
                for i in range(len(f.codes) - n + 1):
                    gram_codes = tuple(f.codes[i:i+n])
                    gram_words = " ".join(f.tokens[i:i+n])
                    ngram_to_file_ids[gram_codes].add(f.file_id)
                    ngram_example.setdefault(gram_codes, gram_words)

        self.df = {code: len(ids) for code, ids in code_to_file_ids.items()}
        self.example_word = code_example

        self.ngram_df = {gram: len(ids) for gram, ids in ngram_to_file_ids.items()
                         if len(ids) >= NGRAM_MIN_DF}
        self.ngram_example = ngram_example

        # Phrases where no member word is an artist hint -> movie-name candidates
        self.movie_phrase_words = set()
        for gram, df in self.ngram_df.items():
            words = ngram_example[gram].split()
            if not any(w in ARTIST_NAME_HINTS for w in words):
                self.movie_phrase_words.update(words)

    def is_recurring(self, code: str) -> bool:
        return self.df.get(code, 0) >= NAME_TIER_MIN_DF

# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------
def classify_files(files: List[SongFile], stats: CorpusStats):
    for f in files:
        tiers = []
        for tok, code in zip(f.tokens, f.codes):
            if tok in ARTIST_NAME_HINTS:
                tiers.append("artist")
            elif tok in f.folder_tokens:   # folder name as artist hint
                tiers.append("artist")
            elif stats.is_recurring(code):
                tiers.append("movie" if tok in stats.movie_phrase_words else "artist")
            else:
                tiers.append("title")
        f.tiers = tiers

# ---------------------------------------------------------------------------
# Pairwise scoring (gated on title)
# ---------------------------------------------------------------------------
def _greedy_overlap(tokens_codes_a, tokens_codes_b):
    if not tokens_codes_a or not tokens_codes_b:
        return 0, 0
    shorter, longer = (tokens_codes_a, tokens_codes_b) if len(tokens_codes_a) <= len(tokens_codes_b) \
        else (tokens_codes_b, tokens_codes_a)
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
    return matched, len(shorter)

def _overlap_pct(tokens_codes_a, tokens_codes_b):
    matched, shorter_len = _greedy_overlap(tokens_codes_a, tokens_codes_b)
    return round((matched / shorter_len) * 100, 1) if shorter_len else 0.0

def pair_score(f1: SongFile, f2: SongFile):
    title_a, title_b = f1.tier_tokens("title"), f2.tier_tokens("title")
    movie_a, movie_b = f1.tier_tokens("movie"), f2.tier_tokens("movie")
    artist_a, artist_b = f1.tier_tokens("artist"), f2.tier_tokens("artist")
    name_a, name_b = movie_a + artist_a, movie_b + artist_b

    title_matched, title_shorter_len = _greedy_overlap(title_a, title_b)
    name_matched, name_shorter_len = _greedy_overlap(name_a, name_b)

    title_overlap = (title_matched / title_shorter_len) if title_shorter_len else 0.0
    name_overlap = (name_matched / name_shorter_len) if name_shorter_len else 0.0

    title_str_a = " ".join(c for _, c in title_a)
    title_str_b = " ".join(c for _, c in title_b)
    title_tsr = (fuzz.token_set_ratio(title_str_a, title_str_b) / 100.0) if (title_str_a and title_str_b) else 0.0

    primary = (TITLE_OVERLAP_WEIGHT * title_overlap + TITLE_TOKEN_SET_RATIO_WEIGHT * title_tsr) * 100

    if title_shorter_len < MIN_TITLE_TOKENS_FOR_CONFIDENT_MATCH:
        primary = min(primary, 40)
    if title_matched < MIN_MATCHED_TITLE_TOKENS_ABS:
        primary = min(primary, 45)

    name_bonus = min(name_overlap * NAME_OVERLAP_BONUS_CAP, NAME_OVERLAP_BONUS_CAP)
    final = min(primary + name_bonus, 100.0)

    # Hard gate: absolute unmatched tokens
    raw_pairs_a, raw_pairs_b = list(zip(f1.tokens, f1.codes)), list(zip(f2.tokens, f2.codes))
    raw_matched, raw_shorter_len = _greedy_overlap(raw_pairs_a, raw_pairs_b)
    unmatched_shorter = raw_shorter_len - raw_matched
    shorter_match_fraction = (raw_matched / raw_shorter_len) if raw_shorter_len else 0.0
    gate_passed = (unmatched_shorter <= UNMATCHED_TOLERANCE_ABS
                   and shorter_match_fraction >= MIN_SHORTER_MATCH_FRACTION)
    if not gate_passed:
        final = min(final, 35.0)

    title_score = round(primary, 1)
    movie_score = _overlap_pct(movie_a, movie_b)
    artist_score = _overlap_pct(artist_a, artist_b)

    debug = {
        "title_tokens_a": [t for t, _ in title_a], "title_tokens_b": [t for t, _ in title_b],
        "movie_tokens_a": [t for t, _ in movie_a], "movie_tokens_b": [t for t, _ in movie_b],
        "artist_tokens_a": [t for t, _ in artist_a], "artist_tokens_b": [t for t, _ in artist_b],
        "title_overlap": round(title_overlap, 3), "title_tsr": round(title_tsr, 3),
        "title_matched": title_matched, "name_overlap": round(name_overlap, 3),
        "primary": round(primary, 1), "name_bonus": round(name_bonus, 1),
        "unmatched_shorter": unmatched_shorter,
        "shorter_match_fraction": round(shorter_match_fraction, 3),
        "gate_passed": gate_passed, "final": round(final, 1),
    }
    return final, title_score, movie_score, artist_score, debug

def confidence_for(score: float) -> str:
    if score >= CONFIDENCE_HIGH_MIN:
        return "HIGH"
    if score >= CONFIDENCE_MEDIUM_MIN:
        return "MEDIUM"
    return "LOW"

# ---------------------------------------------------------------------------
# Candidate blocking (avoid O(n²))
# ---------------------------------------------------------------------------
def build_candidate_pairs(files: List[SongFile]) -> Set[Tuple[str, str]]:
    index = defaultdict(set)
    for f in files:
        for code in set(f.codes):
            index[code].add(f.file_id)

    max_bucket = max(20, int(len(files) * 0.05))
    pairs = set()
    for code, ids in index.items():
        if len(ids) >= 2 and len(ids) <= max_bucket:
            for a, b in itertools.combinations(sorted(ids), 2):
                pairs.add((a, b))
    return pairs

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
# Group ID generation
# ---------------------------------------------------------------------------
def _slugify(text: str, max_len: int = 48) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len] or "group"

def _group_id_for(members: List[str], by_id: Dict[str, SongFile]) -> str:
    rep = max(members, key=lambda m: len(by_id[m].tier_tokens("title")))
    title_words = [t for t, _ in by_id[rep].tier_tokens("title")]
    base = _slugify(" ".join(title_words)) if title_words else _slugify(
        os.path.splitext(os.path.basename(by_id[rep].full_path))[0])
    digest = hashlib.sha1(",".join(sorted(members)).encode()).hexdigest()[:8]
    return f"{base}-{digest}"

# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------
class DuplicateDetector:
    def __init__(self, qdrant_client, collection_name: str,
                 similarity_threshold: float = PAIR_SCORE_THRESHOLD,
                 media_type: str = "auto"):
        self.qdrant = qdrant_client
        self.collection_name = collection_name
        self.similarity_threshold = similarity_threshold
        self.media_type = media_type

    def detect_for_mount(self, mount_name: str, mount_path: str):
        """Run duplicate detection for all files in a mount."""
        # 1. Fetch all processed files for this mount from MySQL
        files_map = mysql_db_instance.get_tracked_files_map(mount_name)
        if not files_map:
            logger.info(f"No files found for mount {mount_name}")
            return

        # 2. Build SongFile objects
        song_files = []
        for rel_path, rec in files_map.items():
            full_path = os.path.join(mount_path, rel_path).replace("\\", "/")
            file_id = rec.get("id") or rec.get("vector_id")  # point_id stored as id
            if not file_id:
                continue
            filename = os.path.basename(rel_path)
            parent_folder = os.path.basename(os.path.dirname(rel_path))
            tokens = clean_and_tokenize(filename)
            codes = [phonetic_code(t) for t in tokens]
            folder_tokens = set(clean_and_tokenize(parent_folder))
            song_files.append(SongFile(
                file_id=file_id,
                full_path=full_path,
                tokens=tokens,
                codes=codes,
                folder_tokens=folder_tokens,
                tiers=[]  # filled later
            ))

        if len(song_files) < 2:
            logger.info(f"Mount {mount_name} has fewer than 2 files, skipping duplicate detection.")
            return

        # 3. Delete old duplicate groups for this mount
        deleted = mysql_db_instance.delete_duplicate_groups_for_mount(mount_name)
        if deleted:
            logger.info(f"Deleted {deleted} existing duplicate groups for mount {mount_name}")

        # 4. Corpus statistics
        logger.info(f"Computing corpus stats for {len(song_files)} files in {mount_name}")
        stats = CorpusStats(song_files)

        # 5. Classify tokens
        classify_files(song_files, stats)

        # 6. Candidate blocking & scoring
        logger.info("Building candidate pairs...")
        candidate_pairs = build_candidate_pairs(song_files)
        logger.info(f"Generated {len(candidate_pairs)} candidate pairs to score")

        by_id = {f.file_id: f for f in song_files}
        uf = UnionFind(by_id.keys())

        pair_data = {}  # (a,b) -> (final, title_score, movie_score, artist_score, debug)
        scored = 0
        for a, b in candidate_pairs:
            result = pair_score(by_id[a], by_id[b])
            if result[0] >= self.similarity_threshold:
                pair_data[(a, b)] = result
                uf.union(a, b)
            scored += 1
            if scored % 1000 == 0:
                logger.info(f"Scored {scored}/{len(candidate_pairs)} pairs")

        # 7. Grouping
        clusters = defaultdict(list)
        for fid in by_id:
            clusters[uf.find(fid)].append(fid)

        groups = []
        for root, members in clusters.items():
            if len(members) < 2:
                continue
            group_id = _group_id_for(members, by_id)
            entries = []
            for m in members:
                candidates = [pair_data.get((min(m, o), max(m, o)))
                              for o in members if o != m and (min(m, o), max(m, o)) in pair_data]
                if candidates:
                    best = max(candidates, key=lambda r: r[0])
                    final, title_score, movie_score, artist_score, debug = best
                else:
                    final, title_score, movie_score, artist_score, debug = 0.0, 0.0, 0.0, 0.0, {}
                entries.append({
                    "file_id": m,
                    "full_path": by_id[m].full_path,
                    "overall_score": round(final, 1),
                    "title_score": title_score,
                    "movie_score": movie_score,
                    "artist_score": artist_score,
                    "confidence": confidence_for(final),
                    "status": "PENDING",
                    "stats_json": debug,
                })
            groups.append({"group_id": group_id, "members": entries})

        if not groups:
            logger.info(f"No duplicate groups found for mount {mount_name}")
            return

        # 8. Write to database
        logger.info(f"Writing {len(groups)} duplicate groups for mount {mount_name}")
        for group in groups:
            gid = group["group_id"]
            # Determine title_key from representative member with highest title score
            rep = max(group["members"], key=lambda e: e["title_score"])
            title_key = " ".join(rep["stats_json"].get("title_tokens_a", [])) or rep["full_path"]
            folder_path = os.path.dirname(rep["full_path"])

            mysql_db_instance.insert_duplicate_group(
                group_id=gid,
                title_key=title_key,
                member_count=len(group["members"]),
                mount=mount_name,
                folder_path=folder_path
            )

            for member in group["members"]:
                mysql_db_instance.insert_candidate(
                    group_id=gid,
                    file_id=member["file_id"],
                    full_path=member["full_path"],
                    mount=mount_name,
                    title_score=member["title_score"],
                    movie_score=member["movie_score"],
                    artist_score=member["artist_score"],
                    overall_score=member["overall_score"],
                    confidence=member["confidence"],
                    status=member["status"],
                    stats_json=member["stats_json"]
                )

        logger.info(f"Duplicate detection complete for mount {mount_name}: {len(groups)} groups")