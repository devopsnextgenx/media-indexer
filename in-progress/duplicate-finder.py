#!/usr/bin/env python3
"""
find_duplicate_songs.py

Detect duplicate candidates in a video-song collection based on filename
title similarity — robust to:
  - reordered fields (title / movie / artist / actor in any order)
  - optional fields (movie name, artist, actor may or may not be present)
  - noise tokens (4k, hd, 1080p, bluray, "video song", etc.)
  - phonetic spelling variants common in Hindi/South-Asian transliteration
    ("luv" vs "love", "nite" vs "night", "zindagi" vs "jindagi")
  - movie-name / cast-list overlap between DIFFERENT songs from the same
    movie (the main source of false positives — see CORPUS STATS below)

Dependencies:
    pip install rapidfuzz jellyfish

Design summary
--------------
1. CLEAN: strip noise words/patterns + extension, split on delimiters
   (| , - _ () [] etc.) into raw tokens.
2. PHONETIC NORMALIZE: Metaphone code per token, collapsing spelling
   variants ("zindagi"/"jindagi"), with a fuzzy-ratio fallback for cases
   Metaphone doesn't fully collapse.
3. CORPUS STATS (2-pass, NEW): before scoring any pair, scan the WHOLE
   collection and count how many distinct files each phonetic code
   appears in (document frequency, df). A token that shows up in many
   files is almost certainly a movie name / artist / cast name — every
   song from the same movie repeats them. A token that shows up in only
   a couple of files is much more likely to be part of an actual,
   song-specific TITLE. This is standard IDF (inverse document
   frequency) reasoning, applied to filename tokens instead of documents.
4. CLASSIFY each token per file into a tier using the df count:
       - "title"  : df below threshold  -> song-specific, high signal
       - "name"   : df at/above threshold -> recurring movie/artist/cast
   (also exports this as human-readable stats for you to review/tune)
5. SCORE A PAIR: primary score is the TITLE-tier overlap (gated — this
   is what must be high for a duplicate). Name-tier overlap only
   contributes a small bonus and can never push a pair over the
   threshold by itself. This directly fixes the "different song, same
   movie + same cast" false-positive pattern.
6. BLOCK before scoring (avoid O(n^2)): candidate pairs are generated
   primarily from shared TITLE-tier tokens (falls back to name-tier only
   if a file has no title tokens at all).
7. GROUP: Union-Find over pairs scoring above threshold.
8. REPORT: prints to console and can write groups.md + stats.md.
"""

import os
import re
import math
import itertools
import argparse
from dataclasses import dataclass, field
from collections import defaultdict, Counter

import jellyfish
from rapidfuzz import fuzz

# --------------------------------------------------------------------------
# 1. CONFIG — tune / extend these lists for your collection
# --------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v"}

# Patterns removed wholesale before tokenizing (quality/source/codec tags,
# generic promo words). Extend freely — this is the main lever for
# collection-specific noise.
NOISE_PATTERNS = [
    r"\b\d{3,4}p\b",                       # 1080p, 720p, 480p
    r"\b[48]k\b",                          # 4k, 8k
    r"\bfull\s*hd\b", r"\bhd\b",
    r"\bblu\s*-?\s*ray\b", r"\bbrrip\b", r"\bdvdrip\b", r"\bdvdscr\b",
    r"\bwebrip\b", r"\bweb-?dl\b", r"\bhdrip\b", r"\bhdtv\b", r"\bcamrip\b",
    r"\bx\.?264\b", r"\bx\.?265\b", r"\bhevc\b", r"\baac\b",
    r"\bofficial\b", r"\bvideo\s*song\b", r"\bfull\s*video\b",
    r"\bfull\s*song\b", r"\blyrical\b", r"\baudio\b", r"\bsong\b",
    r"\bvideo\b", r"\bnew\b", r"\blatest\b", r"\bremix\b", r"\bremaster(ed)?\b",
]
_NOISE_RE = re.compile("|".join(NOISE_PATTERNS), flags=re.IGNORECASE)

# Delimiters that separate fields/words in these filenames
_SPLIT_RE = re.compile(r"[|_\-\(\)\[\]{}.,:;/\\]+")

