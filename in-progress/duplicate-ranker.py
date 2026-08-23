#!/usr/bin/env python3
"""
duplicate_finder.py
====================

Detect duplicate-candidate video/song files in a media library based on
filename similarity, tailored to Indian-movie songs collected from YouTube
(Bollywood / Hindi / Punjabi film songs, mostly).

WHY THIS IS HARD FOR THIS COLLECTION
-------------------------------------
A typical filename crams together up to four kinds of information, in no
fixed order, with fields optional:

    <song title> <movie name> <cast / actor names> <noise: quality, tags,
    channel/production house, "official video", "exclusive song", etc.>

e.g.
    "Nakhralo - Qayamat Hi Qayamat Exclusive Song.mp4"
       title="Nakhralo"  movie="Qayamat Hi Qayamat"  noise="Exclusive Song"

    "Oye Oye Tirchhi Topi Wale | Tridev (1989) | Naseeruddin Shah, Sonam |
     Amit Kumar, Sapna Mukherjee.mp4"
       title="Oye Oye Tirchhi Topi Wale"  movie="Tridev"  year=1989
       cast="Naseeruddin Shah, Sonam"  singers="Amit Kumar, Sapna Mukherjee"

Two files are the SAME song (a duplicate pair to review) when their TITLE
matches, even if the rest of the filename (movie name, cast list, upload
channel) differs wildly in wording/order/presence. Two files are NOT
duplicates just because they share a movie name and cast list -- that
happens for every song in the same film (the single biggest source of
false positives in a library like this).

This program therefore explicitly classifies every token of a filename
into one of four tiers before scoring a pair:

    TITLE  - song-specific words (this is what must match for a dup)
    MOVIE  - the film name (recurs across every song from that film)
    ARTIST - actor/actress/singer/cast names (also recurs across a film,
             and additionally recurs across a person's folder of songs)
    NOISE  - quality/format tags, upload boilerplate, channel/production
             house branding (T-Series, Zee Music, "Official Video", ...)

Classification signals used (in order of how much they're trusted):
  1. A curated static noise/production-house list + regex patterns
     (channel branding, quality tags) -- stripped before tokenizing.
  2. The file's own PARENT FOLDER name. In this library, folders are named
     after the lead actress/artist (.../hindi/hd/<Artist>/file.mp4), so any
     token shared with the folder name is a strong, free ARTIST signal.
  3. A curated list of common Bollywood/Indian-film actor, actress and
     playback-singer name fragments (surnames + common first names) --
     used as an ARTIST hint regardless of how often it recurs.
  4. Corpus-wide document frequency (df) of each token's phonetic code:
     a token appearing in many DIFFERENT files across the whole library is
     almost certainly a MOVIE name or cast/artist name (every song from
     the same film repeats them); a token appearing in only one or two
     files is much more likely to be a song-specific TITLE word. This is
     classic IDF reasoning applied to filename tokens.
  5. Recurring multi-word phrases (n-grams, 2-3 tokens, same order, across
     >=2 files): a strong signature of a MOVIE name ("Zara Hatke Zara
     Bachke") or a stable full artist name, since individual words in a
     movie title are often ordinary and wouldn't stand out on their own.
     Phrases that survive #2/#3 (i.e. contain no artist-hinted token) and
     recur are promoted to MOVIE tier; the rest fall back to the
     unigram df test above.

Everything left over after 1-3 is scored with the df test (df >= threshold
-> MOVIE-or-ARTIST "name" tier, collapsed for scoring purposes into a
single NAME overlap bonus that can only ever be a small addition -- never
enough on its own to call two files duplicates), and TITLE tier tokens
are what's actually gated on. See pair_score() for full mechanics.

DEPENDENCIES
    pip install rapidfuzz jellyfish pymysql pyyaml

USAGE
    # dry run over a real directory tree, report only
    python3 duplicate_finder.py /media/zbox/Crucial-X6/ShareMe/media/songs --no-db

    # same, but also read/write MySQL per config.yaml
    python3 duplicate_finder.py /path/to/library --config config.yaml

    # test against a flat list of paths (e.g. the sample file), no filesystem walk
    python3 duplicate_finder.py --paths-file filename-sample.txt --no-db

    # first-time setup: create database + app user from the `root_pwd` in
    # config.yaml, then create tables
    python3 duplicate_finder.py --init-db --config config.yaml
"""

