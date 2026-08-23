import os
import re
import logging
from typing import Dict, List, Any, Optional
from rapidfuzz import fuzz

from media_indexer.database import mysql_db_instance

logger = logging.getLogger(__name__)

class DuplicateDetector:
    def __init__(self, qdrant_client, collection_name: str,
                 similarity_threshold: float = 0.85,
                 media_type: str = "auto"):
        """
        Detects duplicate media files within the same folder using filename-based
        similarity and folder context. The Qdrant client is kept for compatibility
        but is not used in this implementation.
        """
        self.qdrant = qdrant_client
        self.collection_name = collection_name
        self.similarity_threshold = similarity_threshold
        self.media_type = media_type
        self._normalized_cache: Dict[str, str] = {}

    # ---- Helper functions for filename normalisation ----
    def _normalize_filename(self, filename: str) -> str:
        """
        Strip quality tags, codecs, year, common words, and extra spaces.
        Also folds common Indic transliteration variants.
        """
        if filename in self._normalized_cache:
            return self._normalized_cache[filename]

        # Remove extension
        name, _ = os.path.splitext(filename)

        # Remove bracketed content that contains typical quality/year patterns
        # e.g., [1080p], (2015), [HD], [x264], etc.
        patterns = [
            r'\[[^\]]*?(?:1080p|720p|480p|HD|FHD|4K|HDR|x264|HEVC|AAC|MP3|WEB|DL|BRRip|BluRay|Remux|DVDRip)[^\]]*\]',
            r'\([^\)]*?(?:19\d{2}|20\d{2})\)',   # year in parentheses
            r'\[[^\)]*?(?:19\d{2}|20\d{2})\]',   # year in brackets
            r'\b(?:Official|Video|Song|Music|MV|HD|Full|Movie|Film|Trailer|Teaser|Clip)\b',
            r'\b(?:1080p|720p|480p|HD|FHD|4K|HDR|x264|HEVC|AAC|MP3)\b',
            r'\s+-\s+'  # separators like " - "
        ]
        cleaned = name
        for pat in patterns:
            cleaned = re.sub(pat, ' ', cleaned, flags=re.IGNORECASE)

        # Remove extra spaces and special characters (keep letters, numbers and spaces)
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        # Fold common Indic transliteration variants
        # This is a small dictionary; could be extended.
        translit_map = {
            'saath': 'sath', 'zaalima': 'zalima', 'deewani': 'dewani',
            'tumhi': 'tum hi', 'tum ho': 'tumhi',   # example of joining/splitting
        }
        # Tokenise and apply mapping to each token
        tokens = cleaned.split()
        folded_tokens = []
        for t in tokens:
            t_lower = t.lower()
            if t_lower in translit_map:
                folded = translit_map[t_lower]
                folded_tokens.extend(folded.split())
            else:
                folded_tokens.append(t)
        cleaned = ' '.join(folded_tokens)

        self._normalized_cache[filename] = cleaned
        return cleaned

    def _similarity(self, text1: str, text2: str) -> float:
        """
        Compute token set ratio between two strings, return as float 0..1.
        """
        if not text1 or not text2:
            return 0.0
        return fuzz.token_set_ratio(text1, text2) / 100.0

    # ---- Main detection logic ----
    def detect_for_mount(self, mount_name: str, mount_path: str):
        """
        Scan all files in the mount, group by folder (or actress for songs),
        and detect duplicate clusters within each folder using filename similarity.
        """
        files_map = mysql_db_instance.get_tracked_files_map(mount_name)
        if not files_map:
            logger.info(f"No files found for mount {mount_name}")
            return

        # Group by folder (using the same grouping logic as before)
        folders: Dict[str, List[str]] = {}
        for rel_path, rec in files_map.items():
            if self.media_type == "songs":
                # actress is the first component of the relative path
                parts = rel_path.split('/')
                folder = parts[0] if parts else ""
            else:
                folder = os.path.dirname(rel_path)
            folders.setdefault(folder, []).append(rel_path)

        logger.info(f"Detecting duplicates for mount {mount_name} across {len(folders)} folders")
        for folder, rel_paths in folders.items():
            if folder:  # skip empty folder (files directly under mount)
                self._process_folder(mount_name, mount_path, folder, rel_paths, files_map)

    def _process_folder(self, mount_name: str, mount_path: str, folder: str,
                        rel_paths: List[str], files_map: Dict[str, Dict]):
        """
        Process one folder: build normalized representations, compute similarity
        matrix, cluster by threshold, and insert duplicate groups.
        """
        if len(rel_paths) < 2:
            return

        # Prepare data for each file: rel_path -> normalized full text (folder + cleaned filename)
        file_texts: Dict[str, str] = {}
        for rel in rel_paths:
            filename = os.path.basename(rel)
            cleaned_name = self._normalize_filename(filename)
            # Use folder name as context
            full_text = f"{folder} {cleaned_name}".strip()
            file_texts[rel] = full_text

        rel_list = list(file_texts.keys())
        n = len(rel_list)

        # Compute similarity matrix (only upper triangle) – we can use greedy clustering
        # We'll do simple greedy: for each unvisited i, form cluster with all j>i where sim > threshold
        visited = set()
        clusters = []

        for i in range(n):
            if i in visited:
                continue
            cluster_indices = [i]
            visited.add(i)
            for j in range(i + 1, n):
                if j in visited:
                    continue
                sim = self._similarity(file_texts[rel_list[i]], file_texts[rel_list[j]])
                if sim > self.similarity_threshold:
                    cluster_indices.append(j)
                    visited.add(j)
            if len(cluster_indices) > 1:
                clusters.append([rel_list[idx] for idx in cluster_indices])

        if not clusters:
            return

        # Delete previous duplicate entries for this folder
        group_key = os.path.join(mount_path, folder)  # full container folder path
        mysql_db_instance.delete_duplicate_groups_by_group_key(group_key)

        # For each cluster, insert canonical and duplicates
        for cluster in clusters:
            # canonical = first file in cluster
            canonical_rel = cluster[0]
            canonical_full = os.path.join(mount_path, canonical_rel)
            canonical_file = files_map[canonical_rel]
            canonical_name = os.path.basename(canonical_rel)

            # Insert canonical file itself (similarity 1.0)
            mysql_db_instance.insert_duplicate_group(
                group_key=group_key,
                file_path=canonical_full,
                file_name=canonical_name,
                mount=mount_name,
                vector_id=canonical_file.get('vector_id'),
                similarity_score=1.0,
                canonical_file_path=canonical_full,
                metadata={},
                status='PENDING_REVIEW'
            )

            # Insert each duplicate
            for dup_rel in cluster[1:]:
                dup_full = os.path.join(mount_path, dup_rel)
                dup_file = files_map[dup_rel]
                sim = self._similarity(
                    file_texts[canonical_rel],
                    file_texts[dup_rel]
                )
                mysql_db_instance.insert_duplicate_group(
                    group_key=group_key,
                    file_path=dup_full,
                    file_name=os.path.basename(dup_rel),
                    mount=mount_name,
                    vector_id=dup_file.get('vector_id'),
                    similarity_score=float(sim),
                    canonical_file_path=canonical_full,
                    metadata={},
                    status='PENDING_REVIEW'
                )

        logger.info(f"Folder {folder}: created {sum(len(c)-1 for c in clusters)} duplicate entries")
    