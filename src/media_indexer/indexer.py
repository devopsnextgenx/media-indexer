import os
import logging
import requests
from sentence_transformers import SentenceTransformer
from media_indexer.config import settings
from media_indexer.database import db_instance, mysql_db_instance
from media_indexer.utils import generate_file_id, normalize_text, build_media_metadata
from media_indexer.jellyfin import format_ticks, normalize_mount_path

logger = logging.getLogger(__name__)

class MediaIndexer:
    def __init__(self):
        logger.info(f"Loading embedding model: {settings.embedding.model_name}")
        self.model = SentenceTransformer(settings.embedding.model_name)
        self.supported_exts = {".mp4", ".mkv", ".avi", ".webm", ".mp3", ".m4a", ".flv"}

    def fetch_jellyfin_metadata(self, filename_search: str) -> dict:
        jf = settings.jellyfin
        if not jf.enabled or not jf.api_key or not jf.url:
            return {}
        try:
            url = f"{jf.url.rstrip('/')}/Items"
            params = {
                "searchTerm": filename_search,
                "limit": 1,
                "recursive": "true",
                "api_key": jf.api_key
            }
            res = requests.get(url, params=params, timeout=5)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                if items:
                    item_id = items[0].get("Id")
                    detail_url = f"{jf.url.rstrip('/')}/Users/Items/{item_id}"
                    detail_res = requests.get(detail_url, params={"api_key": jf.api_key}, timeout=5)
                    data = detail_res.json() if detail_res.status_code == 200 else items[0]
                    
                    media_sources = data.get("MediaSources", [])
                    size = media_sources[0].get("Size") if media_sources else None
                    raw_ticks = data.get("RunTimeTicks")

                    return {
                        "jf_id": item_id,
                        "jf_name": data.get("Name"),
                        "jf_overview": data.get("Overview", ""),
                        "jf_genres": data.get("Genres", []),
                        "path": normalize_mount_path(data.get("Path", "")),
                        "width": data.get("Width"),
                        "height": data.get("Height"),
                        "primary_image_tag": data.get("ImageTags", {}).get("Primary"),
                        "size": size,
                        "runtime_ticks": raw_ticks,
                        "duration": format_ticks(raw_ticks),
                    }
        except Exception as e:
            logger.warning(f"Jellyfin API pull failed: {e}")
        return {}

    def scan_and_index(self) -> dict:
        base_dir = settings.mounts.base_dir
        if not os.path.exists(base_dir):
            return {"status": "error", "message": f"Mounts root directory {base_dir} does not exist"}

        indexed_count = 0
        skipped_count = 0

        for root, _, files in os.walk(base_dir):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in self.supported_exts:
                    continue

                full_path = os.path.join(root, file)
                point_id = generate_file_id(full_path)
                
                normalized_title = normalize_text(file)
                jf_meta = self.fetch_jellyfin_metadata(normalized_title)
                meta = build_media_metadata(full_path, jf_meta)

                embedding_input = f"Title: {normalized_title} File: {file} Resolution: {meta.get('resolution')} Overview: {jf_meta.get('jf_overview', '')}"
                vector = self.model.encode(embedding_input).tolist()

                final_path = jf_meta.get("path") or full_path
                mount_name = os.path.relpath(full_path, base_dir).split(os.sep)[0]
                rel_path = os.path.relpath(full_path, base_dir)

                payload = {
                    "file_path": final_path,
                    "file_name": file,
                    "normalized_title": normalized_title,
                    "mount": mount_name,
                    "relative_path": rel_path,
                    "metadata": meta,
                    "jellyfin": jf_meta
                }

                # Upsert into Qdrant Vector DB
                db_instance.upsert_media_item(
                    point_id=point_id,
                    vector=vector,
                    payload=payload
                )

                # Upsert into MySQL DB
                stat = os.stat(full_path) if os.path.exists(full_path) else None
                mysql_db_instance.upsert_file_record(
                    file_id=point_id,
                    file_path=final_path,
                    file_name=file,
                    relative_path=rel_path,
                    mount=mount_name,
                    file_size=stat.st_size if stat else meta.get("size", 0),
                    mtime=stat.st_mtime if stat else 0.0,
                    status="INDEXED",
                    vector_id=point_id,
                    jellyfin_id=jf_meta.get("jf_id"),
                    metadata=meta
                )

                indexed_count += 1

        logger.info(f"Indexing complete. Processed {indexed_count} media files.")
        return {"status": "success", "indexed": indexed_count, "skipped": skipped_count}

indexer_service = MediaIndexer()