import os
import re
import sys
import time
import math
import json
import hashlib
import argparse
import itertools
from datetime import datetime, timezone
from dataclasses import dataclass, field
from collections import defaultdict, Counter

try:
    import jellyfish
except ImportError:
    sys.exit("Missing dependency: pip install jellyfish")
try:
    from rapidfuzz import fuzz
except ImportError:
    sys.exit("Missing dependency: pip install rapidfuzz")


# ==========================================================================
# 0. PROGRESS REPORTING
# ==========================================================================
# Every independent stage/loop in this program (directory scan, tokenizing,
# corpus-stat scanning, tiering, candidate-pair blocking, pairwise scoring,
# database writes) reports live progress through this one small helper, so
# a person watching the console (or piping stdout to a log/monitor) always
# knows what step is running and how far it's gotten. Turn it off with
# --quiet for cron/unattended runs.

PROGRESS_ENABLED = True   # set from --quiet in main(); read by helpers below


def step(message: str):
    """Announce the start of a new top-level stage."""
    if PROGRESS_ENABLED:
        print(f"\n==> {message}", flush=True)


class Progress:
    """In-place progress meter for a loop of known (or estimated) length.

    Usage:
        p = Progress(len(items), "Scoring pairs")
        for item in items:
            ... work ...
            p.update()
        p.done()

    Safe to use even when PROGRESS_ENABLED is False (becomes a no-op), and
    tolerates total=0 (treated as an indeterminate counter that just prints
    how many items it has processed so far).
    """

    def __init__(self, total: int, label: str, every: int = None):
        self.total = total
        self.label = label
        self.count = 0
        self.start = time.time()
        # Re-render at most ~200 times over the run, never less than every
        # single item for small loops.
        self.every = every or max(1, (total or 1) // 200)
        if PROGRESS_ENABLED:
            self._render(force=True)

    def _render(self, force: bool = False):
        if not PROGRESS_ENABLED:
            return
        if not force and self.count % self.every != 0 and self.count != self.total:
            return
        elapsed = time.time() - self.start
        if self.total:
            pct = min(100, int(self.count * 100 / self.total))
            msg = f"\r    {self.label}: {self.count}/{self.total} ({pct}%) [{elapsed:0.1f}s]"
        else:
            msg = f"\r    {self.label}: {self.count} [{elapsed:0.1f}s]"
        print(msg.ljust(90), end="", flush=True)

    def update(self, n: int = 1):
        self.count += n
        self._render()

    def done(self):
        self.count = self.total or self.count
        if PROGRESS_ENABLED:
            elapsed = time.time() - self.start
            total_str = self.total or self.count
            print(f"\r    {self.label}: {total_str}/{total_str} (100%) "
                  f"done in {elapsed:0.1f}s".ljust(90))


# ==========================================================================
# 1. CONFIG -- tune / extend these for your collection
# ==========================================================================

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v"}

# --- 1a. NOISE: quality/format tags + generic upload boilerplate ---------
NOISE_PATTERNS = [
    r"\b\d{3,4}p\b",                         # 1080p, 720p, 480p
    r"\b[48]k\b",                            # 4k, 8k
    r"\bfull\s*hd\b", r"\bhd\b", r"\buhd\b",
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

# Upload channel / production-house / label branding -- common on Indian
# film-song uploads. These are pure noise for duplicate-title matching:
# they recur across totally unrelated songs and would otherwise pollute
# both the "name" tier and (worse) look like a fake title match.
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

# Delimiters that separate fields/words in these filenames (incl. the
# fullwidth vertical bar "｜" seen from some mobile-uploaded titles).
_SPLIT_RE = re.compile(r"[|｜_\-\(\)\[\]{}.,:;/\\!?~]+")

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

MIN_TOKEN_LEN = 2
TOKEN_FUZZY_MATCH_THRESHOLD = 82
PAIR_SCORE_THRESHOLD = 72

# --- 1b. ARTIST hints -----------------------------------------------------
# Folder name is treated as a strong artist signal (library layout is
# .../<language>/<quality>/<Artist Name>/<file>).
USE_FOLDER_NAME_AS_ARTIST_HINT = True

# Curated fragments (lowercase) commonly appearing in Bollywood/Indian-film
# actor, actress, director, and playback-singer names. Deliberately a
# "hint" list, not exhaustive -- extend as you review stats.md/report
# output for your own library. Used to bias a token to ARTIST tier
# regardless of its corpus df.
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

# --- 1c. corpus-frequency / tiering thresholds ----------------------------
# A phonetic code appearing in >= this many DISTINCT files is treated as a
# recurring "name" (movie/artist/cast) token rather than a title word.
NAME_TIER_MIN_DF = 5

TITLE_OVERLAP_WEIGHT = 0.80
TITLE_TOKEN_SET_RATIO_WEIGHT = 0.20
NAME_OVERLAP_BONUS_CAP = 8.0
MIN_TITLE_TOKENS_FOR_CONFIDENT_MATCH = 1
MIN_MATCHED_TITLE_TOKENS_ABS = 2

NGRAM_MIN_DF = 2
NGRAM_SIZES = (2, 3)

UNMATCHED_TOLERANCE_ABS = 1
MIN_SHORTER_MATCH_FRACTION = 0.6

# Confidence bands applied to the final gated score of the BEST pairing a
# file participates in within its group.
CONFIDENCE_HIGH_MIN = 85
CONFIDENCE_MEDIUM_MIN = PAIR_SCORE_THRESHOLD


# ==========================================================================
# 2. CLEAN + TOKENIZE
# ==========================================================================

def clean_and_tokenize(filename: str):
    """Strip extension + noise, split into normalized word tokens."""
    name = os.path.splitext(filename)[0]
    # Replace delimiters (incl. _) with spaces FIRST so noise-pattern \b
    # boundaries work correctly.
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


# ==========================================================================
# 3. PHONETIC NORMALIZATION
# ==========================================================================

def phonetic_code(word: str) -> str:
    """Metaphone collapses common transliteration spelling variants
    (love/luv, night/nite, zindagi/jindagi -> similar consonant skeletons)."""
    try:
        code = jellyfish.metaphone(word)
        return code if code else word
    except Exception:
        return word


def tokens_match(a: str, b: str, code_a: str, code_b: str) -> bool:
    if code_a == code_b:
        return True
    return fuzz.ratio(a, b) >= TOKEN_FUZZY_MATCH_THRESHOLD


# ==========================================================================
# 4. FILE RECORD
# ==========================================================================

@dataclass
class SongFile:
    file_id: int
    full_path: str
    tokens: list = field(default_factory=list)
    codes: list = field(default_factory=list)
    # parallel to tokens: "title" | "movie" | "artist"
    tiers: list = field(default_factory=list)
    folder_tokens: set = field(default_factory=set)

    def tier_tokens(self, tier):
        return [(t, c) for t, c, tr in zip(self.tokens, self.codes, self.tiers) if tr == tier]

    def tokens_str(self, tier):
        return ", ".join(t for t, _ in self.tier_tokens(tier))


def build_song_file(file_id: int, full_path: str) -> SongFile:
    filename = os.path.basename(full_path)
    parent_folder = os.path.basename(os.path.dirname(full_path))
    tokens = clean_and_tokenize(filename)
    codes = [phonetic_code(t) for t in tokens]
    folder_tokens = set(clean_and_tokenize(parent_folder))
    return SongFile(file_id=file_id, full_path=full_path, tokens=tokens,
                     codes=codes, folder_tokens=folder_tokens)


# ==========================================================================
# 5. CORPUS STATS + TIER CLASSIFICATION (title / movie / artist)
# ==========================================================================

class CorpusStats:
    """Document-frequency stats over phonetic codes AND recurring n-gram
    phrases across the whole collection -- used to tell recurring MOVIE /
    ARTIST tokens apart from song-specific TITLE tokens."""

    def __init__(self, files):
        self.n_files = len(files)
        code_to_file_ids = defaultdict(set)
        code_example = {}
        ngram_to_file_ids = defaultdict(set)
        ngram_example = {}

        prog = Progress(len(files), "Building corpus frequency stats")
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
            prog.update()
        prog.done()

        self.df = {code: len(ids) for code, ids in code_to_file_ids.items()}
        self.example_word = code_example

        self.ngram_df = {gram: len(ids) for gram, ids in ngram_to_file_ids.items()
                          if len(ids) >= NGRAM_MIN_DF}
        self.ngram_example = ngram_example

        # Phrases where NO member word is an artist-hint -> movie-name
        # candidates (reporting + classification aid).
        self.movie_phrase_words = set()
        for gram, df in self.ngram_df.items():
            words = ngram_example[gram].split()
            if not any(w in ARTIST_NAME_HINTS for w in words):
                self.movie_phrase_words.update(words)

    def is_recurring(self, code: str) -> bool:
        return self.df.get(code, 0) >= NAME_TIER_MIN_DF


def classify_files(files, stats: CorpusStats):
    """Assign a per-token tier ("title" | "movie" | "artist") to every
    file, in place."""
    prog = Progress(len(files), "Classifying tokens (title/movie/artist)")
    for f in files:
        tiers = []
        for tok, code in zip(f.tokens, f.codes):
            if tok in ARTIST_NAME_HINTS:
                tiers.append("artist")
            elif USE_FOLDER_NAME_AS_ARTIST_HINT and tok in f.folder_tokens:
                tiers.append("artist")
            elif stats.is_recurring(code):
                tiers.append("movie" if tok in stats.movie_phrase_words else "artist")
            else:
                tiers.append("title")
        f.tiers = tiers
        prog.update()
    prog.done()


# ==========================================================================
# 6. PAIRWISE SCORING (gated on title tier)
# ==========================================================================

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


def _overlap_pct(tokens_codes_a, tokens_codes_b):
    matched, shorter_len = _greedy_overlap(tokens_codes_a, tokens_codes_b)
    return round((matched / shorter_len) * 100, 1) if shorter_len else 0.0


def pair_score(f1: SongFile, f2: SongFile):
    """Returns (final_score, title_score, movie_score, artist_score, debug_dict)."""
    title_a, title_b = f1.tier_tokens("title"), f2.tier_tokens("title")
    movie_a, movie_b = f1.tier_tokens("movie"), f2.tier_tokens("movie")
    artist_a, artist_b = f1.tier_tokens("artist"), f2.tier_tokens("artist")
    name_a, name_b = movie_a + artist_a, movie_b + artist_b  # combined, for the gating bonus

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

    # HARD GATE: absolute count of the shorter file's tokens left totally
    # unmatched (any tier) -- what actually distinguishes "same movie,
    # different song" from a true duplicate.
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


# ==========================================================================
# 7. BLOCKING (avoid O(n^2) over large collections)
# ==========================================================================

def build_candidate_pairs(files):
    index = defaultdict(set)
    prog = Progress(len(files), "Indexing tokens for pair blocking")
    for f in files:
        for code in set(f.codes):
            index[code].add(f.file_id)
        prog.update()
    prog.done()

    max_bucket = max(20, int(len(files) * 0.05))
    pairs = set()
    prog = Progress(len(index), "Generating candidate pairs")
    for code, ids in index.items():
        if len(ids) >= 2 and len(ids) <= max_bucket:
            for a, b in itertools.combinations(sorted(ids), 2):
                pairs.add((a, b))
        prog.update()
    prog.done()
    return pairs


# ==========================================================================
# 8. UNION-FIND GROUPING
# ==========================================================================

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


def _slugify(text: str, max_len: int = 48) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len] or "group"


def _group_id_for(members, by_id):
    """Derive a stable, unique-enough group id from the representative
    file's title tokens (the member with the most title tokens), plus a
    short hash of the sorted file_ids so re-runs of the same underlying
    group stay stable while different groups never collide."""
    rep = max(members, key=lambda m: len(by_id[m].tier_tokens("title")))
    title_words = [t for t, _ in by_id[rep].tier_tokens("title")]
    base = _slugify(" ".join(title_words)) if title_words else _slugify(
        os.path.splitext(os.path.basename(by_id[rep].full_path))[0])
    digest = hashlib.sha1(",".join(str(m) for m in sorted(members)).encode()).hexdigest()[:8]
    return f"{base}-{digest}"


def find_duplicate_groups(files, threshold: float = PAIR_SCORE_THRESHOLD):
    by_id = {f.file_id: f for f in files}
    uf = UnionFind(by_id.keys())
    pair_data = {}  # (a, b) -> (final, title_score, movie_score, artist_score, debug)

    candidate_pairs = build_candidate_pairs(files)
    prog = Progress(len(candidate_pairs), "Scoring candidate pairs")
    for a, b in candidate_pairs:
        result = pair_score(by_id[a], by_id[b])
        if result[0] >= threshold:
            pair_data[(a, b)] = result
            uf.union(a, b)
        prog.update()
    prog.done()

    step("Grouping matched pairs into duplicate clusters")
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
                "file_id": m, "full_path": by_id[m].full_path,
                "overall_score": round(final, 1), "title_score": title_score,
                "movie_score": movie_score, "artist_score": artist_score,
                "confidence": confidence_for(final), "status": "PENDING",
                "debug": debug,
            })
        groups.append({"group_id": group_id, "members": entries})
    return groups