# Tokens too generic to help matching / too likely to be pure noise digits
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

MIN_TOKEN_LEN = 2                 # drop 1-char leftovers ("s", "a")
TOKEN_FUZZY_MATCH_THRESHOLD = 82  # rapidfuzz ratio() for token-level fallback match
PAIR_SCORE_THRESHOLD = 72         # final duplicate-candidate cutoff (0-100), tune per collection

# --- NEW: corpus-frequency / tiering config -------------------------------
# A phonetic code appearing in >= this many DISTINCT files is treated as a
# recurring "name" token (movie/artist/cast) rather than a title word.
# Tune this against your collection: if movies typically have ~5-15 songs,
# 3-4 is a reasonable floor (a title word repeating that often by chance is
# less likely than a cast/movie name repeating that often deliberately).
NAME_TIER_MIN_DF = 3

# If True, tokens that also appear in the file's own parent-folder name are
# forced into the "name" tier regardless of df. Useful when your collection
# is organized like .../hindi/hd/<Artist Name>/song_file.mp4 — the folder
# name is a strong, free signal for who the artist/actress is.
USE_FOLDER_NAME_AS_NAME_HINT = True

TITLE_OVERLAP_WEIGHT = 0.80        # primary, gated score
TITLE_TOKEN_SET_RATIO_WEIGHT = 0.20
NAME_OVERLAP_BONUS_CAP = 8.0        # max points a pure name/cast overlap can add
MIN_TITLE_TOKENS_FOR_CONFIDENT_MATCH = 1  # if shorter file has 0 title tokens, be conservative
MIN_MATCHED_TITLE_TOKENS_ABS = 2    # absolute floor: a single coincidentally-shared
                                     # title word must never alone yield a confident match

# --- NEW: phrase (n-gram) mining config — REPORTING ONLY -------------------
# A movie name ("Zara Hatke Zara Bachke") is often made of ordinary words
# individually, so unigram document-frequency alone can't reliably tell
# "recurring movie name" apart from "common lyrics word". Mining recurring
# n-grams surfaces movie-name / stable-artist-name CANDIDATES in stats.md
# for you to review and promote into a curated library (--name-library).
# NOTE: this is intentionally NOT used to auto-gate matches — tried that,
# and it backfired: a genuine duplicate pair's own title also "recurs" as
# an n-gram across exactly its own 2 copies, which wrongly demoted real
# title tokens to name-tier. Keeping it informational avoids that trap.
NGRAM_MIN_DF = 2
NGRAM_SIZES = (2, 3)

# --- NEW: the actual false-positive fix -----------------------------------
# Hard requirement: the SHORTER filename's tokens must almost entirely find
# a partner in the longer filename. A long shared cast list can make crude
# overlap ratios look high even for two completely different songs from
# the same movie (2-4 differing title words vs 6-8 shared cast words) — so
# gating on a RATIO is exactly what caused the false positives you saw.
# Gating on the ABSOLUTE count of leftover, unexplained tokens does not
# have that failure mode: 2 genuinely different title words left over is
# 2 leftover words whether the shared cast list is 3 tokens or 8.
UNMATCHED_TOLERANCE_ABS = 1         # shorter file may have at most this many
                                     # totally unmatched tokens and still count
MIN_SHORTER_MATCH_FRACTION = 0.6    # plus a floor so 1-of-2 tokens doesn't pass


# --------------------------------------------------------------------------
# 2. CLEAN + TOKENIZE
# --------------------------------------------------------------------------

def clean_and_tokenize(filename: str):
    """Strip extension + noise, split into normalized word tokens."""
    name = os.path.splitext(filename)[0]
    # Replace delimiters (incl. _) with spaces FIRST so noise-pattern \b
    # boundaries work correctly — \b treats "_" as a word char, so "_HD_"
    # would otherwise not match \bhd\b.
    name = _SPLIT_RE.sub(" ", name)
    name = _NOISE_RE.sub(" ", name)
    raw_tokens = [t.strip().lower() for t in name.split()]

    tokens = []
    for w in raw_tokens:
        if len(w) < MIN_TOKEN_LEN:
            continue
        if w.isdigit() and not _YEAR_RE.match(w):
            continue
        tokens.append(w)
    return tokens


