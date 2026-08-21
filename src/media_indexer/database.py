import os
import json
import logging
from qdrant_client import QdrantClient, models
from media_indexer.config import settings

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
        
db_instance = VectorDatabase()
mysql_db_instance = MySQLDatabase()