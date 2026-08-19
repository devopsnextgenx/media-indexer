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
from media_indexer.utils import build_media_metadata, normalize_text

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
        # Configured folder ("" = mount root) -> Jellyfin library names
        self.folder_libraries: Dict[str, List[str]] = folder_libraries or {}
        self._folder_keys = {f.lower(): f for f in self.folder_libraries}
        self._folder_caches: Dict[str, Dict[str, Any]] = {}
        self._mount_cache: Dict[str, Any] = {}
        self._collection_recovery_failures = 0
        self._events: Deque[Dict[str, Any]] = deque(maxlen=settings.indexing.log_buffer)
        self._event_seq = 0

        # Ensure collection exists immediately on startup
        if self.qdrant:
            self._ensure_collection()

    def _emit(self, message: str, level: str = "info"):
        """Appends a line to the in-memory scan console buffer consumed by the SSE stream."""
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
        """Maps a mount-relative path onto its configured folder key."""
        parts = Path(rel_path).parts
        if len(parts) > 1:
            match = self._folder_keys.get(parts[0].lower())
            if match is not None:
                return match
        return ""

    async def _build_folder_caches(
        self, force: bool = False, manifest: Optional[Dict[str, Any]] = None
    ):
        """Loads each configured library once and groups the results per folder."""
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
            # Throttle disk writes; SSE readers poll once per second anyway
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
        """Resolves a file against its folder's libraries, then the whole mount."""
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
        """Recreates the collection so the run can continue; aborts after repeated failures."""
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
        """Upserts a batch of points, recreating the collection once if it vanished mid-run."""
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
        """High-performance directory walker with CIFS/SMB and symlink compatibility."""
        valid_extensions = {
            # Video
            ".mp4", ".mkv", ".avi", ".webm", ".flv", ".m4v", 
            ".wmv", ".mov", ".ts", ".m2ts", ".mpg", ".mpeg",
            # Audio
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
                                    tree_entries.append(
                                        {
                                            "path": rel,
                                            "folder": self._resolve_folder(rel),
                                            "status": "PENDING",
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
        """Persists state checkpoint safely using an atomic file write swap."""
        data["job_info"]["last_updated"] = datetime.now(
            timezone.utc
        ).isoformat()
        
        temp_manifest_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(temp_manifest_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            
            # Atomic replacement guarantees SSE readers never encounter partial writes
            temp_manifest_path.replace(self.manifest_path)
        except Exception:
            if temp_manifest_path.exists():
                temp_manifest_path.unlink()

    def load_or_create_manifest(self, force_rescan: bool = False) -> Dict[str, Any]:
        """Loads existing manifest or builds a new tree index. Rebuilds if force_rescan=True."""
        logging.info(f"Starting directory walk for mount: {self.mount_name}")
        
        # Only load existing manifest if force_rescan is False
        if not force_rescan and self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r") as f:
                    manifest = yaml.safe_load(f)
                    if manifest and "job_info" in manifest:
                        return manifest
            except Exception:
                pass  # Fallback to rebuild if file read fails

        # Build fresh manifest using optimized dir walk
        self._emit(f"Walking directory tree at {self.mount_path}")
        tree_entries = self._fast_dir_walk(self.mount_path)
        logging.info(f"Directory walk completed. Found {len(tree_entries)} media files.")
        self._emit(f"Directory walk completed. Found {len(tree_entries)} media files.")

        now = datetime.now(timezone.utc).isoformat()
        manifest = {
            "job_info": {
                "job_id": f"scan_{self.mount_name}_{int(time.time())}",
                "mount_name": self.mount_name,
                "mount_path": str(self.mount_path),
                "status": "PENDING",
                "created_at": now,
                "last_updated": now,
                "total_files": len(tree_entries),
                "processed_files": 0,
                "failed_files": 0,
                "eta_seconds": 0,
                "current_index": 0,
                "current_file": None,
                "current_library": None,
                "libraries_loaded": 0,
                "libraries_total": 0,
                "library_items_loaded": 0,
                "library_items_total": None,
                "bookmark": None,  # Reset bookmark on fresh manifest creation
            },
            "tree": tree_entries,
        }

        self._save_manifest(manifest)
        return manifest

    def _prepare_entry(self, file_entry: Dict[str, Any]) -> Dict[str, Any]:
        """Resolves Jellyfin metadata and builds the embedding text + Qdrant payload for one file."""
        rel_path = file_entry["path"]
        abs_path = self.mount_path / rel_path

        folder = file_entry.get("folder")
        if folder is None:
            folder = self._resolve_folder(rel_path)
            file_entry["folder"] = folder

        jf_metadata = self._lookup_metadata(folder, abs_path.name)
        normalized_title = normalize_text(abs_path.stem)

        # Falls back to ffprobe/stat whenever Jellyfin omits resolution, duration or size
        local_meta = build_media_metadata(str(abs_path), jf_metadata)

        # Resolve mapped file path or fall back to scanner path
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

        return {"text": text_to_embed, "payload": payload, "jellyfin": jf_metadata}

    def _encode_batch(self, texts: List[str]) -> List[List[float]]:
        """Encodes a batch of texts in one model call; runs inside the worker pool."""
        if not texts:
            return []
        if not self.embedding_model:
            return [[] for _ in texts]
        vectors = self.embedding_model.encode(
            texts, batch_size=len(texts), show_progress_bar=False
        )
        return [list(map(float, vector)) for vector in vectors]

    async def process_media_queue(self, force_rescan: bool = False):
        """Starts or resumes scanning using the bookmark pointer, indexing files in parallel."""
        manifest = self.load_or_create_manifest(force_rescan=force_rescan)
        job_info = manifest["job_info"]
        file_tree: List[Dict[str, Any]] = manifest["tree"]

        self._emit(
            f"Scan started for mount '{self.mount_name}' (force_rescan={force_rescan})"
        )

        # Bulk-load the mapped Jellyfin libraries once so each file resolves locally
        if self.jellyfin:
            try:
                await self._build_folder_caches(force=force_rescan, manifest=manifest)
            except Exception as e:
                logging.warning(f"Jellyfin library cache build failed: {e}")
                self._emit(f"Jellyfin library cache build failed: {e}", "warn")

        bookmark = job_info.get("bookmark")
        start_index = 0

        # Bookmark Lookup: Find start position in sequence
        if bookmark and not force_rescan:
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

                # 1. Resolve metadata + build payloads; offloaded because missing
                # Jellyfin fields trigger blocking ffprobe calls
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

                # 2. Embed the chunk across the worker pool, one model call per batch
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

                # 3. Collect points for a single bulk upsert
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
                        point_id = str(uuid.uuid4())
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

                # 4. Mark the chunk's files as indexed
                for absolute_index, file_entry, data, point_id in indexed:
                    jf_metadata = data["jellyfin"]
                    file_entry["status"] = "INDEXED"
                    file_entry["jellyfin_id"] = (
                        jf_metadata.get("jellyfin_id") or jf_metadata.get("jf_id")
                    )
                    file_entry["library"] = jf_metadata.get("library")
                    file_entry["vector_id"] = point_id
                    job_info["processed_files"] += 1
                    self._emit(
                        f"[{absolute_index + 1}/{total_files}] INDEXED {file_entry['path']}"
                    )

                # 5. Advance bookmark to the end of the chunk and checkpoint once
                last_index = chunk_start + len(chunk) - 1
                last_path = file_tree[last_index]["path"]
                job_info["current_index"] = last_index + 1
                job_info["current_file"] = last_path
                job_info["bookmark"] = last_path

                processed_this_run += len(chunk)
                elapsed = time.time() - start_time
                avg_speed = elapsed / processed_this_run
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

        # Emit before flipping status so SSE readers see the summary before they disconnect
        self._emit(
            f"Scan completed: {job_info['processed_files']} indexed, "
            f"{job_info['failed_files']} failed, {total_files} total"
        )
        job_info["status"] = "COMPLETED"
        job_info["error"] = None
        job_info["eta_seconds"] = 0
        job_info["current_index"] = total_files
        job_info["current_file"] = None
        self._save_manifest(manifest)

    async def stream_progress(self) -> AsyncGenerator[str, None]:
        """SSE stream endpoint provider for live dashboard updates."""
        # Replay the buffered console so a late subscriber still sees prior lines
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
                        processed = job.get("processed_files", 0)
                        # Position in the queue, which advances even for failed files
                        current = job.get("current_index") or processed

                        data = {
                            "job_id": job.get("job_id"),
                            "mount_name": self.mount_name,
                            "status": job.get("status"),
                            "bookmark": job.get("bookmark"),
                            "current_index": current,
                            "current_file": job.get("current_file") or job.get("bookmark"),
                            "total": total,
                            "processed": current,
                            "total_files": total,
                            "processed_files": processed,
                            "failed_files": job.get("failed_files", 0),
                            "progress_percentage": round(
                                (current / max(total, 1)) * 100, 2
                            ),
                            "eta_seconds": job.get("eta_seconds", 0),
                            "current_library": job.get("current_library"),
                            "libraries_loaded": job.get("libraries_loaded", 0),
                            "libraries_total": job.get("libraries_total", 0),
                            "library_items_loaded": job.get("library_items_loaded", 0),
                            "library_items_total": job.get("library_items_total"),
                            "error": job.get("error"),
                            "last_updated": job.get("last_updated"),
                            "logs": logs,
                        }

                        yield f"data: {json.dumps(data)}\n\n"

                        if job.get("status") in ["COMPLETED", "FAILED"]:
                            break
                except Exception:
                    pass  # Ignore brief read conflicts during swap operations
            else:
                yield f"data: {json.dumps({'status': 'MANIFEST_NOT_FOUND', 'mount_name': self.mount_name, 'logs': logs})}\n\n"

            await asyncio.sleep(0.5)