# --------------------------------------------------------------------------
# 3. PHONETIC NORMALIZATION
# --------------------------------------------------------------------------

def phonetic_code(word: str) -> str:
    """Metaphone collapses common transliteration spelling variants
    (love/luv, night/nite -> similar consonant skeletons)."""
    try:
        code = jellyfish.metaphone(word)
        return code if code else word
    except Exception:
        return word


def tokens_match(a: str, b: str, code_a: str, code_b: str) -> bool:
    if code_a == code_b:
        return True
    return fuzz.ratio(a, b) >= TOKEN_FUZZY_MATCH_THRESHOLD


# --------------------------------------------------------------------------
# 4. FILE RECORD
# --------------------------------------------------------------------------

@dataclass
class SongFile:
    file_id: int
    full_path: str
    tokens: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    tiers: list = field(default_factory=list)   # parallel to tokens: "title" | "name"
    folder_tokens: set = field(default_factory=set)

    @property
    def phonetic_string(self) -> str:
        return " ".join(self.codes)

    def tier_tokens(self, tier):
        return [(t, c) for t, c, tr in zip(self.tokens, self.codes, self.tiers) if tr == tier]

    @property
    def title_phonetic_string(self) -> str:
        return " ".join(c for c in self.codes_by_tier("title"))

    def codes_by_tier(self, tier):
        return [c for c, tr in zip(self.codes, self.tiers) if tr == tier]


def build_song_file(file_id: int, full_path: str) -> SongFile:
    filename = os.path.basename(full_path)
    parent_folder = os.path.basename(os.path.dirname(full_path))
    tokens = clean_and_tokenize(filename)
    codes = [phonetic_code(t) for t in tokens]
    folder_tokens = set(clean_and_tokenize(parent_folder))
    return SongFile(file_id=file_id, full_path=full_path, tokens=tokens,
                     codes=codes, folder_tokens=folder_tokens)


# --------------------------------------------------------------------------
# 5. CORPUS STATS + TIER CLASSIFICATION  (NEW)
# --------------------------------------------------------------------------

class CorpusStats:
    """Document-frequency stats over phonetic codes AND recurring n-gram
    phrases across the whole collection — used to tell 'recurring name'
    tokens (movie names, artist/cast names) apart from 'song-specific
    title' tokens (including common lyrics words)."""

    def __init__(self, files):
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
                    gram_codes = tuple(f.codes[i:i + n])
                    gram_words = " ".join(f.tokens[i:i + n])
                    ngram_to_file_ids[gram_codes].add(f.file_id)
                    ngram_example.setdefault(gram_codes, gram_words)

        self.df = {code: len(ids) for code, ids in code_to_file_ids.items()}
        self.example_word = code_example

        # Only keep n-grams that actually recur across >=2 distinct files —
        # these are the phrase "library" candidates (movie names, stable
        # multi-word artist names).
        self.ngram_df = {gram: len(ids) for gram, ids in ngram_to_file_ids.items()
                          if len(ids) >= NGRAM_MIN_DF}
        self.ngram_example = ngram_example

        # Build a fast lookup: which codes participate in at least one
        # qualifying recurring phrase.
        self.codes_in_recurring_phrase = set()
        for gram in self.ngram_df:
            self.codes_in_recurring_phrase.update(gram)

    def idf(self, code: str) -> float:
        df = self.df.get(code, 1)
        return math.log((self.n_files + 1) / (df + 1)) + 1

    def tier(self, code: str) -> str:
        # Unigram frequency alone is used for tiering (reliable for artist
        # names, which recur across many DIFFERENT movies/songs in the
        # full collection). Mined n-gram phrases are reported in stats.md
        # for manual review/curation but not auto-applied here — see the
        # NGRAM_MIN_DF comment above for why.
        return "name" if self.df.get(code, 0) >= NAME_TIER_MIN_DF else "title"