# ==========================================================================
# 9. SCAN
# ==========================================================================

def scan_directory(root_dir: str):
    step(f"Discovering video files under {root_dir}")
    candidates = []
    dirs_seen = 0
    for dirpath, _, filenames in os.walk(root_dir):
        dirs_seen += 1
        if PROGRESS_ENABLED and dirs_seen % 25 == 0:
            print(f"\r    Walked {dirs_seen} folders, found {len(candidates)} video files so far..."
                  .ljust(90), end="", flush=True)
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTENSIONS:
                candidates.append(os.path.join(dirpath, fn))
    if PROGRESS_ENABLED:
        print(f"\r    Walked {dirs_seen} folders, found {len(candidates)} video files.".ljust(90))

    step("Tokenizing filenames")
    files = []
    prog = Progress(len(candidates), "Tokenizing")
    for fid, full_path in enumerate(candidates):
        files.append(build_song_file(fid, full_path))
        prog.update()
    prog.done()
    return files


def load_paths_file(path: str):
    step(f"Reading path list from {path}")
    with open(path, "r", encoding="utf-8") as fh:
        paths = [line.strip() for line in fh if line.strip()]

    step("Tokenizing filenames")
    files = []
    prog = Progress(len(paths), "Tokenizing")
    for fid, p in enumerate(paths):
        files.append(build_song_file(fid, p))
        prog.update()
    prog.done()
    return files


