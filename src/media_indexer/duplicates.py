# media_indexer/duplicates.py
import os
import re
import logging
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
from rapidfuzz import fuzz

from media_indexer.database import mysql_db_instance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phonetic / transliteration folding (Indic-friendly)
# ---------------------------------------------------------------------------
_TRANSLIT = {
    "aa": "a", "ee": "i", "ii": "i", "oo": "u", "uu": "u",
    "kh": "k", "ch": "c", "sh": "s", "th": "t", "ph": "p",
    "dh": "d", "gh": "g", "bh": "b", "jh": "j", "nh": "n",
    "saath": "sath", "zaalima": "zalima", "deewani": "dewani",
    "tumhi": "tum hi", "tumho": "tum ho", "raabta": "rabta",
    "channa": "chana", "mereya": "mereya", "subhanallah": "subhan allah",
}

def _fold(s: str) -> str:
    s = s.lower()
    for k, v in _TRANSLIT.items():
        s = s.replace(k, v)
    # loose vowel folding
    s = s.replace("i", "e").replace("u", "o")
    return s

def _normalize_filename(filename: str) -> str:
    name, _ = os.path.splitext(filename)
    # strip quality / year / common noise
    name = re.sub(
        r"\[[^\]]*?(?:1080p|720p|480p|HD|FHD|4K|HDR|x264|HEVC|AAC|MP3|WEB|DL|BRRip|BluRay|Remux|DVDRip)[^\]]*\]",
        " ", name, flags=re.I
    )
    name = re.sub(r"\([^\)]*?(?:19\d{2}|20\d{2})\)", " ", name)
    name = re.sub(r"\b(?:Official|Video|Song|Music|MV|HD|Full|Movie|Film|Trailer|Teaser|Clip)\b", " ", name, flags=re.I)
    name = re.sub(r"[^a-zA-Z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def _tokenize(text: str) -> List[str]:
    return [t for t in _fold(text).split() if t]

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _token_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return fuzz.ratio(a, b) / 100.0

def _score_pair(tokens_a: List[str], tokens_b: List[str],
                context: set) -> float:
    """
    IDF-weighted core coverage with context exclusion + merge tolerance.
    Returns 0.0 – 1.0.
    """
    if not tokens_a or not tokens_b:
        return 0.0

    # simple document-frequency inside the two titles (very small)
    df = defaultdict(int)
    for t in set(tokens_a) | set(tokens_b):
        df[t] += 1

    def is_core(t: str) -> bool:
        return t not in context and df[t] <= 1   # unique to one side → core

    core_a = [t for t in tokens_a if is_core(t)]
    core_b = [t for t in tokens_b if is_core(t)]

    if not core_a or not core_b:
        # fall back to full token_set_ratio when everything is context
        return fuzz.token_set_ratio(" ".join(tokens_a), " ".join(tokens_b)) / 100.0

    # greedy best-match coverage
    used = set()
    matched = 0.0
    for ta in core_a:
        best = 0.0
        best_j = -1
        for j, tb in enumerate(core_b):
            if j in used:
                continue
            s = _token_sim(ta, tb)
            # allow 2-token merges (tum hi ↔ tumhi)
            if len(ta) > 3 and len(tb) > 3:
                s = max(s, _token_sim(ta, tb[:len(ta)]), _token_sim(ta, tb[-len(ta):]))
            if s > best:
                best = s
                best_j = j
        if best >= 0.78 and best_j >= 0:
            used.add(best_j)
            matched += best

    coverage = matched / max(len(core_a), len(core_b))
    # identity gate: need at least one solid long match or two medium matches
    if matched < 0.9 and len(core_a) + len(core_b) > 2:
        if matched < 1.4:
            coverage *= 0.7
    return min(1.0, coverage)

# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
class DuplicateDetector:
    def __init__(self, qdrant_client, collection_name: str,
                 similarity_threshold: float = 0.78,
                 media_type: str = "auto"):
        self.qdrant = qdrant_client          # kept for API compatibility
        self.collection_name = collection_name
        self.similarity_threshold = similarity_threshold
        self.media_type = media_type

    def detect_for_mount(self, mount_name: str, mount_path: str):
        files_map = mysql_db_instance.get_tracked_files_map(mount_name)
        if not files_map:
            logger.info(f"No files found for mount {mount_name}")
            return

        folders: Dict[str, List[str]] = defaultdict(list)
        for rel_path in files_map:
            if self.media_type == "songs":
                folder = rel_path.split("/")[0] if "/" in rel_path else ""
            else:
                folder = os.path.dirname(rel_path)
            if folder:
                folders[folder].append(rel_path)

        logger.info(f"Detecting duplicates for {mount_name} across {len(folders)} folders")
        for folder, rel_paths in folders.items():
            self._process_folder(mount_name, mount_path, folder, rel_paths, files_map)

    def _process_folder(self, mount_name: str, mount_path: str, folder: str,
                        rel_paths: List[str], files_map: Dict[str, Dict]):
        if len(rel_paths) < 2:
            return

        # prepare tokens + context (folder name tokens are context)
        context = set(_tokenize(folder))
        file_tokens: Dict[str, List[str]] = {}
        for rel in rel_paths:
            cleaned = _normalize_filename(os.path.basename(rel))
            file_tokens[rel] = _tokenize(cleaned)

        # greedy clustering
        visited = set()
        clusters = []
        rel_list = list(file_tokens.keys())

        for i, ri in enumerate(rel_list):
            if i in visited:
                continue
            cluster = [ri]
            visited.add(i)
            for j in range(i + 1, len(rel_list)):
                if j in visited:
                    continue
                score = _score_pair(file_tokens[ri], file_tokens[rel_list[j]], context)
                if score >= self.similarity_threshold:
                    cluster.append(rel_list[j])
                    visited.add(j)
            if len(cluster) > 1:          # only real groups
                clusters.append(cluster)

        if not clusters:
            return

        group_key = os.path.join(mount_path, folder).replace("\\", "/")
        mysql_db_instance.delete_duplicate_groups_by_group_key(group_key)

        for cluster in clusters:
            canonical_rel = cluster[0]
            canonical_full = os.path.join(mount_path, canonical_rel).replace("\\", "/")
            canonical_rec = files_map[canonical_rel]

            # insert canonical (score 1.0)
            mysql_db_instance.insert_duplicate_group(
                group_key=group_key,
                file_path=canonical_full,
                file_name=os.path.basename(canonical_rel),
                mount=mount_name,
                vector_id=canonical_rec.get("vector_id"),
                similarity_score=1.0,
                canonical_file_path=canonical_full,
                status="PENDING_REVIEW",
            )

            for dup_rel in cluster[1:]:
                dup_full = os.path.join(mount_path, dup_rel).replace("\\", "/")
                dup_rec = files_map[dup_rel]
                sim = _score_pair(file_tokens[canonical_rel], file_tokens[dup_rel], context)
                mysql_db_instance.insert_duplicate_group(
                    group_key=group_key,
                    file_path=dup_full,
                    file_name=os.path.basename(dup_rel),
                    mount=mount_name,
                    vector_id=dup_rec.get("vector_id"),
                    similarity_score=float(sim),
                    canonical_file_path=canonical_full,
                    status="PENDING_REVIEW",
                )

        logger.info(f"Folder {folder}: {len(clusters)} duplicate group(s)")