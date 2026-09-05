import os
import json
import logging
from collections import Counter
from qdrant_client import QdrantClient, models
from media_indexer.config import settings
from media_indexer.utils import generate_file_id
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
            self._ensure_duplicate_schema_upgrades()
            self._ensure_download_tracker_table()
            self._ensure_indexing_jobs_table()

    # ----------------------------------------------------------------------
    # Table definitions (one place)
    # ----------------------------------------------------------------------
    def _get_table_definitions(self) -> dict:
        return {
            "media_files": """
                CREATE TABLE IF NOT EXISTS media_files (
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
                    scope_key VARCHAR(512),
                    member_count INT DEFAULT 0,
                    mount VARCHAR(255),
                    folder_path VARCHAR(1024),
                    reviewed_status ENUM('UNRESOLVED','RESOLVED') DEFAULT 'UNRESOLVED',
                    resolved_at DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_mount (mount),
                    INDEX idx_scope (scope_key(255)),
                    INDEX idx_folder (folder_path(255)),
                    INDEX idx_reviewed_status (reviewed_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "duplicate_group_candidates": """
                CREATE TABLE IF NOT EXISTS duplicate_group_candidates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    group_id VARCHAR(80) NOT NULL,
                    file_id VARCHAR(255) NOT NULL,
                    full_path VARCHAR(1024) NOT NULL,
                    file_name VARCHAR(512),
                    mount VARCHAR(255),
                    resolution VARCHAR(16),
                    rank_in_group INT DEFAULT 0,
                    is_primary TINYINT(1) DEFAULT 0,
                    song_title VARCHAR(512),
                    movie_or_album VARCHAR(512),
                    artists_json TEXT,
                    title_score DECIMAL(5,1),
                    movie_score DECIMAL(5,1),
                    artist_score DECIMAL(5,1),
                    overall_score DECIMAL(5,1),
                    confidence ENUM('HIGH','MEDIUM','LOW') DEFAULT 'LOW',
                    status ENUM('PENDING','DUPLICATE','REJECTED','NOT_DUPLICATE') DEFAULT 'PENDING',
                    marked_not_duplicate_at DATETIME NULL,
                    stats_json JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_group_file (group_id, file_id),
                    INDEX idx_group_id (group_id),
                    INDEX idx_mount (mount),
                    INDEX idx_file_name (file_name(255)),
                    INDEX idx_full_path (full_path(255)),
                    INDEX idx_status (status),
                    CONSTRAINT fk_candidate_group FOREIGN KEY (group_id)
                        REFERENCES duplicate_groups(group_id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "llm_parsed_metadata": """
                CREATE TABLE IF NOT EXISTS llm_parsed_metadata (
                    file_name VARCHAR(512) PRIMARY KEY,
                    full_path VARCHAR(1024),
                    song_title VARCHAR(512),
                    movie_or_album VARCHAR(512),
                    artists_json TEXT,
                    model_name VARCHAR(255),
                    source_endpoint VARCHAR(255),
                    parsed_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_full_path (full_path(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
            "background_jobs": """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id VARCHAR(255) PRIMARY KEY,
                    job_type VARCHAR(50) NOT NULL,
                    mount_name VARCHAR(255) DEFAULT NULL,
                    status VARCHAR(50) DEFAULT 'PENDING',
                    requested_status VARCHAR(50) DEFAULT NULL,
                    total_items INT DEFAULT 0,
                    processed_items INT DEFAULT 0,
                    failed_items INT DEFAULT 0,
                    checkpoint VARCHAR(1024) DEFAULT NULL,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_type_status (job_type, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """,
        }

    def _ensure_tables(self):
        """Create tables if they do not exist with the latest schema.
        Data in existing tables will be preserved on initialization/restart.
        """
        conn = self._get_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cursor:
                # Create all tables from definitions if they do not exist
                for name, ddl in self._get_table_definitions().items():
                    cursor.execute(ddl)
            conn.close()
            logger.info("MySQL tables initialized safely without dropping existing tables.")
        except Exception as e:
            logger.error(f"Failed to create MySQL tables: {e}")

    def _ensure_duplicate_schema_upgrades(self):
        """Idempotent ALTERs for the duplicate-review feature (group-level
        reviewed_status/resolved_at, candidate-level NOT_DUPLICATE status +
        marked_not_duplicate_at). Safe to run on every startup: databases
        created fresh already have these columns from the DDL above, so each
        statement below simply no-ops (duplicate column/key errors are
        swallowed) on those installs, and only actually migrates older ones.
        """
        conn = self._get_connection()
        if not conn:
            return
        statements = [
            "ALTER TABLE duplicate_groups ADD COLUMN reviewed_status "
            "ENUM('UNRESOLVED','RESOLVED') DEFAULT 'UNRESOLVED'",
            "ALTER TABLE duplicate_groups ADD COLUMN resolved_at DATETIME NULL",
            "ALTER TABLE duplicate_groups ADD INDEX idx_reviewed_status (reviewed_status)",
            "ALTER TABLE duplicate_group_candidates MODIFY COLUMN status "
            "ENUM('PENDING','DUPLICATE','REJECTED','NOT_DUPLICATE') DEFAULT 'PENDING'",
            "ALTER TABLE duplicate_group_candidates ADD COLUMN marked_not_duplicate_at DATETIME NULL",
            "ALTER TABLE duplicate_group_candidates ADD INDEX idx_status (status)",
        ]
        try:
            with conn.cursor() as cursor:
                for stmt in statements:
                    try:
                        cursor.execute(stmt)
                    except Exception as e:
                        msg = str(e).lower()
                        if "duplicate column" in msg or "duplicate key name" in msg:
                            continue
                        logger.debug(f"Duplicate-review schema upgrade skipped ({stmt[:70]}...): {e}")
            conn.close()
        except Exception as e:
            logger.error(f"Failed to run duplicate-review schema upgrades: {e}")

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
                        media_files INT DEFAULT 0,
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

                for table in ("duplicate_group_candidates", "duplicate_groups"):
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
                               member_count: int, mount: str, folder_path: str,
                               scope_key: str = None) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO duplicate_groups
                    (group_id, title_key, scope_key, member_count, mount, folder_path)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        title_key = VALUES(title_key),
                        scope_key = VALUES(scope_key),
                        member_count = VALUES(member_count),
                        mount = VALUES(mount),
                        folder_path = VALUES(folder_path),
                        updated_at = CURRENT_TIMESTAMP
                """, (group_id, title_key, scope_key, member_count, mount, folder_path))
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to insert duplicate group {group_id}: {e}")
            return False

    def insert_candidate(self, group_id: str, file_id: str, full_path: str,
                         mount: str, title_score: float, movie_score: float,
                         artist_score: float, overall_score: float,
                         confidence: str, status: str, stats_json: dict,
                         file_name: str = None, resolution: str = None,
                         rank_in_group: int = 0, is_primary: bool = False,
                         song_title: str = None, movie_or_album: str = None,
                         artists: list = None) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO duplicate_group_candidates
                    (group_id, file_id, full_path, file_name, mount, resolution,
                     rank_in_group, is_primary, song_title, movie_or_album, artists_json,
                     title_score, movie_score, artist_score, overall_score,
                     confidence, status, stats_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        full_path = VALUES(full_path),
                        file_name = VALUES(file_name),
                        mount = VALUES(mount),
                        resolution = VALUES(resolution),
                        rank_in_group = VALUES(rank_in_group),
                        is_primary = VALUES(is_primary),
                        song_title = VALUES(song_title),
                        movie_or_album = VALUES(movie_or_album),
                        artists_json = VALUES(artists_json),
                        title_score = VALUES(title_score),
                        movie_score = VALUES(movie_score),
                        artist_score = VALUES(artist_score),
                        overall_score = VALUES(overall_score),
                        confidence = VALUES(confidence),
                        status = VALUES(status),
                        stats_json = VALUES(stats_json),
                        updated_at = CURRENT_TIMESTAMP
                """, (group_id, file_id, full_path,
                      file_name or os.path.basename(full_path), mount, resolution,
                      rank_in_group, 1 if is_primary else 0, song_title, movie_or_album,
                      json.dumps(artists or []), title_score, movie_score,
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

    @staticmethod
    def _decode_candidate(row: dict) -> dict:
        for key, default in (("stats_json", {}), ("artists_json", [])):
            raw = row.get(key)
            if isinstance(raw, str):
                try:
                    row[key] = json.loads(raw)
                except Exception:
                    row[key] = default
        row["artists"] = row.get("artists_json") or []
        return row

    def _fetch_candidates(self, cursor, group_id: str) -> list:
        cursor.execute("""
            SELECT * FROM duplicate_group_candidates
            WHERE group_id = %s
            ORDER BY rank_in_group ASC, overall_score DESC
        """, (group_id,))
        return [self._decode_candidate(row) for row in cursor.fetchall()]

    _GROUP_SORT_COLUMNS = {
        "count": "g.member_count",
        "member_count": "g.member_count",
        "candidate_count": "g.member_count",
        "updated_at": "g.updated_at",
        "created_at": "g.created_at",
        "title": "g.title_key",
    }

    def get_duplicate_groups(self, mount: str = None, folder: str = None,
                             status: str = None, reviewed: str = None,
                             sort_by: str = "updated_at", sort_dir: str = "desc",
                             limit: int = 100, offset: int = 0) -> dict:
        """
        Fetch groups with optional mount/folder/candidate-status/reviewed
        filters, sorted by `sort_by` (count/updated_at/created_at/title).

        Returns {"items": [...], "total": N, "unresolved_total": M} where
        `unresolved_total` ignores the `reviewed` filter (but still honours
        mount/folder/status) so the UI can always show how many groups still
        need review, independent of whatever filter is currently applied.
        """
        empty = {"items": [], "total": 0, "unresolved_total": 0}
        if not self.enabled:
            return empty
        conn = self._get_connection()
        if not conn:
            return empty
        try:
            base_filters = []
            base_params = []
            if mount:
                base_filters.append("g.mount = %s")
                base_params.append(mount)
            folder_condition = ""
            if folder:
                folder_like = folder.rstrip('/') + '/%'
                folder_condition = (
                    " AND EXISTS (SELECT 1 FROM duplicate_group_candidates c "
                    "WHERE c.group_id = g.group_id AND c.full_path LIKE %s)"
                )
                base_params.append(folder_like)
            status_condition = ""
            if status:
                status_condition = (
                    " AND EXISTS (SELECT 1 FROM duplicate_group_candidates c "
                    "WHERE c.group_id = g.group_id AND c.status = %s)"
                )
                base_params.append(status)

            base_where = " WHERE 1=1"
            if base_filters:
                base_where += " AND " + " AND ".join(base_filters)
            base_where += folder_condition + status_condition

            reviewed_condition = ""
            reviewed_params = []
            if reviewed in ("RESOLVED", "UNRESOLVED"):
                reviewed_condition = " AND g.reviewed_status = %s"
                reviewed_params.append(reviewed)

            sort_column = self._GROUP_SORT_COLUMNS.get((sort_by or "").lower(), "g.updated_at")
            sort_direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

            list_query = (
                f"SELECT g.* FROM duplicate_groups g{base_where}{reviewed_condition} "
                f"ORDER BY {sort_column} {sort_direction}, g.updated_at DESC LIMIT %s OFFSET %s"
            )
            count_query = f"SELECT COUNT(*) AS cnt FROM duplicate_groups g{base_where}{reviewed_condition}"
            unresolved_query = (
                f"SELECT COUNT(*) AS cnt FROM duplicate_groups g{base_where} "
                "AND g.reviewed_status = 'UNRESOLVED'"
            )

            with conn.cursor() as cursor:
                cursor.execute(list_query, (*base_params, *reviewed_params, limit, offset))
                groups = cursor.fetchall()
                for grp in groups:
                    grp["candidates"] = self._fetch_candidates(cursor, grp["group_id"])

                cursor.execute(count_query, (*base_params, *reviewed_params))
                total = (cursor.fetchone() or {}).get("cnt", 0)

                cursor.execute(unresolved_query, tuple(base_params))
                unresolved_total = (cursor.fetchone() or {}).get("cnt", 0)
            conn.close()
            return {"items": groups, "total": total, "unresolved_total": unresolved_total}
        except Exception as e:
            logger.error(f"Failed to fetch duplicate groups: {e}")
            return empty

    def get_duplicate_group_by_id(self, group_id: str) -> dict:
        """Return a single group with its candidates."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM duplicate_groups WHERE group_id = %s", (group_id,))
                group = cursor.fetchone()
                if not group:
                    return {}
                group["candidates"] = self._fetch_candidates(cursor, group_id)
            conn.close()
            return group
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group {group_id}: {e}")
            return {}

    def get_duplicate_group_by_file(self, file_path: str) -> dict:
        """Return only the duplicates of the given file: the file itself plus
        the candidates it directly scored against (recorded in stats_json as
        `matched_file_ids`), never every other member of the folder scope."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM duplicate_group_candidates WHERE full_path = %s LIMIT 1",
                    (self._normalize_path(file_path),),
                )
                target = cursor.fetchone()
                if not target:
                    return {}
                group_id = target["group_id"]
                target = self._decode_candidate(target)

                cursor.execute("SELECT * FROM duplicate_groups WHERE group_id = %s", (group_id,))
                group = cursor.fetchone() or {"group_id": group_id}
                candidates = self._fetch_candidates(cursor, group_id)
            conn.close()

            matched_ids = set((target.get("stats_json") or {}).get("matched_file_ids") or [])
            matched_ids.add(target["file_id"])
            group["candidates"] = [c for c in candidates if c["file_id"] in matched_ids]
            group["member_count"] = len(group["candidates"])
            group["requested_file_path"] = file_path
            return group
        except Exception as e:
            logger.error(f"Failed to fetch duplicate group for file {file_path}: {e}")
            return {}

    def get_duplicate_group_ids_for_paths(self, file_paths: list[str]) -> dict:
        """Return dict mapping file_path (original, as passed in) -> group_id
        for files that belong to any duplicate group. `full_path` in the
        candidates table always uses forward slashes, while callers may pass
        OS-native (backslash) paths, so matching is done on normalized paths."""
        if not self.enabled or not file_paths:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            normalized_to_original = {self._normalize_path(p): p for p in file_paths}
            normalized_paths = list(normalized_to_original.keys())
            with conn.cursor() as cursor:
                placeholders = ','.join(['%s'] * len(normalized_paths))
                query = f"SELECT full_path, group_id FROM duplicate_group_candidates WHERE full_path IN ({placeholders})"
                cursor.execute(query, tuple(normalized_paths))
                rows = cursor.fetchall()
                result = {
                    normalized_to_original.get(row["full_path"], row["full_path"]): row["group_id"]
                    for row in rows
                }
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to get duplicate group ids for paths: {e}")
            return {}

    def find_existing_group_id_for_paths(self, mount: str, full_paths: list[str]) -> str | None:
        """Look up whether any of the given full_paths already belong to a
        duplicate group for this mount. Used before minting a brand-new
        group_id for a freshly (re)detected cluster: if the cluster's
        membership has only shifted slightly since the last run (a file
        added/removed, or one previously excluded via NOT_DUPLICATE now
        rejoining), the hash-derived group_id would differ from before and
        we'd otherwise end up creating a duplicate/orphaned group instead of
        updating the one reviewers already have decisions/history on.
        Returns the group_id most of the current members already share, or
        None if none of them belong to an existing group yet."""
        if not self.enabled or not full_paths:
            return None
        conn = self._get_connection()
        if not conn:
            return None
        try:
            normalized_paths = [self._normalize_path(p) for p in full_paths]
            with conn.cursor() as cursor:
                placeholders = ','.join(['%s'] * len(normalized_paths))
                query = (
                    f"SELECT group_id FROM duplicate_group_candidates "
                    f"WHERE mount = %s AND full_path IN ({placeholders})"
                )
                cursor.execute(query, tuple([mount] + normalized_paths))
                rows = cursor.fetchall()
            conn.close()
            if not rows:
                return None
            counts = Counter(row["group_id"] for row in rows)
            return counts.most_common(1)[0][0]
        except Exception as e:
            logger.error(f"Failed to find existing group for members: {e}")
            return None

    def get_group_candidate_statuses(self, group_id: str) -> dict:
        """Return {file_id: status} for a group's existing candidates, so a
        re-detected cluster being persisted into this same group can tell
        which members were already reviewed (NOT_DUPLICATE/REJECTED) and
        avoid clobbering that decision back to PENDING."""
        if not self.enabled or not group_id:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT file_id, status FROM duplicate_group_candidates WHERE group_id = %s",
                    (group_id,),
                )
                statuses = {row["file_id"]: row["status"] for row in cursor.fetchall()}
            conn.close()
            return statuses
        except Exception as e:
            logger.error(f"Failed to fetch candidate statuses for group {group_id}: {e}")
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
                """, (new_status, self._normalize_path(file_path)))
                updated = cursor.rowcount > 0
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Failed to update candidate status for {file_path}: {e}")
            return False

    def _recompute_group_reviewed_status(self, cursor, group_id: str) -> str:
        """A group is RESOLVED once at most one of its candidates is still
        'active' (anything other than NOT_DUPLICATE/REJECTED) — a group of
        one file can no longer be a duplicate of anything, so once a
        reviewer has marked away every other candidate the group is done."""
        cursor.execute(
            "SELECT status FROM duplicate_group_candidates WHERE group_id = %s",
            (group_id,),
        )
        rows = cursor.fetchall()
        active = [r for r in rows if r["status"] not in ("NOT_DUPLICATE", "REJECTED")]
        new_status = "RESOLVED" if len(active) <= 1 else "UNRESOLVED"
        cursor.execute(
            """UPDATE duplicate_groups
               SET reviewed_status = %s,
                   resolved_at = CASE WHEN %s = 'RESOLVED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                   updated_at = CURRENT_TIMESTAMP
               WHERE group_id = %s""",
            (new_status, new_status, group_id),
        )
        return new_status

    def mark_candidate_not_duplicate(self, file_path: str) -> dict:
        """Marks one candidate as NOT_DUPLICATE instead of deleting its row,
        so the group/candidate record is kept for the review UI but the file
        is excluded from being re-reported by future detection runs (see
        `get_ignored_candidate_paths`). Recomputes the parent group's
        reviewed_status afterwards."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            normalized = self._normalize_path(file_path)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT group_id FROM duplicate_group_candidates WHERE full_path = %s",
                    (normalized,),
                )
                row = cursor.fetchone()
                if not row:
                    return {}
                group_id = row["group_id"]
                cursor.execute(
                    """UPDATE duplicate_group_candidates
                       SET status = 'NOT_DUPLICATE', marked_not_duplicate_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE full_path = %s""",
                    (normalized,),
                )
                updated = cursor.rowcount > 0
                group_status = self._recompute_group_reviewed_status(cursor, group_id)
            conn.close()
            return {"updated": updated, "group_id": group_id, "group_reviewed_status": group_status}
        except Exception as e:
            logger.error(f"Failed to mark candidate not-duplicate for {file_path}: {e}")
            return {}

    def unmark_candidate_not_duplicate(self, file_path: str) -> dict:
        """Undo: reverts a NOT_DUPLICATE (or legacy REJECTED) candidate back
        to PENDING so it re-enters review and future detection runs."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            normalized = self._normalize_path(file_path)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT group_id FROM duplicate_group_candidates WHERE full_path = %s",
                    (normalized,),
                )
                row = cursor.fetchone()
                if not row:
                    return {}
                group_id = row["group_id"]
                cursor.execute(
                    """UPDATE duplicate_group_candidates
                       SET status = 'PENDING', marked_not_duplicate_at = NULL,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE full_path = %s""",
                    (normalized,),
                )
                updated = cursor.rowcount > 0
                group_status = self._recompute_group_reviewed_status(cursor, group_id)
            conn.close()
            return {"updated": updated, "group_id": group_id, "group_reviewed_status": group_status}
        except Exception as e:
            logger.error(f"Failed to unmark candidate not-duplicate for {file_path}: {e}")
            return {}

    def get_ignored_candidate_paths(self, mount: str) -> set:
        """Full paths (normalized, forward-slash) of files a reviewer has
        marked NOT_DUPLICATE (or the legacy REJECTED) for this mount — these
        are permanently excluded from future automatic duplicate detection
        so a resolved decision doesn't get re-reported on the next run."""
        if not self.enabled:
            return set()
        conn = self._get_connection()
        if not conn:
            return set()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT full_path FROM duplicate_group_candidates "
                    "WHERE mount = %s AND status IN ('NOT_DUPLICATE','REJECTED')",
                    (mount,),
                )
                paths = {row["full_path"] for row in cursor.fetchall()}
            conn.close()
            return paths
        except Exception as e:
            logger.error(f"Failed to fetch ignored candidate paths for mount {mount}: {e}")
            return set()

    def reset_active_duplicate_groups_for_mount(self, mount: str) -> int:
        """Ahead of a fresh detection pass: clears PENDING/DUPLICATE candidate
        rows (and any group left with none) for this mount, while preserving
        NOT_DUPLICATE/REJECTED rows and their parent group so reviewer
        decisions survive across runs instead of being wiped and re-reported.
        Returns the number of candidate rows cleared."""
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cleared = 0
            with conn.cursor() as cursor:
                cursor.execute("SELECT group_id FROM duplicate_groups WHERE mount = %s", (mount,))
                group_ids = [row["group_id"] for row in cursor.fetchall()]
                for group_id in group_ids:
                    cursor.execute(
                        "DELETE FROM duplicate_group_candidates "
                        "WHERE group_id = %s AND status IN ('PENDING','DUPLICATE')",
                        (group_id,),
                    )
                    cleared += cursor.rowcount
                    cursor.execute(
                        "SELECT COUNT(*) AS cnt FROM duplicate_group_candidates WHERE group_id = %s",
                        (group_id,),
                    )
                    remaining = (cursor.fetchone() or {}).get("cnt", 0)
                    if remaining == 0:
                        cursor.execute("DELETE FROM duplicate_groups WHERE group_id = %s", (group_id,))
                    else:
                        cursor.execute(
                            "UPDATE duplicate_groups SET member_count = %s, updated_at = CURRENT_TIMESTAMP "
                            "WHERE group_id = %s",
                            (remaining, group_id),
                        )
            conn.close()
            return cleared
        except Exception as e:
            logger.error(f"Failed to reset active duplicate groups for mount {mount}: {e}")
            return 0

    def get_file_details_by_paths(self, file_paths: list[str]) -> dict:
        """Return {normalized_path: media_files row (metadata decoded)} so
        duplicate candidates can be shown with size / resolution / duration."""
        if not self.enabled or not file_paths:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            normalized = list({self._normalize_path(p) for p in file_paths if p})
            result: dict = {}
            with conn.cursor() as cursor:
                chunk_size = 500
                for i in range(0, len(normalized), chunk_size):
                    chunk = normalized[i:i + chunk_size]
                    placeholders = ','.join(['%s'] * len(chunk))
                    cursor.execute(
                        "SELECT file_path, file_name, file_size, jellyfin_id, metadata_json "
                        f"FROM media_files WHERE REPLACE(file_path, '\\\\', '/') IN ({placeholders})",
                        chunk,
                    )
                    for row in cursor.fetchall():
                        raw = row.pop("metadata_json", None)
                        try:
                            row["metadata"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
                        except Exception:
                            row["metadata"] = {}
                        result[self._normalize_path(row["file_path"])] = row
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to fetch media_files details for paths: {e}")
            return {}

    def get_file_details_for_duplicate_candidates(self, candidates: list[dict]) -> dict:
        """Return media_files details keyed by duplicate candidate file_id and path."""
        if not self.enabled or not candidates:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            file_ids = list({c.get("file_id") for c in candidates if c.get("file_id")})
            normalized_paths = list({
                self._normalize_path(c.get("full_path") or c.get("file_path"))
                for c in candidates
                if c.get("full_path") or c.get("file_path")
            })
            result: dict = {}
            with conn.cursor() as cursor:
                conditions = []
                params = []
                if file_ids:
                    conditions.append(f"id IN ({','.join(['%s'] * len(file_ids))})")
                    params.extend(file_ids)
                if normalized_paths:
                    conditions.append(f"REPLACE(file_path, '\\\\', '/') IN ({','.join(['%s'] * len(normalized_paths))})")
                    params.extend(normalized_paths)
                if not conditions:
                    return {}

                cursor.execute(
                    "SELECT id, file_path, file_name, file_size, jellyfin_id, metadata_json, mtime "
                    f"FROM media_files WHERE {' OR '.join(conditions)}",
                    tuple(params),
                )
                for row in cursor.fetchall():
                    raw = row.pop("metadata_json", None)
                    try:
                        row["metadata"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except Exception:
                        row["metadata"] = {}
                    # add mtime to the metadata dict so the UI can show it without needing to merge with the row
                    row["metadata"]["mtime"] = row["mtime"]
                    result[row["id"]] = row
                    result[self._normalize_path(row["file_path"])] = row
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to fetch duplicate candidate details: {e}")
            return {}

    def delete_duplicate_group(self, group_id: str) -> bool:
        """Remove a group and all of its candidate rows (files are untouched)."""
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM duplicate_group_candidates WHERE group_id = %s", (group_id,))
                cursor.execute("DELETE FROM duplicate_groups WHERE group_id = %s", (group_id,))
                deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete duplicate group {group_id}: {e}")
            return False

    def delete_duplicate_candidate(self, file_path: str) -> dict:
        """Drop a single candidate row. When the owning group is left with one
        member or none it is removed too, so the file stops being flagged."""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            normalized = self._normalize_path(file_path)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT group_id FROM duplicate_group_candidates WHERE full_path = %s",
                    (normalized,),
                )
                group_ids = {row["group_id"] for row in cursor.fetchall()}
                cursor.execute(
                    "DELETE FROM duplicate_group_candidates WHERE full_path = %s",
                    (normalized,),
                )
                removed = cursor.rowcount

                groups_removed = []
                for group_id in group_ids:
                    cursor.execute(
                        "SELECT COUNT(*) AS cnt FROM duplicate_group_candidates WHERE group_id = %s",
                        (group_id,),
                    )
                    remaining = (cursor.fetchone() or {}).get("cnt", 0)
                    if remaining <= 1:
                        cursor.execute("DELETE FROM duplicate_group_candidates WHERE group_id = %s", (group_id,))
                        cursor.execute("DELETE FROM duplicate_groups WHERE group_id = %s", (group_id,))
                        groups_removed.append(group_id)
                    else:
                        cursor.execute(
                            "UPDATE duplicate_groups SET member_count = %s, updated_at = CURRENT_TIMESTAMP WHERE group_id = %s",
                            (remaining, group_id),
                        )
            conn.close()
            return {
                "candidates_removed": removed,
                "groups_removed": groups_removed,
                "group_ids": list(group_ids),
            }
        except Exception as e:
            logger.error(f"Failed to delete duplicate candidate {file_path}: {e}")
            return {}

    def delete_llm_metadata(self, file_name: str) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM llm_parsed_metadata WHERE file_name = %s",
                    (self._normalize_file_key(file_name),),
                )
                count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed to delete llm_parsed_metadata for {file_name}: {e}")
            return 0

    # ----------------------------------------------------------------------
    # Truncate tables (used by admin clean)
    # ----------------------------------------------------------------------
    def truncate_tables(self, tables: list = None) -> dict:
        """Truncate given tables; default list: media_files,
        duplicate_group_candidates, duplicate_groups (if exists)."""
        if not self.enabled:
            return {}
        default_tables = ["media_files",
                          "duplicate_group_candidates", "duplicate_groups"]
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
                INSERT INTO media_files 
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
            old_name = os.path.basename(old_path)
            old_norm = self._normalize_path(old_path)
            new_norm = self._normalize_path(new_path)
            old_key = self._normalize_file_key(old_name)
            new_key = self._normalize_file_key(new_name)
            old_file_id = generate_file_id(old_path)
            new_file_id = generate_file_id(new_path)

            with conn.cursor() as cursor:
                # 1. Update media_files
                if relative_path:
                    query = "UPDATE media_files SET file_path=%s, file_name=%s, relative_path=%s WHERE file_path=%s OR file_name=%s"
                    cursor.execute(query, (new_path, new_name, relative_path, old_path, old_name))
                else:
                    query = "UPDATE media_files SET file_path=%s, file_name=%s WHERE file_path=%s OR file_name=%s"
                    cursor.execute(query, (new_path, new_name, old_path, old_name))
                count = cursor.rowcount

                # 2. Update llm_parsed_metadata (both file_name and full_path)
                if old_key != new_key:
                    cursor.execute("DELETE FROM llm_parsed_metadata WHERE file_name = %s", (new_key,))
                    cursor.execute(
                        "UPDATE llm_parsed_metadata SET file_name=%s, full_path=%s WHERE file_name=%s OR full_path=%s",
                        (new_key, new_norm, old_key, old_norm)
                    )
                else:
                    cursor.execute(
                        "UPDATE llm_parsed_metadata SET full_path=%s WHERE file_name=%s OR full_path=%s",
                        (new_norm, old_key, old_norm)
                    )

                # 3. Update duplicate_group_candidates (file_id, full_path, file_name)
                cursor.execute(
                    """
                    UPDATE duplicate_group_candidates
                    SET full_path = %s, file_name = %s, file_id = %s
                    WHERE full_path = %s OR file_name = %s OR file_id = %s
                    """,
                    (new_norm, new_name, new_file_id, old_norm, old_name, old_file_id)
                )

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
            file_name = os.path.basename(file_path)
            normalized_path = self._normalize_path(file_path)
            file_key = self._normalize_file_key(file_name)
            file_id = generate_file_id(file_path)

            with conn.cursor() as cursor:
                # 1. Delete from media_files
                query = "DELETE FROM media_files WHERE file_path=%s OR file_name=%s OR id=%s"
                cursor.execute(query, (file_path, file_name, file_id))
                count = cursor.rowcount

                # 2. Delete from llm_parsed_metadata
                cursor.execute(
                    "DELETE FROM llm_parsed_metadata WHERE file_name=%s OR full_path=%s OR file_name=%s",
                    (file_name, normalized_path, file_key)
                )

                # 3. Delete candidate from duplicate_group_candidates and prune empty duplicate_groups
                cursor.execute(
                    "SELECT group_id FROM duplicate_group_candidates WHERE full_path=%s OR file_name=%s OR file_id=%s",
                    (normalized_path, file_name, file_id)
                )
                group_ids = {row["group_id"] for row in cursor.fetchall()}

                cursor.execute(
                    "DELETE FROM duplicate_group_candidates WHERE full_path=%s OR file_name=%s OR file_id=%s",
                    (normalized_path, file_name, file_id)
                )

                for group_id in group_ids:
                    cursor.execute(
                        "SELECT COUNT(*) AS cnt FROM duplicate_group_candidates WHERE group_id = %s",
                        (group_id,)
                    )
                    remaining = (cursor.fetchone() or {}).get("cnt", 0)
                    if remaining <= 1:
                        cursor.execute("DELETE FROM duplicate_group_candidates WHERE group_id = %s", (group_id,))
                        cursor.execute("DELETE FROM duplicate_groups WHERE group_id = %s", (group_id,))
                    else:
                        cursor.execute(
                            "UPDATE duplicate_groups SET member_count = %s, updated_at = CURRENT_TIMESTAMP WHERE group_id = %s",
                            (remaining, group_id)
                        )

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
                query = "SELECT id, file_path, relative_path, file_size, mtime, status, vector_id FROM media_files WHERE mount=%s"
                cursor.execute(query, (mount,))
                rows = cursor.fetchall()
            conn.close()
            return {row["relative_path"] or row["file_path"]: row for row in rows}
        except Exception as e:
            logger.error(f"MySQL fetch failed for mount {mount}: {e}")
            return {}
        
    def get_tracked_files_map(self, mount: str) -> dict:
        """Returns map: relative_path -> {mtime, file_size, id, vector_id, status, jellyfin_id, metadata_json}"""
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            with conn.cursor() as cursor:
                query = "SELECT id, relative_path, file_path, file_size, mtime, status, vector_id, jellyfin_id, metadata_json FROM media_files WHERE mount=%s"
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
                cursor.execute(f"DELETE FROM media_files WHERE file_path IN ({format_strings})", tuple(file_paths))
                count = cursor.rowcount
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed deleting records: {e}")
            return 0

    def truncate_media_files(self) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM media_files")
                removed = (cursor.fetchone() or {}).get("cnt", 0)
                cursor.execute("TRUNCATE TABLE media_files")
            conn.close()
            logger.info(f"Truncated 'media_files' table ({removed} rows removed)")
            return removed
        except Exception as e:
            logger.error(f"Failed to truncate media_files table: {e}")
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
                (job_id, mount_name, status, total_files, media_files, added_files, updated_files, skipped_files, failed_files, cleaned_orphans, eta_seconds, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    total_files = VALUES(total_files),
                    media_files = VALUES(media_files),
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
                    job_info.get("total_files", 0), job_info.get("media_files", 0),
                    job_info.get("added_files", 0), job_info.get("updated_files", 0),
                    job_info.get("skipped_files", 0), job_info.get("failed_files", 0),
                    job_info.get("cleaned_orphans", 0), job_info.get("eta_seconds", 0),
                    job_info.get("error")
                ))
            conn.close()
        except Exception as e:
            logger.error(f"MySQL upsert job failed for {job_info.get('job_id')}: {e}")

    # ----------------------------------------------------------------------
    # LLM-parsed metadata cache (song/movie/artist), keyed by file name so it
    # is reusable independent of which mount/path a file currently lives at.
    # ----------------------------------------------------------------------
    @staticmethod
    def _normalize_file_key(file_name: str) -> str:
        return os.path.splitext(os.path.basename(file_name))[0].strip().lower()

    @staticmethod
    def _normalize_path(path: str) -> str:
        # duplicate_group_candidates.full_path is always stored with forward
        # slashes; callers may pass OS-native (backslash) paths on Windows.
        return path.replace("\\", "/") if path else path

    def get_llm_metadata(self, file_name: str) -> dict | None:
        if not self.enabled:
            return None
        conn = self._get_connection()
        if not conn:
            return None
        try:
            key = self._normalize_file_key(file_name)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM llm_parsed_metadata WHERE file_name = %s", (key,)
                )
                row = cursor.fetchone()
            conn.close()
            if not row:
                return None
            try:
                row["artists"] = json.loads(row.pop("artists_json") or "[]")
            except Exception:
                row["artists"] = []
            return row
        except Exception as e:
            logger.error(f"Failed to fetch llm_parsed_metadata for {file_name}: {e}")
            return None

    def get_llm_metadata_bulk(self, file_names: list[str]) -> dict:
        """Batched version of get_llm_metadata: returns {normalized_key: row}
        so duplicate detection doesn't open a connection per file."""
        if not self.enabled or not file_names:
            return {}
        keys = sorted({self._normalize_file_key(name) for name in file_names})
        conn = self._get_connection()
        if not conn:
            return {}
        result: dict = {}
        try:
            with conn.cursor() as cursor:
                chunk_size = 500
                for i in range(0, len(keys), chunk_size):
                    chunk = keys[i:i + chunk_size]
                    placeholders = ",".join(["%s"] * len(chunk))
                    cursor.execute(
                        f"SELECT * FROM llm_parsed_metadata WHERE file_name IN ({placeholders})",
                        chunk,
                    )
                    for row in cursor.fetchall():
                        try:
                            row["artists"] = json.loads(row.pop("artists_json") or "[]")
                        except Exception:
                            row["artists"] = []
                        result[row["file_name"]] = row
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to bulk fetch llm_parsed_metadata: {e}")
            return result

    def upsert_llm_metadata(
        self,
        file_name: str,
        full_path: str,
        song_title: str,
        movie_or_album: str | None,
        artists: list[str],
        model_name: str,
        source_endpoint: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            key = self._normalize_file_key(file_name)
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO llm_parsed_metadata
                    (file_name, full_path, song_title, movie_or_album, artists_json, model_name, source_endpoint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        full_path = VALUES(full_path),
                        song_title = VALUES(song_title),
                        movie_or_album = VALUES(movie_or_album),
                        artists_json = VALUES(artists_json),
                        model_name = VALUES(model_name),
                        source_endpoint = VALUES(source_endpoint),
                        parsed_at = CURRENT_TIMESTAMP
                    """,
                    (key, full_path, song_title, movie_or_album,
                     json.dumps(artists or []), model_name, source_endpoint),
                )
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to upsert llm_parsed_metadata for {file_name}: {e}")
            return False

    def get_files_needing_llm_parse(self, mount: str = None, limit: int = 100,
                                     after_file_name: str = None) -> list[dict]:
        """Files tracked in media_files that have no llm_parsed_metadata
        row yet (or whose row is older than the file's last index time).
        Paginated by file_name for cheap checkpoint/resume."""
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            params = []
            query = """
                SELECT p.id, p.file_path, p.file_name, p.mount
                FROM media_files p
                LEFT JOIN llm_parsed_metadata m
                    ON m.file_name = LOWER(REGEXP_REPLACE(p.file_name, '\\.[^.]+$', ''))
                WHERE m.file_name IS NULL
            """
            if mount:
                query += " AND p.mount = %s"
                params.append(mount)
            if after_file_name:
                query += " AND p.file_name > %s"
                params.append(after_file_name)
            query += " ORDER BY p.file_name ASC LIMIT %s"
            params.append(limit)
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch files needing LLM parse: {e}")
            return []

    def count_files_needing_llm_parse(self, mount: str = None) -> int:
        if not self.enabled:
            return 0
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            params = []
            query = """
                SELECT COUNT(*) AS cnt
                FROM media_files p
                LEFT JOIN llm_parsed_metadata m
                    ON m.file_name = LOWER(REGEXP_REPLACE(p.file_name, '\\.[^.]+$', ''))
                WHERE m.file_name IS NULL
            """
            if mount:
                query += " AND p.mount = %s"
                params.append(mount)
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                row = cursor.fetchone()
            conn.close()
            return (row or {}).get("cnt", 0)
        except Exception as e:
            logger.error(f"Failed to count files needing LLM parse: {e}")
            return 0

    # ----------------------------------------------------------------------
    # Background jobs (pause/resume-able llm_parse & duplicate_detect runs)
    # ----------------------------------------------------------------------
    def create_job(self, job_id: str, job_type: str, mount_name: str | None, total_items: int = 0) -> bool:
        if not self.enabled:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO background_jobs (job_id, job_type, mount_name, status, total_items)
                    VALUES (%s, %s, %s, 'PENDING', %s)
                    ON DUPLICATE KEY UPDATE total_items = VALUES(total_items)
                    """,
                    (job_id, job_type, mount_name, total_items),
                )
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to create background job {job_id}: {e}")
            return False

    def update_job(self, job_id: str, **fields) -> bool:
        if not self.enabled or not fields:
            return False
        conn = self._get_connection()
        if not conn:
            return False
        try:
            set_clause = ", ".join(f"{k} = %s" for k in fields)
            params = list(fields.values()) + [job_id]
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE background_jobs SET {set_clause} WHERE job_id = %s", params
                )
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Failed to update background job {job_id}: {e}")
            return False

    def request_job_status(self, job_id: str, requested_status: str) -> bool:
        """Called by the API layer to ask a running job to pause/resume/cancel.
        The job runner polls `requested_status` between batches."""
        return self.update_job(job_id, requested_status=requested_status)

    def get_job(self, job_id: str) -> dict | None:
        if not self.enabled:
            return None
        conn = self._get_connection()
        if not conn:
            return None
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM background_jobs WHERE job_id = %s", (job_id,))
                row = cursor.fetchone()
            conn.close()
            return row
        except Exception as e:
            logger.error(f"Failed to fetch background job {job_id}: {e}")
            return None

    def list_jobs(self, job_type: str = None, limit: int = 50) -> list[dict]:
        if not self.enabled:
            return []
        conn = self._get_connection()
        if not conn:
            return []
        try:
            params = []
            query = "SELECT * FROM background_jobs"
            if job_type:
                query += " WHERE job_type = %s"
                params.append(job_type)
            query += " ORDER BY updated_at DESC LIMIT %s"
            params.append(limit)
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"Failed to list background jobs: {e}")
            return []

    def get_mount_action_statuses(self, mount_names: list[str]) -> dict:
        """
        Returns a dict mapping mount_name -> {
            'indexing': {'status', 'updated_at', 'error_message'} or None,
            'llm_parse': {'status', 'updated_at', 'last_error', 'processed_items', 'total_items'} or None,
            'duplicate_detect': same structure as llm_parse
        }
        """
        if not self.enabled:
            return {}
        conn = self._get_connection()
        if not conn:
            return {}
        result = {}
        try:
            with conn.cursor() as cursor:
                for mount in mount_names:
                    # Indexing
                    cursor.execute("""
                        SELECT status, updated_at, error_message
                        FROM indexing_jobs
                        WHERE mount_name = %s
                        ORDER BY updated_at DESC LIMIT 1
                    """, (mount,))
                    idx = cursor.fetchone()

                    # LLM parse
                    cursor.execute("""
                        SELECT status, updated_at, last_error, processed_items, total_items
                        FROM background_jobs
                        WHERE job_type = 'llm_parse' AND mount_name = %s
                        ORDER BY updated_at DESC LIMIT 1
                    """, (mount,))
                    llm = cursor.fetchone()

                    # Duplicate detect
                    cursor.execute("""
                        SELECT status, updated_at, last_error, processed_items, total_items
                        FROM background_jobs
                        WHERE job_type = 'duplicate_detect' AND mount_name = %s
                        ORDER BY updated_at DESC LIMIT 1
                    """, (mount,))
                    dup = cursor.fetchone()

                    result[mount] = {
                        'indexing': idx,
                        'llm_parse': llm,
                        'duplicate_detect': dup
                    }
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Failed to get mount action statuses: {e}")
            return {}



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
        status: str = "INDEXED",
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
                        child["status"] = status
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
                if not found:
                    new_file_node = {
                        "name": part,
                        "type": "file",
                        "path": rel_path,
                        "status": status,
                        "vector_id": vector_id,
                        "jellyfin_id": jellyfin_id,
                        "primary_image_tag": primary_image_tag,
                        "width": width,
                        "height": height,
                        "duration": duration,
                    }
                    curr.setdefault("children", []).append(new_file_node)
                    found = True
            else:
                matched_dir = None
                for child in curr.get("children", []):
                    if child.get("name") == part and child.get("type") == "folder":
                        matched_dir = child
                        break
                if not matched_dir:
                    curr_path = "/".join(parts[: i + 1])
                    matched_dir = {
                        "name": part,
                        "type": "folder",
                        "path": curr_path,
                        "children": [],
                    }
                    curr.setdefault("children", []).append(matched_dir)
                curr = matched_dir

        if found:
            self.set_mount_tree(mount_name, tree)

    def rename_file_node(self, old_path: str, new_path: str):
        if not self.enabled or not self.client:
            return
        try:
            old_name = os.path.basename(old_path)
            new_name = os.path.basename(new_path)
            keys = list(self.client.scan_iter(match="mount:tree:*"))
            for key in keys:
                data = self.client.get(key)
                if not data:
                    continue
                tree = json.loads(data)
                modified = self._rename_node_in_tree(tree, old_path, new_path, old_name, new_name)
                if modified:
                    self.client.set(key, json.dumps(tree))
        except Exception as e:
            logger.error(f"Failed to rename Redis node from {old_path} to {new_path}: {e}")

    def _rename_node_in_tree(self, node: dict, old_path: str, new_path: str, old_name: str, new_name: str) -> bool:
        modified = False
        children = node.get("children", [])
        for child in children:
            if child.get("type") == "file":
                child_path = child.get("path", "")
                child_name = child.get("name", "")
                if child_path == old_path or child_name == old_name or child_path.endswith(f"/{old_name}"):
                    child["name"] = new_name
                    if child_path.endswith(old_name):
                        child["path"] = child_path[:-len(old_name)] + new_name
                    else:
                        child["path"] = new_path
                    modified = True
            elif child.get("type") == "folder":
                if self._rename_node_in_tree(child, old_path, new_path, old_name, new_name):
                    modified = True
        return modified

    def remove_file_node(self, file_path: str) -> int:
        if not self.enabled or not self.client:
            return 0
        removed_count = 0
        try:
            file_name = os.path.basename(file_path)
            keys = list(self.client.scan_iter(match="mount:tree:*"))
            for key in keys:
                data = self.client.get(key)
                if not data:
                    continue
                tree = json.loads(data)
                count, modified = self._remove_node_from_tree(tree, file_path, file_name)
                if modified:
                    self.client.set(key, json.dumps(tree))
                    removed_count += count
        except Exception as e:
            logger.error(f"Failed to remove Redis node for {file_path}: {e}")
        return removed_count

    def _remove_node_from_tree(self, node: dict, file_path: str, file_name: str) -> tuple[int, bool]:
        modified = False
        removed = 0
        children = node.get("children", [])
        new_children = []
        for child in children:
            if child.get("type") == "file":
                child_path = child.get("path", "")
                child_name = child.get("name", "")
                if child_path == file_path or child_name == file_name or child_path.endswith(f"/{file_name}"):
                    modified = True
                    removed += 1
                else:
                    new_children.append(child)
            elif child.get("type") == "folder":
                sub_removed, sub_mod = self._remove_node_from_tree(child, file_path, file_name)
                if sub_mod:
                    modified = True
                    removed += sub_removed
                new_children.append(child)
            else:
                new_children.append(child)
        if modified:
            node["children"] = new_children
        return removed, modified

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