# ==========================================================================
# 10. REPORTING -- single duplicate-report.md (console summary too)
# ==========================================================================

def print_groups(groups):
    print(f"Found {len(groups)} duplicate-candidate group(s):\n")
    for gi, group in enumerate(groups, 1):
        print(f"--- Group {gi} ({group['group_id']}) ---")
        for entry in sorted(group["members"], key=lambda e: -e["overall_score"]):
            print(f"  [{entry['overall_score']:>5} | {entry['confidence']:<6}] {entry['full_path']}")
        print()


def write_report_md(groups, files, stats: CorpusStats, out_path, top_n=100):
    title_count = sum(1 for f in files for t in f.tiers if t == "title")
    movie_count = sum(1 for f in files for t in f.tiers if t == "movie")
    artist_count = sum(1 for f in files for t in f.tiers if t == "artist")

    lines = ["# Duplicate Song Report", ""]
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Files scanned: **{len(files)}**")
    lines.append(f"- Duplicate-candidate groups: **{len(groups)}**")
    lines.append(f"- Files flagged as part of a group: "
                 f"**{sum(len(g['members']) for g in groups)}**")
    lines.append(f"- Tokens classified -- title: {title_count}, movie: {movie_count}, "
                 f"artist: {artist_count}")
    lines.append("")

    lines.append("## Duplicate Groups")
    lines.append("")
    for gi, group in enumerate(groups, 1):
        lines.append(f"### Group {gi} -- `{group['group_id']}`")
        lines.append("")
        lines.append("| Score | Title | Movie | Artist | Confidence | Status | Path |")
        lines.append("|---|---|---|---|---|---|---|")
        for entry in sorted(group["members"], key=lambda e: -e["overall_score"]):
            lines.append(f"| {entry['overall_score']} | {entry['title_score']} | "
                         f"{entry['movie_score']} | {entry['artist_score']} | "
                         f"{entry['confidence']} | {entry['status']} | `{entry['full_path']}` |")
        lines.append("")

    lines.append("## Corpus Token Stats")
    lines.append("")
    lines.append(f"- Distinct phonetic codes: {len(stats.df)}")
    lines.append(f"- Recurring-token threshold (name-tier min df): {NAME_TIER_MIN_DF}")
    lines.append("")
    ranked = sorted(stats.df.items(), key=lambda kv: -kv[1])[:top_n]
    lines.append(f"### Top {top_n} most frequent tokens (movie/artist candidates)")
    lines.append("")
    lines.append("| word (example) | phonetic code | document frequency |")
    lines.append("|---|---|---|")
    for code, df in ranked:
        lines.append(f"| {stats.example_word.get(code, '')} | {code} | {df} |")
    lines.append("")

    ranked_ngrams = sorted(stats.ngram_df.items(), key=lambda kv: -kv[1])[:top_n]
    lines.append(f"### Recurring phrases (candidate movie-name library)")
    lines.append("")
    lines.append("| phrase | document frequency |")
    lines.append("|---|---|")
    for gram, df in ranked_ngrams:
        lines.append(f"| {stats.ngram_example.get(gram, '')} | {df} |")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ==========================================================================
