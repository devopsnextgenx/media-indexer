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
            self._ensure_table()
            self._ensure_download_tracker_table()
            self._ensure_indexing_jobs_table()
            self._ensure_duplicate_groups_table()


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

    def _ensure_table(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
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
                """)
            conn.close()
            logger.info("MySQL 'processed_files' table initialized.")
        except Exception as e:
            logger.error(f"Failed to create MySQL processed_files table: {e}")

    def _ensure_download_tracker_table(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS download_tracker (
                        entry VARCHAR(1024) PRIMARY KEY,
                        status VARCHAR(50) DEFAULT 'PENDING',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
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

        
    def _ensure_duplicate_groups_table(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS duplicate_groups (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        group_key VARCHAR(1024) NOT NULL COMMENT 'Full folder path (container)',
                        file_path VARCHAR(1024) NOT NULL,
                        file_name VARCHAR(255) NOT NULL,
                        mount VARCHAR(255),
                        vector_id VARCHAR(255),
                        similarity_score FLOAT DEFAULT 0,
                        canonical_file_path VARCHAR(1024),
                        status ENUM('PENDING_REVIEW', 'CONFIRMED_DUPLICATE', 'CONFIRMED_UNIQUE', 'AUTO_RESOLVED') DEFAULT 'PENDING_REVIEW',
                        metadata_json LONGTEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_group_file (group_key(255), file_path(255)),
                        INDEX idx_group_key (group_key(255)),
                        INDEX idx_status (status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
            conn.close()
            logger.info("MySQL 'duplicate_groups' table initialized.")
        except Exception as e:
            logger.error(f"Failed to create duplicate_groups table: {e}")

    def insert_duplicate_group(self, group_key: str, file_path: str, file_name: str,
                            mount: str, vector_id: str, similarity_score: float,
                            canonical_file_path: str, metadata: dict = None,
                            status: str = "PENDING_REVIEW"):
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            meta_str = json.dumps(metadata) if metadata else None
            with conn.cursor() as cursor:
                query = """
                    INSERT INTO duplicate_groups
                    (group_key, file_path, file_name, mount, vector_id,
                    similarity_score, canonical_file_path, metadata_json, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        similarity_score = VALUES(similarity_score),
                        canonical_file_path = VALUES(canonical_file_path),
                        status = VALUES(status),
                        metadata_json = VALUES(metadata_json),
                        updated_at = CURRENT_TIMESTAMP
                """
                cursor.execute(query, (group_key, file_path, file_name, mount, vector_id,
                                    similarity_score, canonical_file_path, meta_str, status))
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to insert duplicate group: {e}")
            return False

    def update_duplicate_status_by_file_path(self, file_path: str, status: str) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE duplicate_groups SET status=%s WHERE file_path=%s", (status, file_path))
                updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Failed to update duplicate status: {e}")
            return False

    def get_duplicate_groups(self, group_key: str = None, mount: str = None,
                            status: str = None, limit: int = 100, offset: int = 0) -> list:
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            conditions = []
            params = []
            if group_key:
                conditions.append("group_key = %s")
                params.append(group_key)
            if mount:
                conditions.append("mount = %s")
                params.append(mount)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions) if conditions else "1"
            query = f"SELECT * FROM duplicate_groups WHERE {where} ORDER BY group_key, similarity_score DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch duplicate groups: {e}")
            return []

    def get_duplicate_group_by_group_key(self, group_key: str) -> list:
        return self.get_duplicate_groups(group_key=group_key)

    def delete_duplicate_groups_by_group_key(self, group_key: str) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM duplicate_groups WHERE group_key = %s", (group_key,))
                deleted = cursor.rowcount
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete duplicate groups by group_key: {e}")
            return 0

    def truncate_duplicate_groups(self) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM duplicate_groups")
                count = (cursor.fetchone() or {}).get("cnt", 0)
                cursor.execute("TRUNCATE TABLE duplicate_groups")
            conn.close()
            logger.info(f"Truncated 'duplicate_groups' table ({count} rows removed)")
            return count
        except Exception as e:
            logger.error(f"Failed to truncate duplicate_groups: {e}")
            return 0

    def get_duplicate_groups_by_folder(self, folder_path: str, status: str = None, limit: int = 100, offset: int = 0) -> list:
        """Return duplicate entries whose group_key starts with folder_path."""
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            conditions = ["group_key LIKE %s"]
            params = [folder_path + '%']
            if status:
                conditions.append("status = %s")
                params.append(status)
            query = f"SELECT * FROM duplicate_groups WHERE {' AND '.join(conditions)} ORDER BY group_key, similarity_score DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch duplicate groups by folder: {e}")
            return []

    def get_duplicate_group_by_file_path(self, file_path: str) -> list:
        """Return all duplicate entries matching the exact file_path (usually one)."""
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM duplicate_groups WHERE file_path = %s", (file_path,))
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group by file_path: {e}")
            return []


    def get_duplicate_group_by_vector_id(self, vector_id: str) -> list:
        """Return duplicate group entries matching a specific vector_id."""
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM duplicate_groups WHERE vector_id = %s",
                    (vector_id,)
                )
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group by vector_id: {e}")
            return []

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
            
    def add_or_update_download_entry(self, entry: str) -> str:
        """
        Adds entry or updates timestamp.
        If existing entry has status 'CONFIRMED', ignores the update and returns 'CONFIRMED'.
        """
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
                    INSERT INTO download_tracker (entry, status, updated_at)
                    VALUES (%s, 'PENDING', CURRENT_TIMESTAMP)
                    ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP;
                """
                cursor.execute(query, (entry,))
            conn.close()
            return "PENDING" if not row else row["status"]
        except Exception as e:
            logger.error(f"Failed to add/update download entry '{entry}': {e}")
            return "ERROR"

    def update_download_status(self, entry: str, status: str) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                query = "UPDATE download_tracker SET status=%s WHERE entry=%s"
                cursor.execute(query, (status, entry))
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
        """Batch removes records for deleted disk files."""
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
        """Wipes the processed_files table only, for a clean re-index.
        download_tracker and indexing_jobs are intentionally left untouched."""
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
        """Stores mount nested tree structure in Redis."""
        if not self.enabled or not self.client:
            return
        try:
            key = f"mount:tree:{mount_name}"
            self.client.set(key, json.dumps(tree_data))
        except Exception as e:
            logger.error(f"Failed to set Redis mount tree for {mount_name}: {e}")

    def get_mount_tree(self, mount_name: str) -> dict | None:
        """Retrieves mount nested tree structure from Redis."""
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
        """Updates individual file node attributes in the cached tree."""
        tree = self.get_mount_tree(mount_name)
        if not tree:
            return

        parts = [p for p in rel_path.split("/") if p]
        curr = tree
        found = False

        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                # Target file node
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
                # Traverse directory level
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
        """Deletes every cached mount:tree:* key, e.g. as part of a full index clean."""
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