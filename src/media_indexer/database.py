import os
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
        # Prefix tokenizer enables partial/single-word matches (e.g. "21", "aveng")
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
        """Merges payload_updates into every point currently pointing at file_path."""
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
        """Removes every point but keeps the collection and its vector config."""
        removed = self.count_items()
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=models.Filter())
        )
        logger.info(f"Truncated collection '{self.collection_name}' ({removed} points)")
        return removed

    def reset_collection(self) -> int:
        """Drops and recreates the collection for a clean, fresh index."""
        removed = self.count_items()
        self.client.delete_collection(collection_name=self.collection_name)
        self._ensure_collection()
        logger.info(f"Recreated collection '{self.collection_name}' ({removed} points dropped)")
        return removed

    def search_vectors(self, query_vector: list[float], limit: int = 10):
      response = self.client.query_points(
          collection_name=self.collection_name,  # or your collection variable name
          query=query_vector,
          limit=limit,
      )
      # query_points returns a QueryResponse object containing a .points list
      return response.points

    def keyword_search(self, query: str, limit: int = 50):
        """Full-text/prefix match on normalized_title or file_name, for single-word/partial searches."""
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

db_instance = VectorDatabase()