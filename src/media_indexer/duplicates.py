import os
import logging
import numpy as np
from typing import Dict, List, Any, Optional
from qdrant_client import QdrantClient
from media_indexer.database import mysql_db_instance
from media_indexer.config import settings

logger = logging.getLogger(__name__)

class DuplicateDetector:
    def __init__(self, qdrant_client: QdrantClient, collection_name: str,
                 similarity_threshold: float = 0.85):
        self.qdrant = qdrant_client
        self.collection_name = collection_name
        self.similarity_threshold = similarity_threshold

    def detect_for_mount(self, mount_name: str, mount_path: str):
        """Run duplicate detection for all folders under a mount."""
        # Get tracked files from MySQL
        files_map = mysql_db_instance.get_tracked_files_map(mount_name)
        if not files_map:
            logger.info(f"No files found for mount {mount_name}")
            return

        # Group by folder (relative path directory)
        folders: Dict[str, List[str]] = {}
        for rel_path, rec in files_map.items():
            folder = os.path.dirname(rel_path)  # relative folder
            folders.setdefault(folder, []).append(rel_path)

        logger.info(f"Detecting duplicates for mount {mount_name} across {len(folders)} folders")
        for folder, rel_paths in folders.items():
            self._process_folder(mount_name, mount_path, folder, rel_paths, files_map)

    def _process_folder(self, mount_name: str, mount_path: str, folder: str,
                        rel_paths: List[str], files_map: Dict[str, Dict]):
        if len(rel_paths) < 2:
            return

        # Build mapping from rel_path to vector_id and vector
        vector_ids = []
        rel_to_vid = {}
        for rel in rel_paths:
            vid = files_map[rel].get('vector_id')
            if vid:
                vector_ids.append(vid)
                rel_to_vid[rel] = vid

        if len(vector_ids) < 2:
            return

        # Retrieve vectors from Qdrant
        try:
            points = self.qdrant.retrieve(
                collection_name=self.collection_name,
                ids=vector_ids,
                with_vectors=True,
                with_payload=False
            )
        except Exception as e:
            logger.warning(f"Failed to retrieve vectors for folder {folder}: {e}")
            return

        id_to_vec = {p.id: p.vector for p in points if p.vector is not None}
        if len(id_to_vec) < 2:
            return

        # Map rel_path -> vector
        rel_to_vec = {}
        for rel, vid in rel_to_vid.items():
            if vid in id_to_vec:
                rel_to_vec[rel] = id_to_vec[vid]

        if len(rel_to_vec) < 2:
            return

        # Build list of vectors and corresponding rel_paths
        vec_list = []
        rel_list = []
        for rel, vec in rel_to_vec.items():
            vec_list.append(vec)
            rel_list.append(rel)

        # Normalize and compute cosine similarity
        vecs = np.array(vec_list)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        # Avoid division by zero
        norms = np.where(norms == 0, 1e-10, norms)
        vecs = vecs / norms
        sim_matrix = np.dot(vecs, vecs.T)

        # Greedy clustering
        visited = set()
        groups = []
        n = len(rel_list)
        for i in range(n):
            if i in visited:
                continue
            cluster = [i]
            visited.add(i)
            for j in range(i+1, n):
                if j in visited:
                    continue
                if sim_matrix[i][j] > self.similarity_threshold:
                    cluster.append(j)
                    visited.add(j)
            if len(cluster) > 1:
                groups.append(cluster)

        if not groups:
            return

        # For each group, insert into duplicate_groups (and remove previous entries for this folder)
        group_key = os.path.join(mount_path, folder)  # full container folder path
        # Delete previous duplicate entries for this folder
        mysql_db_instance.delete_duplicate_groups_by_group_key(group_key)

        for cluster in groups:
            canonical_idx = cluster[0]
            canonical_rel = rel_list[canonical_idx]
            canonical_full = os.path.join(mount_path, canonical_rel)
            canonical_file = files_map[canonical_rel]
            canonical_name = os.path.basename(canonical_rel)
            canonical_metadata = {}  # optional

            for idx in cluster[1:]:
                rel = rel_list[idx]
                full_path = os.path.join(mount_path, rel)
                file_info = files_map[rel]
                similarity = sim_matrix[canonical_idx][idx]
                mysql_db_instance.insert_duplicate_group(
                    group_key=group_key,
                    file_path=full_path,
                    file_name=os.path.basename(rel),
                    mount=mount_name,
                    vector_id=file_info.get('vector_id'),
                    similarity_score=float(similarity),
                    canonical_file_path=canonical_full,
                    metadata=canonical_metadata  # we can enrich later
                )

        logger.info(f"Folder {folder}: created {sum(len(g)-1 for g in groups)} duplicate entries")