def classify_files(files, stats: CorpusStats):
    """Assign a per-token tier to every file in place."""
    for f in files:
        tiers = []
        for tok, code in zip(f.tokens, f.codes):
            if USE_FOLDER_NAME_AS_NAME_HINT and tok in f.folder_tokens:
                tiers.append("name")
            else:
                tiers.append(stats.tier(code))
        f.tiers = tiers


# --------------------------------------------------------------------------
# 6. PAIRWISE SCORING  (gated on title tier)
# --------------------------------------------------------------------------

def _greedy_overlap(tokens_codes_a, tokens_codes_b):
    """Order-agnostic greedy match count between two (token, code) lists."""
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


def pair_score(f1: SongFile, f2: SongFile):
    """Returns (final_score, debug_dict)."""
    title_a = f1.tier_tokens("title")
    title_b = f2.tier_tokens("title")
    name_a = f1.tier_tokens("name")
    name_b = f2.tier_tokens("name")

    title_matched, title_shorter_len = _greedy_overlap(title_a, title_b)
    name_matched, name_shorter_len = _greedy_overlap(name_a, name_b)

    title_overlap = (title_matched / title_shorter_len) if title_shorter_len else 0.0
    name_overlap = (name_matched / name_shorter_len) if name_shorter_len else 0.0

    title_str_a = " ".join(c for _, c in title_a)
    title_str_b = " ".join(c for _, c in title_b)
    title_tsr = (fuzz.token_set_ratio(title_str_a, title_str_b) / 100.0) if (title_str_a and title_str_b) else 0.0

    primary = (TITLE_OVERLAP_WEIGHT * title_overlap + TITLE_TOKEN_SET_RATIO_WEIGHT * title_tsr) * 100

    # If neither file has any title-tier tokens at all, we have no reliable
    # signal — do not let name-tier overlap alone produce a "confident" match.
    if title_shorter_len < MIN_TITLE_TOKENS_FOR_CONFIDENT_MATCH:
        primary = min(primary, 40)  # cap — flags as low-confidence only

    # A single coincidentally-shared title word (a common lyrics word that
    # also happens to sit inside the movie-name phrase, or wasn't caught by
    # the name-tier classifier) must not alone yield a "confident" ratio —
    # e.g. shorter file has exactly 1 title token and it happens to match.
    if title_matched < MIN_MATCHED_TITLE_TOKENS_ABS:
        primary = min(primary, 45)

    name_bonus = min(name_overlap * NAME_OVERLAP_BONUS_CAP, NAME_OVERLAP_BONUS_CAP)

    final = min(primary + name_bonus, 100.0)

    # HARD GATE: how many of the shorter file's tokens found NO partner at
    # all in the longer file (raw, all tiers, fuzzy/phonetic match)? This
    # is what actually distinguishes "same movie, different song" (title
    # words are genuinely left over, unmatched, no matter how big the
    # shared cast list is) from a true duplicate (almost nothing is left
    # unexplained). Gating on this ABSOLUTE count — not a ratio — is the
    # fix: a long shared cast list can't launder a real title mismatch.
    raw_pairs_a = list(zip(f1.tokens, f1.codes))
    raw_pairs_b = list(zip(f2.tokens, f2.codes))
    raw_matched, raw_shorter_len = _greedy_overlap(raw_pairs_a, raw_pairs_b)
    unmatched_shorter = raw_shorter_len - raw_matched
    shorter_match_fraction = (raw_matched / raw_shorter_len) if raw_shorter_len else 0.0

    gate_passed = (unmatched_shorter <= UNMATCHED_TOLERANCE_ABS
                   and shorter_match_fraction >= MIN_SHORTER_MATCH_FRACTION)
    if not gate_passed:
        final = min(final, 35.0)  # force below any sane threshold

    debug = {
        "title_tokens_a": [t for t, _ in title_a], "title_tokens_b": [t for t, _ in title_b],
        "name_tokens_a": [t for t, _ in name_a], "name_tokens_b": [t for t, _ in name_b],
        "title_overlap": round(title_overlap, 3), "title_tsr": round(title_tsr, 3),
        "title_matched": title_matched,
        "name_overlap": round(name_overlap, 3), "primary": round(primary, 1),
        "name_bonus": round(name_bonus, 1),
        "unmatched_shorter": unmatched_shorter, "shorter_match_fraction": round(shorter_match_fraction, 3),
        "gate_passed": gate_passed, "final": round(final, 1),
    }
    return final, debug


