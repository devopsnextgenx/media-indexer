import os
import json
import logging
from qdrant_client import QdrantClient, models
from media_indexer.config import settings
import redis

logger = logging.getLogger(__name__)

class VectorDatabase:
    def __init__(self):
        v_cfg = settings.vectordb
        if v_cfg.host and v_cfg.port:
            logger.info(f"Connecting to standalone Qdrant at {v_cfg.host}:{v_cfg.port}")
            self.client = QdrantClient(host=v_cfg.host, port=v_cfg.port)
        else:
            os.makedirs(v_cfg.embedded_path, exist_ok=True)
            logger.info(f"Initializing embedded Qdrant DB at path: {v_cfg.embedded_path}")
            self.client = QdrantClient(path=v_cfg.embedded_path)
            
        self.collection_name = v_cfg.collection_name
        self._ensure_collection()

    def _ensure_collection(self):
        collections = [col.name for col in self.client.get_collections().collections]
        if self.collection_name not in collections:
            logger.info(f"Creating vector collection '{self.collection_name}'")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=settings.embedding.dimension,
                    distance=models.Distance.COSINE
                )
            )
        self._ensure_text_indexes()

    def _ensure_text_indexes(self):
        text_params = models.TextIndexParams(
            type=models.TextIndexType.TEXT,
            tokenizer=models.TokenizerType.PREFIX,
            min_token_len=1,
            max_token_len=20,
            lowercase=True,
        )
        for field in ("normalized_title", "file_name"):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field,
                    field_schema=text_params,
                )
            except Exception as e:
                logger.debug(f"Payload text index for '{field}' already exists or failed: {e}")

    def upsert_media_item(self, point_id: str, vector: list[float], payload: dict):
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload
                )
            ]
        )

    def delete_media_item(self, point_id: str):
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.PointIdsList(points=[point_id])
        )

    def _match_filter(self, key: str, value: str) -> models.Filter:
        return models.Filter(
            must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]
        )

    def find_points_by_field(self, key: str, value: str, limit: int = 100) -> list:
        try:
            points, _ = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._match_filter(key, value),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            return points
        except Exception as e:
            logger.warning(f"Lookup by {key}='{value}' failed: {e}")
            return []

    def find_point_ids_by_file_path(self, file_path: str, limit: int = 100) -> list:
        return [p.id for p in self.find_points_by_file_path(file_path, limit=limit)]

    def find_points_by_file_path(self, file_path: str, limit: int = 100) -> list:
        return self.find_points_by_field("file_path", file_path, limit=limit)

    def delete_points(self, point_ids: list) -> int:
        if not point_ids:
            return 0
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.PointIdsList(points=point_ids),
        )
        return len(point_ids)

    def update_payload_for_points(self, point_ids: list, payload_updates: dict):
        self.client.set_payload(
            collection_name=self.collection_name,
            payload=payload_updates,
            points=models.PointIdsList(points=point_ids),
        )

    def update_payload_by_file_path(self, file_path: str, payload_updates: dict) -> int:
        point_ids = self.find_point_ids_by_file_path(file_path)
        if not point_ids:
            return 0
        self.update_payload_for_points(point_ids, payload_updates)
        return len(point_ids)

    def delete_by_file_path(self, file_path: str) -> int:
        return self.delete_points(self.find_point_ids_by_file_path(file_path))

    def delete_by_file_name(self, file_name: str) -> int:
        return self.delete_points([p.id for p in self.find_points_by_field("file_name", file_name)])

    def count_items(self) -> int:
        try:
            return self.client.count(collection_name=self.collection_name, exact=True).count
        except Exception as e:
            logger.warning(f"Failed to count points in '{self.collection_name}': {e}")
            return 0

    def truncate_collection(self) -> int:
        removed = self.count_items()
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=models.Filter())
        )
        logger.info(f"Truncated collection '{self.collection_name}' ({removed} points)")
        return removed

    def reset_collection(self) -> int:
        removed = self.count_items()
        self.client.delete_collection(collection_name=self.collection_name)
        self._ensure_collection()
        logger.info(f"Recreated collection '{self.collection_name}' ({removed} points dropped)")
        return removed

    def search_vectors(self, query_vector: list[float], limit: int = 10):
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=limit,
        )
        return response.points

    def keyword_search(self, query: str, limit: int = 50):
        text_filter = models.Filter(
            should=[
                models.FieldCondition(key="normalized_title", match=models.MatchText(text=query)),
                models.FieldCondition(key="file_name", match=models.MatchText(text=query)),
            ]
        )
        points, _ = self.client.scroll(
            collection_name=self.collection_name,
            scroll_filter=text_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return points


class MySQLDatabase:
    def __init__(self):
        self.cfg = getattr(settings, "mysql", None)
        self.enabled = getattr(self.cfg, "enabled", False) if self.cfg else False
        if self.enabled:
            self._ensure_tables()
            self._ensure_download_tracker_table()
            self._ensure_indexing_jobs_table()

    # ----------------------------------------------------------------------
    # Table definitions (one place)
    # ----------------------------------------------------------------------
    def _get_table_definitions(self) -> dict:
        return {
            "processed_files": """
                CREATE TABLE IF NOT EXISTS processed_files (
                    id VARCHAR(255) PRIMARY KEY,
                    file_path VARCHAR(1024) NOT NULL,
                    file_name VARCHAR(255) NOT NULL,
                    relative_path VARCHAR(1024),
                    mount VARCHAR(255),
                    file_size BIGINT DEFAULT 0,
                    mtime DOUBLE DEFAULT 0,
                    status VARCHAR(50) DEFAULT 'PENDING',
                    vector_id VARCHAR(255),
                    jellyfin_id VARCHAR(255),
                    metadata_json LONGTEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_file_path (file_path(255)),
                    INDEX idx_mount (mount)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "duplicate_groups": """
                CREATE TABLE IF NOT EXISTS duplicate_groups (
                    group_id VARCHAR(80) PRIMARY KEY,
                    title_key VARCHAR(512),
                    member_count INT DEFAULT 0,
                    mount VARCHAR(255),
                    folder_path VARCHAR(1024),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_mount (mount),
                    INDEX idx_folder (folder_path(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "duplicate_group_candidates": """
                CREATE TABLE IF NOT EXISTS duplicate_group_candidates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    group_id VARCHAR(80) NOT NULL,
                    file_id VARCHAR(255) NOT NULL,
                    full_path VARCHAR(1024) NOT NULL,
                    mount VARCHAR(255),
                    title_score DECIMAL(5,1),
                    movie_score DECIMAL(5,1),
                    artist_score DECIMAL(5,1),
                    overall_score DECIMAL(5,1),
                    confidence ENUM('HIGH','MEDIUM','LOW') DEFAULT 'LOW',
                    status ENUM('PENDING','DUPLICATE','REJECTED') DEFAULT 'PENDING',
                    stats_json JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_group_file (group_id, file_id),
                    INDEX idx_group_id (group_id),
                    INDEX idx_mount (mount),
                    INDEX idx_full_path (full_path(255)),
                    CONSTRAINT fk_candidate_group FOREIGN KEY (group_id)
                        REFERENCES duplicate_groups(group_id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "token_stats": """
                CREATE TABLE IF NOT EXISTS token_stats (
                    phonetic_code VARCHAR(32) PRIMARY KEY,
                    example_word VARCHAR(255),
                    tier ENUM('title','movie','artist') DEFAULT 'title',
                    doc_frequency INT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
        }

    def _ensure_tables(self):
        """Create (or recreate) duplicate‑related tables with the latest schema.
        This drops and re‑creates the tables managed by this module to ensure
        columns like `mount` exist. Existing data in these tables will be lost.
        """
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                # Drop and recreate duplicate‑related tables to guarantee new schema
                tables_to_recreate = ["duplicate_groups", "duplicate_group_candidates", "token_stats"]
                for table in tables_to_recreate:
                    cursor.execute(f"DROP TABLE IF EXISTS {table}")
                # Now create all tables from definitions (including processed_files)
                for name, ddl in self._get_table_definitions().items():
                    cursor.execute(ddl)
            conn.close()
            logger.info("MySQL duplicate‑related tables recreated with latest schema.")
        except Exception as e:
            logger.error(f"Failed to create MySQL tables: {e}")

    def _get_connection(self):
        if not self.enabled:
            return None
        try:
            import pymysql
            return pymysql.connect(
                host=self.cfg.host,
                port=self.cfg.port,
                user=self.cfg.user,
                password=self.cfg.password,
                database=self.cfg.database,
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor
            )
        except Exception as e:
            logger.warning(f"MySQL connection error: {e}")
            return None

    # ----------------------------------------------------------------------
    # Download tracker & indexing jobs (unchanged)
    # ----------------------------------------------------------------------
    def _ensure_download_tracker_table(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS download_tracker (
                        entry VARCHAR(768) PRIMARY KEY,
                        title VARCHAR(255) DEFAULT NULL,
                        status VARCHAR(50) DEFAULT 'PENDING',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        thumbnail MEDIUMTEXT DEFAULT NULL,
                        size BIGINT DEFAULT 0,
                        INDEX idx_status (status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
            conn.close()
            logger.info("MySQL 'download_tracker' table initialized.")
        except Exception as e:
            logger.error(f"Failed to create MySQL download_tracker table: {e}")
            
    def _ensure_indexing_jobs_table(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS indexing_jobs (
                        job_id VARCHAR(255) PRIMARY KEY,
                        mount_name VARCHAR(255) NOT NULL,
                        status VARCHAR(50) DEFAULT 'PENDING',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        total_files INT DEFAULT 0,
                        processed_files INT DEFAULT 0,
                        added_files INT DEFAULT 0,
                        updated_files INT DEFAULT 0,
                        skipped_files INT DEFAULT 0,
                        failed_files INT DEFAULT 0,
                        cleaned_orphans INT DEFAULT 0,
                        eta_seconds INT DEFAULT 0,
                        error_message TEXT,
                        INDEX idx_mount_status (mount_name, status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
            conn.close()
            logger.info("MySQL 'indexing_jobs' table initialized.")
        except Exception as e:
            logger.error(f"Failed to create MySQL indexing_jobs table: {e}")

    # ----------------------------------------------------------------------
    # Duplicate groups – new schema methods
    # ----------------------------------------------------------------------
    def truncate_duplicate_tables(self) -> dict:
        """Truncate duplicate_groups and duplicate_group_candidates tables."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        results = {}
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

                for table in ["duplicate_group_candidates", "duplicate_groups"]:
                    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                    removed = (cursor.fetchone() or {}).get("cnt", 0)
                    cursor.execute(f"TRUNCATE TABLE {table}")
                    results[table] = removed

                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.close()
            logger.info(f"Truncated duplicate tables: {results}")
        except Exception as e:
            logger.error(f"Failed to truncate duplicate tables: {e}")
        return results

    def insert_duplicate_group(self, group_id: str, title_key: str,
                               member_count: int, mount: str, folder_path: str) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO duplicate_groups
                    (group_id, title_key, member_count, mount, folder_path)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        title_key = VALUES(title_key),
                        member_count = VALUES(member_count),
                        mount = VALUES(mount),
                        folder_path = VALUES(folder_path),
                        updated_at = CURRENT_TIMESTAMP
                """, (group_id, title_key, member_count, mount, folder_path))
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to insert duplicate group {group_id}: {e}")
            return False

    def insert_candidate(self, group_id: str, file_id: str, full_path: str,
                         mount: str, title_score: float, movie_score: float,
                         artist_score: float, overall_score: float,
                         confidence: str, status: str, stats_json: dict) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            import json
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO duplicate_group_candidates
                    (group_id, file_id, full_path, mount, title_score, movie_score,
                     artist_score, overall_score, confidence, status, stats_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        full_path = VALUES(full_path),
                        mount = VALUES(mount),
                        title_score = VALUES(title_score),
                        movie_score = VALUES(movie_score),
                        artist_score = VALUES(artist_score),
                        overall_score = VALUES(overall_score),
                        confidence = VALUES(confidence),
                        status = VALUES(status),
                        stats_json = VALUES(stats_json),
                        updated_at = CURRENT_TIMESTAMP
                """, (group_id, file_id, full_path, mount, title_score, movie_score,
                      artist_score, overall_score, confidence, status, json.dumps(stats_json)))
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to insert candidate {file_id}: {e}")
            return False

    def delete_duplicate_groups_for_mount(self, mount: str) -> int:
        """Delete all groups and candidates for a given mount."""
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT group_id FROM duplicate_groups WHERE mount = %s", (mount,))
                group_ids = [row["group_id"] for row in cursor.fetchall()]
                if group_ids:
                    placeholders = ','.join(['%s'] * len(group_ids))
                    cursor.execute(f"DELETE FROM duplicate_group_candidates WHERE group_id IN ({placeholders})", group_ids)
                    cursor.execute(f"DELETE FROM duplicate_groups WHERE group_id IN ({placeholders})", group_ids)
                count = len(group_ids)
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed to delete duplicate groups for mount {mount}: {e}")
            return 0

    def get_duplicate_groups(self, mount: str = None, folder: str = None,
                             status: str = None, limit: int = 100, offset: int = 0) -> list:
        """
        Fetch groups with optional mount/folder/status filter.
        Returns a list of group dicts, each containing a 'candidates' list.
        """
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            group_filters = []
            params = []
            if mount:
                group_filters.append("g.mount = %s")
                params.append(mount)
            folder_condition = ""
            if folder:
                folder = folder.rstrip('/') + '/%'
                folder_condition = " AND EXISTS (SELECT 1 FROM duplicate_group_candidates c WHERE c.group_id = g.group_id AND c.full_path LIKE %s)"
                params.append(folder)

            query = """
                SELECT g.group_id, g.title_key, g.member_count, g.mount, g.folder_path,
                       g.created_at, g.updated_at
                FROM duplicate_groups g
                WHERE 1=1
            """
            if group_filters:
                query += " AND " + " AND ".join(group_filters)
            if folder_condition:
                query += folder_condition
            query += " ORDER BY g.updated_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            with conn.cursor() as cursor:
                cursor.execute(query, params)
                groups = cursor.fetchall()

            result = []
            for grp in groups:
                group_id = grp["group_id"]
                cursor.execute("""
                    SELECT id, file_id, full_path, mount, title_score, movie_score,
                           artist_score, overall_score, confidence, status, stats_json,
                           created_at, updated_at
                    FROM duplicate_group_candidates
                    WHERE group_id = %s
                    ORDER BY overall_score DESC
                """, (group_id,))
                candidates = cursor.fetchall()
                for c in candidates:
                    if c.get("stats_json"):
                        try:
                            c["stats_json"] = json.loads(c["stats_json"])
                        except:
                            pass
                grp["candidates"] = candidates
                result.append(grp)
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to fetch duplicate groups: {e}")
            return []

    def get_duplicate_group_by_id(self, group_id: str) -> dict:
        """Return a single group with its candidates."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT group_id, title_key, member_count, mount, folder_path,
                           created_at, updated_at
                    FROM duplicate_groups WHERE group_id = %s
                """, (group_id,))
                group = cursor.fetchone()
                if not group:
                    return {}
                cursor.execute("""
                    SELECT id, file_id, full_path, mount, title_score, movie_score,
                           artist_score, overall_score, confidence, status, stats_json,
                           created_at, updated_at
                    FROM duplicate_group_candidates
                    WHERE group_id = %s
                    ORDER BY overall_score DESC
                """, (group_id,))
                candidates = cursor.fetchall()
                for c in candidates:
                    if c.get("stats_json"):
                        try:
                            c["stats_json"] = json.loads(c["stats_json"])
                        except:
                            pass
                group["candidates"] = candidates
            conn.close()
            return group
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group {group_id}: {e}")
            return {}

    def get_duplicate_group_by_file(self, file_path: str) -> dict:
        """Find the group that contains the given file path."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT group_id FROM duplicate_group_candidates
                    WHERE full_path = %s
                """, (file_path,))
                row = cursor.fetchone()
                if not row:
                    return {}
                group_id = row["group_id"]
            conn.close()
            return self.get_duplicate_group_by_id(group_id)
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group for file {file_path}: {e}")
            return {}

    def get_duplicate_group_ids_for_paths(self, file_paths: list[str]) -> dict:
        """Return dict mapping file_path -> group_id for files that belong to any duplicate group."""
        if not self.enabled or not file_paths:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                placeholders = ','.join(['%s'] * len(file_paths))
                query = f"SELECT full_path, group_id FROM duplicate_group_candidates WHERE full_path IN ({placeholders})"
                cursor.execute(query, tuple(file_paths))
                rows = cursor.fetchall()
                result = {row["full_path"]: row["group_id"] for row in rows}
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to get duplicate group ids for paths: {e}")
            return {}

    def update_candidate_status(self, file_path: str, new_status: str) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE duplicate_group_candidates
                    SET status = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE full_path = %s
                """, (new_status, file_path))
                updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Failed to update candidate status for {file_path}: {e}")
            return False

    # ----------------------------------------------------------------------
    # Truncate tables (used by admin clean)
    # ----------------------------------------------------------------------
    def truncate_tables(self, tables: list = None) -> dict:
        """Truncate given tables; default list: token_stats, processed_files,
        duplicate_group_candidates, duplicate_groups, media_files (if exists)."""
        if not self.enabled:
            return {}
        default_tables = ["token_stats", "processed_files",
                          "duplicate_group_candidates", "duplicate_groups", "media_files"]
        to_truncate = tables if tables is not None else default_tables
        conn = self._get_connection()
        if not conn:
            return {}
        results = {}
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

                for table in to_truncate:
                    cursor.execute(f"SHOW TABLES LIKE '{table}'")
                    if cursor.fetchone():
                        cursor.execute(f"TRUNCATE TABLE {table}")
                        results[table] = "truncated"
                    else:
                        results[table] = "skipped (not exists)"

                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
            conn.close()
            logger.info(f"Truncated tables: {', '.join(to_truncate)}")
        except Exception as e:
            logger.error(f"Failed to truncate tables: {e}")
        return results

    # ----------------------------------------------------------------------
    # Processed files methods (unchanged)
    # ----------------------------------------------------------------------
    def upsert_file_record(
        self,
        file_id: str,
        file_path: str,
        file_name: str,
        relative_path: str,
        mount: str,
        file_size: int = 0,
        mtime: float = 0.0,
        status: str = "INDEXED",
        vector_id: str = None,
        jellyfin_id: str = None,
        metadata: dict = None
    ):
        if not self.enabled:
            return
        conn = self._get_connection()
        if not conn:
            return
        try:
            meta_str = json.dumps(metadata) if metadata else None
            query = """
                INSERT INTO processed_files 
                (id, file_path, file_name, relative_path, mount, file_size, mtime, status, vector_id, jellyfin_id, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    file_path = VALUES(file_path),
                    file_name = VALUES(file_name),
                    relative_path = VALUES(relative_path),
                    mount = VALUES(mount),
                    file_size = VALUES(file_size),
                    mtime = VALUES(mtime),
                    status = VALUES(status),
                    vector_id = VALUES(vector_id),
                    jellyfin_id = VALUES(jellyfin_id),
                    metadata_json = VALUES(metadata_json);
            """
            with conn.cursor() as cursor:
                cursor.execute(query, (file_id, file_path, file_name, relative_path, mount, file_size, mtime, status, vector_id, jellyfin_id, meta_str))
            conn.close()
        except Exception as e:
            logger.error(f"MySQL upsert failed for {file_path}: {e}")

    def update_file_path(self, old_path: str, new_path: str, new_name: str, relative_path: str = None) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                if relative_path:
                    query = "UPDATE processed_files SET file_path=%s, file_name=%s, relative_path=%s WHERE file_path=%s OR file_name=%s"
                    cursor.execute(query, (new_path, new_name, relative_path, old_path, os.path.basename(old_path)))
                else:
                    query = "UPDATE processed_files SET file_path=%s, file_name=%s WHERE file_path=%s OR file_name=%s"
                    cursor.execute(query, (new_path, new_name, old_path, os.path.basename(old_path)))
                count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"MySQL update path failed for {old_path}: {e}")
            return 0

    def delete_file_by_path(self, file_path: str) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                query = "DELETE FROM processed_files WHERE file_path=%s OR file_name=%s"
                cursor.execute(query, (file_path, os.path.basename(file_path)))
                count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"MySQL delete failed for {file_path}: {e}")
            return 0

    def get_tracked_files_by_mount(self, mount: str) -> dict:
        """Returns dict mapping relative_path -> row dict."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                query = "SELECT id, file_path, relative_path, file_size, mtime, status, vector_id FROM processed_files WHERE mount=%s"
                cursor.execute(query, (mount,))
                rows = cursor.fetchall()
            conn.close()
            return {row["relative_path"] or row["file_path"]: row for row in rows}
        except Exception as e:
            logger.error(f"MySQL fetch failed for mount {mount}: {e}")
            return {}
        
    def get_tracked_files_map(self, mount: str) -> dict:
        """Returns map: relative_path -> {mtime, file_size, id, vector_id}"""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                query = "SELECT id, relative_path, file_path, file_size, mtime, vector_id FROM processed_files WHERE mount=%s"
                cursor.execute(query, (mount,))
                rows = cursor.fetchall()
            conn.close()
            return {row["relative_path"] or row["file_path"]: row for row in rows}
        except Exception as e:
            logger.error(f"Failed fetching tracked files for mount {mount}: {e}")
            return {}

    def delete_records_by_paths(self, file_paths: list[str]) -> int:
        if not self.enabled or not file_paths:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                format_strings = ','.join(['%s'] * len(file_paths))
                cursor.execute(f"DELETE FROM processed_files WHERE file_path IN ({format_strings})", tuple(file_paths))
                count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed deleting records: {e}")
            return 0

    def truncate_processed_files(self) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM processed_files")
                removed = (cursor.fetchone() or {}).get("cnt", 0)
                cursor.execute("TRUNCATE TABLE processed_files")
            conn.close()
            logger.info(f"Truncated 'processed_files' table ({removed} rows removed)")
            return removed
        except Exception as e:
            logger.error(f"Failed to truncate processed_files table: {e}")
            return 0

    # ----------------------------------------------------------------------
    # Download tracker methods (unchanged)
    # ----------------------------------------------------------------------
    def add_or_update_download_entry(self, entry: str, title: str) -> str:
        if not self.enabled:
            return "DISABLED"
        conn = self._get_connection()
        if not conn:
            return "ERROR"
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status FROM download_tracker WHERE entry=%s", (entry,))
                row = cursor.fetchone()
                if row and row["status"] == "CONFIRMED":
                    conn.close()
                    return "CONFIRMED"
                query = """
                    INSERT INTO download_tracker (entry, status, updated_at, title)
                    VALUES (%s, 'PENDING', CURRENT_TIMESTAMP, %s)
                    ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP;
                """
                cursor.execute(query, (entry, title))
            conn.close()
            return "PENDING" if not row else row["status"]
        except Exception as e:
            logger.error(f"Failed to add/update download entry '{entry}': {e}")
            return "ERROR"

    def update_download_status(self, entry: str, status: str, size: int = 0, thumbnail: str = None) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                query = "UPDATE download_tracker SET status=%s, size=%s, thumbnail=%s WHERE entry=%s"
                cursor.execute(query, (status, size, thumbnail, entry))
                updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Failed to update download entry status: {e}")
            return False

    def remove_download_entry(self, entry: str) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                query = "DELETE FROM download_tracker WHERE entry=%s"
                cursor.execute(query, (entry,))
                deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to remove download entry: {e}")
            return False

    # ----------------------------------------------------------------------
    # Indexing jobs methods (unchanged)
    # ----------------------------------------------------------------------
    def upsert_job_record(self, job_info: dict):
        if not self.enabled:
            return
        conn = self._get_connection()
        if not conn:
            return
        try:
            query = """
                INSERT INTO indexing_jobs 
                (job_id, mount_name, status, total_files, processed_files, added_files, updated_files, skipped_files, failed_files, cleaned_orphans, eta_seconds, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    total_files = VALUES(total_files),
                    processed_files = VALUES(processed_files),
                    added_files = VALUES(added_files),
                    updated_files = VALUES(updated_files),
                    skipped_files = VALUES(skipped_files),
                    failed_files = VALUES(failed_files),
                    cleaned_orphans = VALUES(cleaned_orphans),
                    eta_seconds = VALUES(eta_seconds),
                    error_message = VALUES(error_message);
            """
            with conn.cursor() as cursor:
                cursor.execute(query, (
                    job_info["job_id"], job_info["mount_name"], job_info["status"],
                    job_info.get("total_files", 0), job_info.get("processed_files", 0),
                    job_info.get("added_files", 0), job_info.get("updated_files", 0),
                    job_info.get("skipped_files", 0), job_info.get("failed_files", 0),
                    job_info.get("cleaned_orphans", 0), job_info.get("eta_seconds", 0),
                    job_info.get("error")
                ))
            conn.close()
        except Exception as e:
            logger.error(f"MySQL upsert job failed for {job_info.get('job_id')}: {e}")


class RedisDatabase:
    def __init__(self):
        self.cfg = getattr(settings, "redis", None)
        self.enabled = getattr(self.cfg, "enabled", False) if self.cfg else False
        self.client = None
        if self.enabled:
            try:
                self.client = redis.Redis(
                    host=self.cfg.host,
                    port=self.cfg.port,
                    db=self.cfg.db,
                    password=self.cfg.password,
                    decode_responses=True
                )
                self.client.ping()
                logger.info("Redis database connection established successfully.")
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}")
                self.enabled = False

    def set_mount_tree(self, mount_name: str, tree_data: dict):
        if not self.enabled or not self.client:
            return
        try:
            key = f"mount:tree:{mount_name}"
            self.client.set(key, json.dumps(tree_data))
        except Exception as e:
            logger.error(f"Failed to set Redis mount tree for {mount_name}: {e}")

    def get_mount_tree(self, mount_name: str) -> dict | None:
        if not self.enabled or not self.client:
            return None
        try:
            key = f"mount:tree:{mount_name}"
            data = self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Failed to fetch Redis mount tree for {mount_name}: {e}")
            return None

    def update_node_metadata(
        self,
        mount_name: str,
        rel_path: str,
        vector_id: str,
        jellyfin_id: str = None,
        primary_image_tag: str = None,
        width: int = None,
        height: int = None,
        duration: str = None,
    ):
        tree = self.get_mount_tree(mount_name)
        if not tree:
            return

        parts = [p for p in rel_path.split("/") if p]
        curr = tree
        found = False

        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                for child in curr.get("children", []):
                    if child.get("name") == part and child.get("type") == "file":
                        child["vector_id"] = vector_id
                        if jellyfin_id:
                            child["jellyfin_id"] = jellyfin_id
                        if primary_image_tag:
                            child["primary_image_tag"] = primary_image_tag
                        if width:
                            child["width"] = width
                        if height:
                            child["height"] = height
                        if duration:
                            child["duration"] = duration
                        found = True
                        break
            else:
                matched_dir = None
                for child in curr.get("children", []):
                    if child.get("name") == part and child.get("type") == "folder":
                        matched_dir = child
                        break
                if matched_dir:
                    curr = matched_dir
                else:
                    break

        if found:
            self.set_mount_tree(mount_name, tree)

    def clear_all_mount_trees(self) -> int:
        if not self.enabled or not self.client:
            return 0
        try:
            keys = list(self.client.scan_iter(match="mount:tree:*"))
            if keys:
                self.client.delete(*keys)
            return len(keys)
        except Exception as e:
            logger.error(f"Failed to clear Redis mount trees: {e}")
            return 0


db_instance = VectorDatabase()
mysql_db_instance = MySQLDatabase()
redis_db_instance = RedisDatabase()