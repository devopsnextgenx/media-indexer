import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional
import uuid
import yaml
import logging

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

from media_indexer.jellyfin import JellyfinClient
from media_indexer.config import settings
from media_indexer.database import db_instance, mysql_db_instance
from media_indexer.utils import build_media_metadata, normalize_text, generate_file_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MAX_COLLECTION_RECOVERY_ATTEMPTS = 3


class CollectionUnavailableError(RuntimeError):
    """Raised when the Qdrant collection is missing and cannot be recreated."""


class DirectoryTreeScanner:
    def __init__(
        self,
        mount_path: str,
        mount_name: str,
        jellyfin_client: Optional[JellyfinClient] = None,
        qdrant_client: Optional[QdrantClient] = None,
        embedding_model: Optional[Any] = None,
        collection_name: str = "media_library",
        folder_libraries: Optional[Dict[str, List[str]]] = None,
        media_type: str = "auto",
    ):
        p = Path(mount_path)
        self.mount_path = p if p.is_absolute() else Path("/media/storage") / mount_path
        self.mount_name = mount_name
        self.manifest_path = self.mount_path / f"manifest_{self.mount_name}.yml"
        self.jellyfin = jellyfin_client
        self.qdrant = qdrant_client
        self.embedding_model = embedding_model
        self.collection_name = collection_name
        self.media_type = media_type
        self.folder_libraries: Dict[str, List[str]] = folder_libraries or {}
        self._folder_keys = {f.lower(): f for f in self.folder_libraries}
        self._folder_caches: Dict[str, Dict[str, Any]] = {}
        self._mount_cache: Dict[str, Any] = {}
        self._collection_recovery_failures = 0
        self._events: Deque[Dict[str, Any]] = deque(maxlen=settings.indexing.log_buffer)
        self._event_seq = 0

        if self.qdrant:
            self._ensure_collection()

    def _emit(self, message: str, level: str = "info"):
        self._event_seq += 1
        self._events.append(
            {
                "seq": self._event_seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": level,
                "mount": self.mount_name,
                "message": message,
            }
        )

    def _events_since(self, seq: int) -> List[Dict[str, Any]]:
        return [event for event in self._events if event["seq"] > seq]

    def _resolve_folder(self, rel_path: str) -> str:
        parts = Path(rel_path).parts
        if len(parts) > 1:
            match = self._folder_keys.get(parts[0].lower())
            if match is not None:
                return match
        return ""

    async def _build_folder_caches(
        self, force: bool = False, manifest: Optional[Dict[str, Any]] = None
    ):
        if not self.jellyfin:
            return

        self._folder_caches = {}
        self._mount_cache = {}

        all_libraries = [lib for libs in self.folder_libraries.values() for lib in libs]
        job_info = manifest["job_info"] if manifest else None
        loaded_libraries: List[str] = []
        last_flush = 0.0

        if job_info is not None:
            job_info["status"] = "LOADING_LIBRARIES"
            job_info["libraries_total"] = len(all_libraries)
            job_info["libraries_loaded"] = 0
            job_info["current_library"] = all_libraries[0] if all_libraries else None
            job_info["library_items_loaded"] = 0
            job_info["library_items_total"] = None
            self._save_manifest(manifest)

        def on_library_progress(name, loaded, total, done):
            nonlocal last_flush
            if job_info is None:
                return
            job_info["current_library"] = name
            job_info["library_items_loaded"] = loaded
            job_info["library_items_total"] = total
            if done and name not in loaded_libraries:
                loaded_libraries.append(name)
                job_info["libraries_loaded"] = len(loaded_libraries)
                self._emit(
                    f"Library '{name}' loaded ({loaded} items) "
                    f"[{len(loaded_libraries)}/{len(all_libraries)}]"
                )
            now = time.time()
            if done or now - last_flush >= 0.5:
                last_flush = now
                self._save_manifest(manifest)

        for folder, libraries in self.folder_libraries.items():
            caches = await self.jellyfin.build_library_caches(
                libraries, force=force, on_progress=on_library_progress
            )
            merged: Dict[str, Any] = {}
            for library in libraries:
                for file_key, meta in caches.get(library, {}).items():
                    merged.setdefault(file_key, meta)
            self._folder_caches[folder] = merged
            for file_key, meta in merged.items():
                self._mount_cache.setdefault(file_key, meta)

        if job_info is not None:
            job_info["current_library"] = None
            job_info["library_items_loaded"] = len(self._mount_cache)
            job_info["library_items_total"] = len(self._mount_cache)
            self._save_manifest(manifest)

        logging.info(
            f"[{self.mount_name}] Jellyfin folder caches ready: "
            + ", ".join(
                f"{folder or '<root>'}={len(cache)}"
                for folder, cache in self._folder_caches.items()
            )
        )

    def _lookup_metadata(self, folder: str, file_name: str) -> Dict[str, Any]:
        key = file_name.lower()
        scoped = self._folder_caches.get(folder) or {}
        return scoped.get(key) or self._mount_cache.get(key) or {}

    def _ensure_collection(self) -> bool:
        try:
            collections = [c.name for c in self.qdrant.get_collections().collections]
            if self.collection_name not in collections:
                logging.info(f"Creating missing Qdrant collection: {self.collection_name}")
                self.qdrant.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=settings.embedding.dimension, distance=Distance.COSINE
                    ),
                )
            return True
        except Exception as e:
            logging.error(f"Error checking/creating Qdrant collection: {e}")
            return False

    @staticmethod
    def _is_missing_collection_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "doesn't exist" in message or "not found" in message

    def _recover_collection(self):
        if self._ensure_collection():
            self._collection_recovery_failures = 0
            return

        self._collection_recovery_failures += 1
        if self._collection_recovery_failures >= MAX_COLLECTION_RECOVERY_ATTEMPTS:
            raise CollectionUnavailableError(
                f"Qdrant collection '{self.collection_name}' is unavailable and could not be "
                f"created after {MAX_COLLECTION_RECOVERY_ATTEMPTS} attempts. Indexing aborted."
            )
        raise RuntimeError(
            f"Qdrant collection '{self.collection_name}' missing; recreate attempt "
            f"{self._collection_recovery_failures}/{MAX_COLLECTION_RECOVERY_ATTEMPTS} failed."
        )

    def _upsert_points(self, points: List[PointStruct]):
        if not points:
            return
        try:
            self.qdrant.upsert(collection_name=self.collection_name, points=points)
        except Exception as e:
            if not self._is_missing_collection_error(e):
                raise
            self._recover_collection()
            self.qdrant.upsert(collection_name=self.collection_name, points=points)

    def _fast_dir_walk(self, directory: Path) -> List[Dict[str, Any]]:
        valid_extensions = {
            ".mp4", ".mkv", ".avi", ".webm", ".flv", ".m4v", 
            ".wmv", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg",
            ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"
        }
        tree_entries = []

        def _walk(curr_path: Path):
            try:
                with os.scandir(curr_path) as entries:
                    for entry in sorted(entries, key=lambda e: e.name):
                        if entry.name.startswith("."):
                            continue

                        child_path = curr_path / entry.name

                        try:
                            if child_path.is_dir():
                                _walk(child_path)
                            elif child_path.is_file():
                                ext = child_path.suffix.lower()
                                if ext in valid_extensions:
                                    rel = str(child_path.relative_to(self.mount_path))
                                    stat = child_path.stat()
                                    tree_entries.append(
                                        {
                                            "path": rel,
                                            "folder": self._resolve_folder(rel),
                                            "status": "PENDING",
                                            "mtime": stat.st_mtime,
                                            "size": stat.st_size,
                                            "jellyfin_id": None,
                                            "library": None,
                                            "vector_id": None,
                                            "error": None,
                                        }
                                    )
                        except (PermissionError, OSError) as e:
                            logging.warning(f"Error accessing entry {child_path}: {e}")
                            continue
            except (PermissionError, OSError) as e:
                logging.warning(f"Error scanning directory {curr_path}: {e}")

        _walk(directory)
        return tree_entries

    def _save_manifest(self, data: Dict[str, Any]):
        data["job_info"]["last_updated"] = datetime.now(
            timezone.utc
        ).isoformat()
        
        temp_manifest_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(temp_manifest_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            temp_manifest_path.replace(self.manifest_path)
        except Exception:
            if temp_manifest_path.exists():
                temp_manifest_path.unlink()

    def load_or_create_manifest(
        self, force_rescan: bool = False, incremental_scan: bool = False
    ) -> Dict[str, Any]:
        """Loads directory tree, purges orphaned database entries, and filters unmodified files."""
        logging.info(f"Starting directory walk for mount: {self.mount_name} (incremental={incremental_scan}, force_rescan={force_rescan})")

        # 1. ONLY load existing manifest if NOT doing an incremental scan and NOT forcing rescan
        if not force_rescan and not incremental_scan and self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r") as f:
                    existing_manifest = yaml.safe_load(f)
                    if existing_manifest and "job_info" in existing_manifest:
                        return existing_manifest
            except Exception as e:
                logging.warning(f"Failed to read existing manifest: {e}")

        # 2. Re-walk the disk directory tree
        self._emit(f"Walking directory tree at {self.mount_path}")
        all_disk_entries = self._fast_dir_walk(self.mount_path)
        disk_map = {e["path"]: e for e in all_disk_entries}

        added_count = 0
        updated_count = 0
        skipped_count = 0
        cleaned_orphans_count = 0
        entries_to_process = []

        if incremental_scan:
            # Fetch tracked state directly from MySQL
            tracked_mysql = mysql_db_instance.get_tracked_files_map(self.mount_name)

            # Purge orphaned entries in DB that no longer exist on disk
            orphaned_paths = []
            for rel_path, rec in tracked_mysql.items():
                if rel_path not in disk_map:
                    full_del_path = str(self.mount_path / rel_path)
                    db_instance.delete_by_file_path(full_del_path)
                    orphaned_paths.append(full_del_path)

            if orphaned_paths:
                cleaned_orphans_count = mysql_db_instance.delete_records_by_paths(orphaned_paths)
                self._emit(f"Incremental scan purged {cleaned_orphans_count} orphaned file(s) from DBs.")

            # Filter entries: identify new vs modified vs unchanged
            for entry in all_disk_entries:
                rel_path = entry["path"]
                mysql_rec = tracked_mysql.get(rel_path)

                if not mysql_rec:
                    entry["operation"] = "ADD"
                    entries_to_process.append(entry)
                    added_count += 1
                else:
                    db_mtime = float(mysql_rec.get("mtime") or 0)
                    db_size = int(mysql_rec.get("file_size") or 0)

                    # Compare mtime/size tolerances
                    if abs(entry["mtime"] - db_mtime) > 1.0 or entry["size"] != db_size:
                        entry["operation"] = "UPDATE"
                        entries_to_process.append(entry)
                        updated_count += 1
                    else:
                        skipped_count += 1

            self._emit(
                f"Incremental filter complete: {len(entries_to_process)} to process "
                f"({added_count} new, {updated_count} modified, {skipped_count} skipped, {cleaned_orphans_count} orphans cleaned)."
            )
        else:
            # Full scan: process everything as ADD
            for entry in all_disk_entries:
                entry["operation"] = "ADD"
            entries_to_process = all_disk_entries
            added_count = len(all_disk_entries)

        now = datetime.now(timezone.utc).isoformat()
        manifest = {
            "job_info": {
                "job_id": f"scan_{self.mount_name}_{int(time.time())}",
                "mount_name": self.mount_name,
                "mount_path": str(self.mount_path),
                "status": "PENDING",
                "created_at": now,
                "last_updated": now,
                "total_files": len(all_disk_entries),
                "to_process_files": len(entries_to_process),
                "processed_files": 0,
                "skipped_files": skipped_count,
                "added_files": 0,
                "updated_files": 0,
                "cleaned_orphans": cleaned_orphans_count,
                "failed_files": 0,
                "eta_seconds": 0,
                "current_index": 0,
                "current_file": None,
            },
            "tree": entries_to_process,
        }

        self._save_manifest(manifest)
        return manifest

    def _prepare_entry(self, file_entry: Dict[str, Any]) -> Dict[str, Any]:
        rel_path = file_entry["path"]
        abs_path = self.mount_path / rel_path

        folder = file_entry.get("folder")
        if folder is None:
            folder = self._resolve_folder(rel_path)
            file_entry["folder"] = folder

        jf_metadata = self._lookup_metadata(folder, abs_path.name)
        normalized_title = normalize_text(abs_path.stem)

        local_meta = build_media_metadata(str(abs_path), jf_metadata)
        file_path = jf_metadata.get("path") or str(abs_path)

        title_for_embedding = (
            jf_metadata.get("title") or jf_metadata.get("jf_name") or normalized_title
        )
        overview = jf_metadata.get("overview") or jf_metadata.get("jf_overview") or ""
        genres = jf_metadata.get("genres") or jf_metadata.get("jf_genres") or []
        genres_str = ", ".join(genres) if isinstance(genres, list) else str(genres)

        text_to_embed = (
            f"Title: {title_for_embedding} "
            f"File: {abs_path.name} "
            f"Overview: {overview} "
            f"Genres: {genres_str}"
        )

        payload = {
            "file_name": abs_path.name,
            "normalized_title": normalized_title,
            "file_path": file_path,
            "relative_path": rel_path,
            "mount": self.mount_name,
            "mount_name": self.mount_name,
            "folder": folder,
            "library": jf_metadata.get("library"),
            "media_type": self.media_type,
            "metadata": local_meta,
            "jellyfin": jf_metadata,
        }

        return {
            "text": text_to_embed, 
            "payload": payload, 
            "jellyfin": jf_metadata,
            "abs_path": str(abs_path),
            "mtime": file_entry.get("mtime", 0.0),
            "size": file_entry.get("size", 0)
        }

    def _encode_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if not self.embedding_model:
            return [[] for _ in texts]
        vectors = self.embedding_model.encode(
            texts, batch_size=len(texts), show_progress_bar=False
        )
        return [list(map(float, vector)) for vector in vectors]

    async def process_media_queue(self, force_rescan: bool = False, incremental_scan: bool = False):
        manifest = self.load_or_create_manifest(force_rescan=force_rescan, incremental_scan=incremental_scan)
        job_info = manifest["job_info"]
        file_tree: List[Dict[str, Any]] = manifest["tree"]

        self._emit(
            f"Scan started for mount '{self.mount_name}' (force_rescan={force_rescan}, incremental={incremental_scan})"
        )

        if self.jellyfin:
            try:
                await self._build_folder_caches(force=force_rescan, manifest=manifest)
            except Exception as e:
                logging.warning(f"Jellyfin library cache build failed: {e}")
                self._emit(f"Jellyfin library cache build failed: {e}", "warn")

        bookmark = job_info.get("bookmark")
        start_index = 0

        if bookmark and not force_rescan and not incremental_scan:
            for idx, item in enumerate(file_tree):
                if item["path"] == bookmark:
                    start_index = idx + 1
                    break

        total_files = len(file_tree)
        job_info["status"] = "IN_PROGRESS"
        job_info["total_files"] = total_files
        job_info["current_index"] = start_index
        job_info["error"] = None
        self._collection_recovery_failures = 0
        self._save_manifest(manifest)

        workers = max(1, settings.indexing.workers)
        batch_size = max(1, settings.indexing.batch_size)
        chunk_size = workers * batch_size

        start_time = time.time()
        processed_this_run = 0
        total_remaining = total_files - start_index
        self._emit(
            f"Indexing {total_remaining} file(s) from position {start_index + 1} "
            f"using {workers} worker(s) x batch {batch_size}"
        )

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"index-{self.mount_name}"
        )

        try:
            for chunk_start in range(start_index, total_files, chunk_size):
                chunk = file_tree[chunk_start : chunk_start + chunk_size]

                prepared_results = await asyncio.gather(
                    *(
                        loop.run_in_executor(executor, self._prepare_entry, file_entry)
                        for file_entry in chunk
                    ),
                    return_exceptions=True,
                )

                prepared: List[tuple] = []
                for offset, (file_entry, result) in enumerate(zip(chunk, prepared_results)):
                    absolute_index = chunk_start + offset
                    if isinstance(result, BaseException):
                        file_entry["status"] = "FAILED"
                        file_entry["error"] = str(result)
                        job_info["failed_files"] += 1
                        logging.error(
                            f"Failed preparing '{file_entry['path']}': {result}",
                            exc_info=result,
                        )
                        self._emit(
                            f"[{absolute_index + 1}/{total_files}] FAILED "
                            f"{file_entry['path']}: {result}",
                            "error",
                        )
                        continue
                    prepared.append((absolute_index, file_entry, result))

                batches = [
                    prepared[i : i + batch_size]
                    for i in range(0, len(prepared), batch_size)
                ]
                vector_groups = await asyncio.gather(
                    *(
                        loop.run_in_executor(
                            executor, self._encode_batch, [item[2]["text"] for item in batch]
                        )
                        for batch in batches
                    ),
                    return_exceptions=True,
                )

                points: List[PointStruct] = []
                indexed: List[tuple] = []
                for batch, vectors in zip(batches, vector_groups):
                    if isinstance(vectors, BaseException):
                        for absolute_index, file_entry, _ in batch:
                            file_entry["status"] = "FAILED"
                            file_entry["error"] = str(vectors)
                            job_info["failed_files"] += 1
                            self._emit(
                                f"[{absolute_index + 1}/{total_files}] FAILED "
                                f"{file_entry['path']}: embedding error: {vectors}",
                                "error",
                            )
                        continue

                    for (absolute_index, file_entry, data), vector in zip(batch, vectors):
                        point_id = generate_file_id(data["abs_path"])
                        if vector:
                            points.append(
                                PointStruct(
                                    id=point_id, vector=vector, payload=data["payload"]
                                )
                            )
                        indexed.append((absolute_index, file_entry, data, point_id))

                if self.qdrant and points:
                    try:
                        await loop.run_in_executor(executor, self._upsert_points, points)
                    except CollectionUnavailableError as e:
                        job_info["status"] = "FAILED"
                        job_info["error"] = str(e)
                        job_info["eta_seconds"] = 0
                        logging.error(str(e))
                        self._emit(f"Scan aborted: {e}", "error")
                        self._save_manifest(manifest)
                        return
                    except Exception as e:
                        logging.error(f"Batch upsert failed: {e}", exc_info=True)
                        for absolute_index, file_entry, _, _ in indexed:
                            file_entry["status"] = "FAILED"
                            file_entry["error"] = str(e)
                            job_info["failed_files"] += 1
                            self._emit(
                                f"[{absolute_index + 1}/{total_files}] FAILED "
                                f"{file_entry['path']}: upsert error: {e}",
                                "error",
                            )
                        indexed = []

                # Update status & record in MySQL
                total_remaining = len(file_tree)
                for absolute_index, file_entry, data, point_id in indexed:
                    payload = data["payload"]
                    op = file_entry.get("operation", "ADD")
                    
                    if op == "UPDATE":
                        job_info["updated_files"] += 1
                    else:
                        job_info["added_files"] += 1

                    mysql_db_instance.upsert_file_record(
                        file_id=point_id,
                        file_path=payload["file_path"],
                        file_name=payload["file_name"],
                        relative_path=payload["relative_path"],
                        mount=self.mount_name,
                        file_size=data.get("size", 0),
                        mtime=data.get("mtime", 0.0),
                        status="INDEXED",
                        vector_id=point_id,
                        jellyfin_id=data["jellyfin"].get("jf_id"),
                        metadata=payload.get("metadata")
                    )

                    job_info["processed_files"] += 1
                    
                    # Change total_files to total_remaining so log displays [1/5] instead of [5413/5417]
                    self._emit(
                        f"[{job_info['processed_files']}/{total_remaining}] {op}ED {file_entry['path']}"
                    )

                last_index = chunk_start + len(chunk) - 1
                last_path = file_tree[last_index]["path"]
                job_info["current_index"] = last_index + 1
                job_info["current_file"] = last_path
                job_info["bookmark"] = last_path

                processed_this_run += len(chunk)
                elapsed = time.time() - start_time
                avg_speed = elapsed / max(processed_this_run, 1)
                job_info["eta_seconds"] = int(
                    max(0, total_remaining - processed_this_run) * avg_speed
                )

                logging.info(
                    f"[{self.mount_name}] Chunk done "
                    f"[{last_index + 1}/{total_files}] "
                    f"({len(chunk) / max(elapsed, 0.001):.1f} files/s avg)"
                )
                self._save_manifest(manifest)
                await asyncio.sleep(0)
        finally:
            executor.shutdown(wait=False)

        self._emit(
            f"Scan completed: {job_info['added_files']} added, "
            f"{job_info['updated_files']} updated, "
            f"{job_info['skipped_files']} skipped, "
            f"{job_info['cleaned_orphans']} orphans cleaned "
            f"({job_info['failed_files']} failed, {total_files} total disk files)"
        )
        job_info["status"] = "COMPLETED"
        job_info["error"] = None
        job_info["eta_seconds"] = 0
        job_info["current_index"] = total_files
        job_info["current_file"] = None
        self._save_manifest(manifest)

    async def stream_progress(self) -> AsyncGenerator[str, None]:
        last_seq = 0
        while True:
            logs = self._events_since(last_seq)
            if logs:
                last_seq = logs[-1]["seq"]

            if self.manifest_path.exists():
                try:
                    with open(self.manifest_path, "r") as f:
                        manifest = yaml.safe_load(f)

                    if manifest and "job_info" in manifest:
                        job = manifest.get("job_info", {})
                        total = job.get("total_files", 0)
                        to_process = job.get("to_process_files", total)
                        processed = job.get("processed_files", 0)

                        data = {
                            "job_id": job.get("job_id"),
                            "mount_name": self.mount_name,
                            "status": job.get("status"),
                            "total_files": total,
                            "to_process_files": to_process,
                            "processed_files": processed,
                            "skipped_files": job.get("skipped_files", 0),
                            "added_files": job.get("added_files", 0),
                            "updated_files": job.get("updated_files", 0),
                            "cleaned_orphans": job.get("cleaned_orphans", 0),
                            "failed_files": job.get("failed_files", 0),
                            "progress_percentage": round(
                                (processed / max(to_process, 1)) * 100, 2
                            ) if to_process > 0 else 100.0,
                            "logs": logs,
                        }

                        yield f"data: {json.dumps(data)}\n\n"

                        if job.get("status") in ["COMPLETED", "FAILED"]:
                            break
                except Exception:
                    pass
            await asyncio.sleep(0.5)