# 11. MYSQL PERSISTENCE
# ==========================================================================

DDL = {
    "media_files": """
        CREATE TABLE IF NOT EXISTS media_files (
            file_id       INT PRIMARY KEY,
            full_path     VARCHAR(1024) NOT NULL,
            filename      VARCHAR(512),
            parent_folder VARCHAR(255),
            title_tokens  TEXT,
            movie_tokens  TEXT,
            artist_tokens TEXT,
            scanned_at    DATETIME,
            UNIQUE KEY uq_media_files_path (full_path(768))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "duplicate_groups": """
        CREATE TABLE IF NOT EXISTS duplicate_groups (
            group_id      VARCHAR(80) PRIMARY KEY,
            title_key     VARCHAR(512),
            member_count  INT,
            created_at    DATETIME,
            updated_at    DATETIME
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "duplicate_group_candidates": """
        CREATE TABLE IF NOT EXISTS duplicate_group_candidates (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            group_id       VARCHAR(80) NOT NULL,
            file_id        INT NOT NULL,
            full_path      VARCHAR(1024),
            title_score    DECIMAL(5,1),
            movie_score    DECIMAL(5,1),
            artist_score   DECIMAL(5,1),
            overall_score  DECIMAL(5,1),
            confidence     ENUM('HIGH','MEDIUM','LOW') DEFAULT 'LOW',
            status         ENUM('PENDING','DUPLICATE','REJECTED') DEFAULT 'PENDING',
            stats_json     JSON,
            created_at     DATETIME,
            updated_at     DATETIME,
            UNIQUE KEY uq_group_file (group_id, file_id),
            KEY idx_status (status),
            CONSTRAINT fk_dgc_group FOREIGN KEY (group_id)
                REFERENCES duplicate_groups(group_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "token_stats": """
        CREATE TABLE IF NOT EXISTS token_stats (
            phonetic_code  VARCHAR(32) PRIMARY KEY,
            example_word   VARCHAR(255),
            tier           ENUM('title','movie','artist') DEFAULT 'title',
            doc_frequency  INT,
            updated_at     DATETIME
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}


def load_db_config(config_path: str) -> dict:
    import yaml
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("mysql", {})


def init_database(db_cfg: dict):
    """One-time setup: using root credentials, create the database and app
    user/grants if they don't already exist. Safe to re-run."""
    import pymysql
    conn = pymysql.connect(host=db_cfg["host"], port=int(db_cfg.get("port", 3306)),
                            user="root", password=db_cfg["root_pwd"], autocommit=True)
    try:
        with conn.cursor() as cur:
            db = db_cfg["database"]
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s",
                        (db_cfg["user"], db_cfg["password"]))
            cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO %s@'%%'", (db_cfg["user"],))
            cur.execute("FLUSH PRIVILEGES")
        print(f"Database '{db_cfg['database']}' and user '{db_cfg['user']}' ready.")
    finally:
        conn.close()

    conn = connect_app(db_cfg)
    try:
        with conn.cursor() as cur:
            for name, ddl in DDL.items():
                cur.execute(ddl)
        conn.commit()
        print("Tables created/verified:", ", ".join(DDL.keys()))
    finally:
        conn.close()


def connect_app(db_cfg: dict):
    import pymysql
    return pymysql.connect(host=db_cfg["host"], port=int(db_cfg.get("port", 3306)),
                            user=db_cfg["user"], password=db_cfg["password"],
                            database=db_cfg["database"], autocommit=False,
                            cursorclass=pymysql.cursors.Cursor)


def ensure_tables(conn):
    with conn.cursor() as cur:
        for ddl in DDL.values():
            cur.execute(ddl)
    conn.commit()


def persist_to_db(conn, files, groups, stats: CorpusStats):
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        prog = Progress(len(files), "Writing media_files")
        for f in files:
            cur.execute(
                """INSERT INTO media_files
                       (file_id, full_path, filename, parent_folder,
                        title_tokens, movie_tokens, artist_tokens, scanned_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                       filename=VALUES(filename), parent_folder=VALUES(parent_folder),
                       title_tokens=VALUES(title_tokens), movie_tokens=VALUES(movie_tokens),
                       artist_tokens=VALUES(artist_tokens), scanned_at=VALUES(scanned_at)""",
                (f.file_id, f.full_path, os.path.basename(f.full_path),
                 os.path.basename(os.path.dirname(f.full_path)),
                 f.tokens_str("title"), f.tokens_str("movie"), f.tokens_str("artist"), now),
            )
            prog.update()
        prog.done()

        prog = Progress(len(stats.df), "Writing token_stats")
        for code, df in stats.df.items():
            tier = "title"
            if any(stats.example_word.get(code) in ARTIST_NAME_HINTS for _ in [0]):
                tier = "artist"
            elif df >= NAME_TIER_MIN_DF:
                tier = "movie" if stats.example_word.get(code) in stats.movie_phrase_words else "artist"
            cur.execute(
                """INSERT INTO token_stats (phonetic_code, example_word, tier, doc_frequency, updated_at)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE example_word=VALUES(example_word), tier=VALUES(tier),
                       doc_frequency=VALUES(doc_frequency), updated_at=VALUES(updated_at)""",
                (code, stats.example_word.get(code, ""), tier, df, now),
            )
            prog.update()
        prog.done()

        prog = Progress(len(groups), "Writing duplicate_groups + candidates")
        for group in groups:
            gid = group["group_id"]
            rep_title = ""
            for entry in group["members"]:
                if entry["debug"].get("title_tokens_a"):
                    rep_title = " ".join(entry["debug"]["title_tokens_a"])
                    break
            cur.execute(
                """INSERT INTO duplicate_groups (group_id, title_key, member_count, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE title_key=VALUES(title_key),
                       member_count=VALUES(member_count), updated_at=VALUES(updated_at)""",
                (gid, rep_title, len(group["members"]), now, now),
            )
            for entry in group["members"]:
                cur.execute(
                    """INSERT INTO duplicate_group_candidates
                           (group_id, file_id, full_path, title_score, movie_score,
                            artist_score, overall_score, confidence, status, stats_json,
                            created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                           title_score=VALUES(title_score), movie_score=VALUES(movie_score),
                           artist_score=VALUES(artist_score), overall_score=VALUES(overall_score),
                           confidence=VALUES(confidence), stats_json=VALUES(stats_json),
                           updated_at=VALUES(updated_at)""",
                    (gid, entry["file_id"], entry["full_path"], entry["title_score"],
                     entry["movie_score"], entry["artist_score"], entry["overall_score"],
                     entry["confidence"], entry["status"], json.dumps(entry["debug"]),
                     now, now),
                )
            prog.update()
        prog.done()
    conn.commit()


# ==========================================================================
# 12. MAIN
# ==========================================================================

def main():
    global NAME_TIER_MIN_DF
    parser = argparse.ArgumentParser(description="Find duplicate song-video candidates by filename.")
    parser.add_argument("directory", nargs="?", help="Root directory to scan")
    parser.add_argument("--paths-file", help="Text file of full paths, one per line "
                         "(alternative to scanning a real directory)")
    parser.add_argument("--threshold", type=float, default=PAIR_SCORE_THRESHOLD)
    parser.add_argument("--name-min-df", type=int, default=NAME_TIER_MIN_DF)
    parser.add_argument("--export-dir", default=".",
                         help="Directory to write duplicate-report.md into")
    parser.add_argument("--top-tokens", type=int, default=100)
    parser.add_argument("--config", default="config.yaml", help="YAML config with mysql: section")
    parser.add_argument("--no-db", action="store_true", help="Skip all database operations")
    parser.add_argument("--init-db", action="store_true",
                         help="Create database/user/tables (needs root_pwd in config), then exit")
    parser.add_argument("--quiet", action="store_true",
                         help="Suppress step banners and progress meters (for cron/unattended runs)")
    args = parser.parse_args()
    NAME_TIER_MIN_DF = args.name_min_df

    global PROGRESS_ENABLED
    PROGRESS_ENABLED = not args.quiet

    if args.init_db:
        db_cfg = load_db_config(args.config)
        if not db_cfg.get("enabled", True):
            sys.exit("mysql.enabled is false in config; nothing to init.")
        init_database(db_cfg)
        return

    if not args.directory and not args.paths_file:
        parser.error("Provide either a directory to scan or --paths-file")

    run_start = time.time()
    files = load_paths_file(args.paths_file) if args.paths_file else scan_directory(args.directory)
    if not files:
        print("No video files found.")
        return

    step(f"Computing corpus statistics over {len(files)} files")
    stats = CorpusStats(files)

    step("Classifying tokens")
    classify_files(files, stats)

    step("Finding duplicate groups")
    groups = find_duplicate_groups(files, threshold=args.threshold)
    print()
    print_groups(groups)

    step("Writing report")
    os.makedirs(args.export_dir, exist_ok=True)
    report_path = os.path.join(args.export_dir, "duplicate-report.md")
    write_report_md(groups, files, stats, report_path, top_n=args.top_tokens)
    print(f"Wrote {report_path}")

    if not args.no_db:
        try:
            db_cfg = load_db_config(args.config)
        except FileNotFoundError:
            print(f"No config file at {args.config}; skipping database write "
                  f"(pass --no-db to silence this).")
            db_cfg = {}
        if db_cfg.get("enabled"):
            step("Persisting results to MySQL")
            try:
                conn = connect_app(db_cfg)
                ensure_tables(conn)
                persist_to_db(conn, files, groups, stats)
                conn.close()
                print(f"Persisted {len(files)} files and {len(groups)} groups to "
                      f"{db_cfg['database']}@{db_cfg['host']}.")
            except Exception as e:
                print(f"Database write skipped (error: {e})")
        elif db_cfg:
            print("mysql.enabled is false in config; skipping database write.")

    step(f"Done in {time.time() - run_start:0.1f}s total")


if __name__ == "__main__":
    main()