# --------------------------------------------------------------------------
# 7. BLOCKING (avoid O(n^2) over large collections)
# --------------------------------------------------------------------------

def build_candidate_pairs(files):
    """Generate candidate pairs from ANY shared phonetic code (title or
    name tier) — blocking is only about not missing a pair, not about
    filtering false positives. The tier-gated logic in pair_score() is
    what actually rejects movie/cast-only overlap; restricting blocking
    to title-tier codes alone would risk missing true duplicates whose
    only exact-code overlap happens to fall on a name-tier token (e.g. a
    small collection where a title word's df happens to cross the
    name-tier threshold) while their real title match is only a fuzzy
    (non-exact-code) match, like zindagi/jindagi."""
    index = defaultdict(set)
    for f in files:
        for code in set(f.codes):
            index[code].add(f.file_id)

    max_bucket = max(20, int(len(files) * 0.05))
    pairs = set()
    for code, ids in index.items():
        if len(ids) < 2 or len(ids) > max_bucket:
            continue
        for a, b in itertools.combinations(sorted(ids), 2):
            pairs.add((a, b))
    return pairs


# --------------------------------------------------------------------------
# 8. UNION-FIND GROUPING
# --------------------------------------------------------------------------

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


def find_duplicate_groups(files, threshold: float = PAIR_SCORE_THRESHOLD):
    by_id = {f.file_id: f for f in files}
    uf = UnionFind(by_id.keys())
    pair_scores = {}
    pair_debug = {}

    for a, b in build_candidate_pairs(files):
        score, debug = pair_score(by_id[a], by_id[b])
        if score >= threshold:
            pair_scores[(a, b)] = score
            pair_debug[(a, b)] = debug
            uf.union(a, b)

    groups = defaultdict(list)
    for fid in by_id:
        groups[uf.find(fid)].append(fid)

    result = []
    for root, members in groups.items():
        if len(members) < 2:
            continue
        entries = []
        for m in members:
            candidates = [(pair_scores.get((min(m, o), max(m, o)), 0),
                           pair_debug.get((min(m, o), max(m, o)), {}))
                          for o in members if o != m]
            best_score, best_debug = max(candidates, key=lambda x: x[0], default=(0, {}))
            entries.append({"file_id": m, "full_path": by_id[m].full_path,
                             "status": "pending", "similarity_score": round(best_score, 1),
                             "debug": best_debug})
        result.append(entries)
    return result


# --------------------------------------------------------------------------
# 9. SCAN
# --------------------------------------------------------------------------

def scan_directory(root_dir: str):
    files = []
    fid = 0
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTENSIONS:
                files.append(build_song_file(fid, os.path.join(dirpath, fn)))
                fid += 1
    return files


# --------------------------------------------------------------------------
# 10. REPORTING — console + groups.md + stats.md
# --------------------------------------------------------------------------

def print_groups(groups):
    print(f"Found {len(groups)} duplicate-candidate group(s):\n")
    for gi, group in enumerate(groups, 1):
        print(f"--- Group {gi} ---")
        for entry in sorted(group, key=lambda e: -e["similarity_score"]):
            d = entry["debug"]
            print(f"  [{entry['similarity_score']:>5}] {entry['full_path']}  (status={entry['status']})")
            if d:
                print(f"           title_tokens={d.get('title_tokens_a')} vs {d.get('title_tokens_b')}"
                      f"  title_overlap={d.get('title_overlap')} name_bonus={d.get('name_bonus')}")
        print()


def write_groups_md(groups, out_path):
    lines = ["# Duplicate candidate groups\n"]
    for gi, group in enumerate(groups, 1):
        lines.append(f"## Group {gi}\n")
        for entry in sorted(group, key=lambda e: -e["similarity_score"]):
            d = entry["debug"]
            lines.append(f"- **[{entry['similarity_score']}]** `{entry['full_path']}` — status: {entry['status']}")
            if d:
                lines.append(f"  - title tokens: {d.get('title_tokens_a')} vs {d.get('title_tokens_b')}")
                lines.append(f"  - name/cast tokens: {d.get('name_tokens_a')} vs {d.get('name_tokens_b')}")
                lines.append(f"  - title_overlap={d.get('title_overlap')} title_tsr={d.get('title_tsr')} "
                             f"name_overlap={d.get('name_overlap')} name_bonus={d.get('name_bonus')} "
                             f"primary={d.get('primary')}")
        lines.append("")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_stats_md(files, stats: CorpusStats, out_path, top_n=100):
    title_count = sum(1 for f in files for t in f.tiers if t == "title")
    name_count = sum(1 for f in files for t in f.tiers if t == "name")

    ranked = sorted(stats.df.items(), key=lambda kv: -kv[1])
    ranked_ngrams = sorted(stats.ngram_df.items(), key=lambda kv: -kv[1])

    lines = ["# Corpus token stats\n"]
    lines.append(f"- Files scanned: {stats.n_files}")
    lines.append(f"- Distinct phonetic codes: {len(stats.df)}")
    lines.append(f"- Total tokens classified as **title**-tier: {title_count}")
    lines.append(f"- Total tokens classified as **name**-tier "
                 f"(unigram df >= {NAME_TIER_MIN_DF}, or part of a recurring phrase): {name_count}")
    lines.append("")
    lines.append(f"## Recurring phrases (candidate movie-name / stable-name library)")
    lines.append("")
    lines.append("These are 2+ word sequences that appear together, in the same order, "
                 "in multiple different files — the signature of a movie name or a "
                 "full artist name, as opposed to an ordinary word that just happens "
                 "to recur on its own. Review this list to build a curated library.")
    lines.append("")
    lines.append("| phrase | document frequency |")
    lines.append("|---|---|")
    for gram, df in ranked_ngrams[:top_n]:
        lines.append(f"| {stats.ngram_example.get(gram, '')} | {df} |")
    lines.append("")
    lines.append(f"## Top {top_n} most frequent single tokens (candidate movie/artist/cast library)")
    lines.append("")
    lines.append("| word (example spelling) | phonetic code | document frequency | tier |")
    lines.append("|---|---|---|---|")
    for code, df in ranked[:top_n]:
        tier = stats.tier(code)
        lines.append(f"| {stats.example_word.get(code, '')} | {code} | {df} | {tier} |")

    lines.append("")
    lines.append(f"## Rarest tokens (df == 1) — strongest title-word candidates")
    rare = [c for c, df in stats.df.items() if df == 1]
    lines.append(f"- Count: {len(rare)}")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------
# 11. MAIN
# --------------------------------------------------------------------------

def main():
    global NAME_TIER_MIN_DF
    parser = argparse.ArgumentParser(description="Find duplicate song-video candidates by filename.")
    parser.add_argument("directory", help="Root directory to scan")
    parser.add_argument("--threshold", type=float, default=PAIR_SCORE_THRESHOLD)
    parser.add_argument("--name-min-df", type=int, default=NAME_TIER_MIN_DF,
                         help="df at/above which a token is treated as movie/artist/cast rather than title")
    parser.add_argument("--export-dir", default="/home/shared/git/devopsnextgenx/media-indexer/tmp",
                         help="If set, write groups.md and stats.md into this directory")
    parser.add_argument("--top-tokens", type=int, default=100,
                         help="How many top-frequency tokens to list in stats.md")
    args = parser.parse_args()
    NAME_TIER_MIN_DF = args.name_min_df

    files = scan_directory(args.directory)
    print(f"Scanned {len(files)} video files.\n")

    stats = CorpusStats(files)
    classify_files(files, stats)

    groups = find_duplicate_groups(files, threshold=args.threshold)
    print_groups(groups)

    if args.export_dir:
        os.makedirs(args.export_dir, exist_ok=True)
        write_groups_md(groups, os.path.join(args.export_dir, "groups.md"))
        write_stats_md(files, stats, os.path.join(args.export_dir, "stats.md"), top_n=args.top_tokens)
        print(f"Wrote {args.export_dir}/groups.md and {args.export_dir}/stats.md")


if __name__ == "__main__":
    main()