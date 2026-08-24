import os
import time
import httpx
import uvicorn
import json
import asyncio
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient

from media_indexer.config import settings
from media_indexer.database import db_instance, mysql_db_instance, redis_db_instance
from media_indexer.search import search_engine
from media_indexer.actions import MediaActions
from media_indexer.utils import extract_media_metadata
from media_indexer.llms import OllamaEmbeddingClient, OllamaLLMClient
from media_indexer import ytdlp

from media_indexer.jellyfin import JellyfinClient
from media_indexer.scanner import DirectoryTreeScanner

from media_indexer.duplicates import DuplicateDetector
import logging

logger = logging.getLogger(__name__)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler()

app = FastAPI(
    title="Semantic Media Indexer & Search Engine",
    description="Vector search & management engine for local media mounts.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[a-z0-9]+$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

jellyfin_cfg = getattr(settings, "jellyfin", None)

if jellyfin_cfg and getattr(jellyfin_cfg, "enabled", False):
    jellyfin_client = JellyfinClient(
        base_url=getattr(jellyfin_cfg, "url", "http://192.168.12.111:8096"),
        api_key=getattr(jellyfin_cfg, "api_key", "2f74464824354b2195e85757d4aaa723"),
        user_id=getattr(jellyfin_cfg, "user_id", None) or None,
    )
else:
    jellyfin_client = None

v_cfg = getattr(settings, "vectordb", None)
qdrant_host = getattr(v_cfg, "host", "host.docker.internal")
qdrant_port = getattr(v_cfg, "port", 6333)

qdrant_client = QdrantClient(
    host=qdrant_host,
    port=qdrant_port,
)

embedding_model = OllamaEmbeddingClient(
    base_url=settings.embedding.host,
    model_name=settings.embedding.model_name
)

llm_client = OllamaLLMClient(
    base_url=settings.llm.host,
    model_name=settings.llm.model_name
)

MOUNT_REGISTRY = {
    name: mount
    for name, mount in settings.mounts.registry.items()
    if mount.enabled
}

MOUNT_MAP = {name: mount.path for name, mount in MOUNT_REGISTRY.items()}

COLLECTION_NAME = getattr(
    getattr(settings, "vectordb", None), "collection_name", "media_library"
)

scanners = {
    name: DirectoryTreeScanner(
        mount_path=mount.path,
        mount_name=name,
        jellyfin_client=jellyfin_client,
        qdrant_client=qdrant_client,
        embedding_model=embedding_model,
        collection_name=COLLECTION_NAME,
        folder_libraries={f.folder: f.libraries for f in mount.folders},
        media_type=mount.media_type,
    )
    for name, mount in MOUNT_REGISTRY.items()
}

def get_mount_path(path: str) -> str:
    if not path:
        return path

    if os.path.exists(path):
        return path

    for name, mount in MOUNT_REGISTRY.items():
        disk_path = (
            getattr(mount, "disk_path", None)
            or getattr(mount, "host_path", None)
            or getattr(mount, "source_path", None)
            or getattr(mount, "source", None)
        )
        if disk_path and path.startswith(disk_path):
            return path.replace(disk_path, mount.path, 1)

    normalized_path = path.replace("\\", "/")
    for name, mount in MOUNT_REGISTRY.items():
        for folder_item in getattr(mount, "folders", []):
            folder_name = folder_item.folder.strip("/")
            if folder_name and f"/{folder_name}/" in normalized_path:
                subpath = normalized_path.split(f"/{folder_name}/", 1)[1]
                return os.path.join(mount.path, folder_name, subpath)

        mount_basename = os.path.basename(mount.path.rstrip("/"))
        if f"/{mount_basename}/" in normalized_path:
            subpath = normalized_path.split(f"/{mount_basename}/", 1)[1]
            return os.path.join(mount.path, subpath)

    return path

class DownloadRequest(BaseModel):
    url: str
    mount: str = "mount1"


class BulkRenameRequest(BaseModel):
    directory: str | None = None


class RenameRequest(BaseModel):
    old_path: str
    new_name: str


class PluginSearchRequest(BaseModel):
    query: str | None = None
    strings: list[str] = []
    limit: int = 5


class TargetRequest(BaseModel):
    media_type: str = "song"
    title: str | None = None
    language: str | None = None
    quality: str | None = None
    actress: str | None = None
    industry: str | None = None
    movie_name: str | None = None


class FormatsRequest(BaseModel):
    url: str
    cookies: str | None = None


class YtDownloadRequest(TargetRequest):
    url: str
    video_format: dict | None = None
    audio_format: dict | None = None
    cookies: str | None = None
    verbose: bool = False


def resolve_media_path(path: str) -> str:
    path = get_mount_path(path)
    real_path = os.path.realpath(path)
    root = os.path.realpath(settings.mounts.base_dir)
    if os.path.commonpath([real_path, root]) != root:
        raise HTTPException(status_code=403, detail="Path outside of media root")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="File not found")
    return real_path


# ==========================================
# Library Browser (folder/breadcrumb view)
# ==========================================

# Caches recursive folder summaries (file count + a representative cover file)
# computed from the Redis-cached mount tree, so opening a folder with a huge
# tree doesn't re-walk that JSON structure on every request. Keyed by
# "mount::rel_path" -> (cached_at, summary_dict).
_LIBRARY_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}
LIBRARY_CACHE_TTL = 30  # seconds
# Stops the recursive tree walk after this many nodes so a folder with tens of
# thousands of files can't stall a request; the UI shows "N+" instead.
FOLDER_SCAN_CAP = 20000


def _human_size(num_bytes) -> Optional[str]:
    if not num_bytes:
        return None
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return None


def _resolution_label(width, height) -> Optional[str]:
    if height:
        return f"{int(height)}p"
    if width:
        return f"{int(width)}w"
    return None


def _find_tree_node(tree: dict, rel_path: str) -> Optional[dict]:
    """Descends the Redis-cached mount tree to the folder node at rel_path,
    entirely in-memory (no disk access)."""
    if not tree:
        return None
    node = tree
    for part in [p for p in (rel_path or "").split("/") if p]:
        match = None
        for child in node.get("children", []):
            if child.get("type") == "folder" and child.get("name") == part:
                match = child
                break
        if match is None:
            return None
        node = match
    return node


def _summarize_tree_node(node: dict, cache_key: str) -> dict:
    """Recursively counts files under a tree node and picks a representative
    cover file, entirely from the cached Redis tree. Capped for performance
    and cached for LIBRARY_CACHE_TTL."""
    now = time.time()
    cached = _LIBRARY_SUMMARY_CACHE.get(cache_key)
    if cached and now - cached[0] < LIBRARY_CACHE_TTL:
        return cached[1]

    file_count = 0
    cover_node = None
    capped = False
    visited = 0
    stack = [node]

    while stack and not capped:
        current = stack.pop()
        for child in current.get("children", []):
            visited += 1
            if visited > FOLDER_SCAN_CAP:
                capped = True
                break
            if child.get("type") == "folder":
                stack.append(child)
            else:
                file_count += 1
                # Prefer a cover file that actually has Jellyfin artwork
                if cover_node is None:
                    cover_node = child
                elif not cover_node.get("primary_image_tag") and child.get("primary_image_tag"):
                    cover_node = child

    summary = {"file_count": file_count, "cover_node": cover_node, "capped": capped}
    _LIBRARY_SUMMARY_CACHE[cache_key] = (now, summary)
    return summary


def _library_folder_entry(name: str, path: str, mount: str, node: dict, cache_key: str) -> dict:
    summary = _summarize_tree_node(node, cache_key)
    cover = summary["cover_node"] or {}
    return {
        "type": "folder",
        "name": name,
        "path": path,
        "mount": mount,
        "item_count": summary["file_count"],
        "count_capped": summary["capped"],
        "jellyfin_id": cover.get("jellyfin_id"),
        "primary_image_tag": cover.get("primary_image_tag"),
    }


def _library_file_entry(mount_name: str, mount_root: str, rel_dir: str, node: dict) -> dict:
    rel_path = node.get("path") or (os.path.join(rel_dir, node["name"]) if rel_dir else node["name"])
    size_bytes = node.get("size")
    return {
        "type": "file",
        "name": node["name"],
        "path": rel_path,
        "file_path": os.path.join(mount_root, rel_path),
        "mount": mount_name,
        "folder_name": os.path.basename(rel_dir) if rel_dir else mount_name,
        "resolution": _resolution_label(node.get("width"), node.get("height")),
        "size_bytes": size_bytes,
        "size_human": _human_size(size_bytes),
        "duration": node.get("duration"),
        "jellyfin_id": node.get("jellyfin_id"),
        "primary_image_tag": node.get("primary_image_tag"),
    }


@app.get("/api/library/browse", tags=["Library"])
def browse_library(
    mount: str = Query(None, description="Mount name; omit or 'all' to list mounts as top-level libraries"),
    path: str = Query("", description="Relative folder path within the mount"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    sort: str = Query("name", pattern="^(name|size|resolution|duration)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Lists the folders and files at one level of a mount, for the Library tab's
    breadcrumb/card browser. Reads entirely from the Redis-cached tree built
    during indexing — no disk walking on the request path. Folders always
    sort ahead of files."""
    entries: list[dict] = []

    if not mount or mount == "all":
        for name, mnt in MOUNT_REGISTRY.items():
            tree = redis_db_instance.get_mount_tree(name)
            if tree:
                entries.append(_library_folder_entry(name, "", name, tree, f"{name}::"))
            else:
                # Mount hasn't been scanned yet (no cached tree in Redis) —
                # show it as an empty folder rather than erroring out.
                entries.append({
                    "type": "folder", "name": name, "path": "", "mount": name,
                    "item_count": 0, "count_capped": False,
                    "jellyfin_id": None, "primary_image_tag": None,
                })
        breadcrumb = []
    else:
        mnt = MOUNT_REGISTRY.get(mount)
        if not mnt:
            raise HTTPException(status_code=404, detail=f"Mount '{mount}' not registered")

        safe_rel = os.path.normpath(path or "").replace("\\", "/").strip("/")
        safe_rel = "" if safe_rel in (".", "") else safe_rel

        mount_root = mnt.path
        tree = redis_db_instance.get_mount_tree(mount)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Mount '{mount}' has not been indexed yet")

        node = _find_tree_node(tree, safe_rel)
        if node is None:
            raise HTTPException(status_code=404, detail="Folder not found")

        for child in node.get("children", []):
            if child.get("type") == "folder":
                child_rel = os.path.join(safe_rel, child["name"]) if safe_rel else child["name"]
                entries.append(
                    _library_folder_entry(child["name"], child_rel, mount, child, f"{mount}::{child_rel}")
                )
            else:
                entries.append(_library_file_entry(mount, mount_root, safe_rel, child))

        breadcrumb = [{"label": mount, "mount": mount, "path": ""}]
        accum = ""
        for part in [p for p in safe_rel.split("/") if p]:
            accum = f"{accum}/{part}" if accum else part
            breadcrumb.append({"label": part, "mount": mount, "path": accum})

    def sort_key(e: dict):
        if sort == "size":
            return e["item_count"] if e["type"] == "folder" else (e.get("size_bytes") or 0)
        if sort == "resolution":
            return e["item_count"] if e["type"] == "folder" else (e.get("resolution") or "")
        if sort == "duration":
            return e["item_count"] if e["type"] == "folder" else (e.get("duration") or "")
        return e["name"].lower()

    reverse = sort_dir == "desc"
    folders = sorted([e for e in entries if e["type"] == "folder"], key=sort_key, reverse=reverse)
    files = sorted([e for e in entries if e["type"] == "file"], key=sort_key, reverse=reverse)
    ordered = folders + files

    total = len(ordered)
    page = ordered[offset: offset + limit]

    return {
        "mount": mount or "all",
        "path": path or "",
        "breadcrumb": breadcrumb,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
        "items": page,
    }


@app.get("/", include_in_schema=False)
def serve_ui():
    return FileResponse("static/index.html")


@app.post("/api/index/scan", tags=["Indexing"])
async def trigger_scan(
    background_tasks: BackgroundTasks,
    rescan_disk: bool = Query(False, description="Re-walk disk to discover new files"),
    incremental_scan: bool = Query(False, description="Incremental scan only for changed/added/deleted files"),
):
    """Triggers manifest generation and queue processing across all mounts."""
    for mount_name, scanner in scanners.items():
        if rescan_disk or incremental_scan or not hasattr(scanner, "manifest_path") or not scanner.manifest_path.exists():
            if hasattr(scanner, "load_or_create_manifest"):
                scanner.load_or_create_manifest(force_rescan=rescan_disk, incremental_scan=incremental_scan)

        if hasattr(scanner, "process_media_queue"):
            background_tasks.add_task(
                scanner.process_media_queue, force_rescan=rescan_disk, incremental_scan=incremental_scan
            )

    return {
        "status": "success",
        "message": f"Manifest scan and queue processing started for mounts: {list(scanners.keys())} (incremental={incremental_scan})",
    }

@app.get("/api/search", tags=["Search"])
def search_media(q: str = Query(..., description="Semantic search query"), limit: int = 50):
    results = search_engine.search(query=q, limit=limit)
    logger.info(f"Search query '{q}' returned {len(results)} results")
    for r in results:
        mfp = get_mount_path(r.get("file_path")) if r.get("file_path") else None
        r["mounted_file_path"] = mfp
    # Enrich with duplicate group ids
    if mysql_db_instance.enabled and results:
        file_paths = [r.get("mounted_file_path") for r in results if r.get("mounted_file_path")]

        if file_paths:
            group_map = mysql_db_instance.get_duplicate_group_ids_for_paths(file_paths)
            logger.info(f"Found {len(group_map)} duplicate group ids for search results")
            for r in results:
                mfp = r.get("mounted_file_path")
                if mfp and mfp in group_map:
                    logger.info(f"File path '{mfp}' has duplicate group id '{group_map[mfp]}'")
                    r["duplicate_group_id"] = group_map[mfp]
    return results


@app.get("/api/media/thumbnail", tags=["Media Stream"])
async def get_thumbnail(
    path: str = Query(None, description="Absolute file path"),
    jellyfin_id: str = Query(None, description="Jellyfin Item ID"),
    tag: str = Query(None, description="Jellyfin primary image tag"),
    width: int = Query(251, ge=32, le=1920, description="Target fill width"),
    height: int = Query(377, ge=32, le=1920, description="Target fill height"),
    quality: int = Query(96, ge=1, le=100),
):
    if jellyfin_client and jellyfin_id:
        image = await jellyfin_client.get_item_image(
            jellyfin_id,
            tag=tag,
            fill_width=width,
            fill_height=height,
            quality=quality,
        )
        if image:
            img_bytes, content_type = image
            return Response(
                content=img_bytes,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

    return Response(status_code=404, content="Thumbnail unavailable")


@app.get("/api/media/stream", tags=["Media Stream"])
def stream_media(path: str = Query(..., description="Absolute file path")):
    return FileResponse(resolve_media_path(path))


@app.get("/api/media/jellyfin/stream", tags=["Media Stream"])
async def stream_jellyfin_media(
    request: Request,
    jellyfin_id: str = Query(..., description="Jellyfin Item ID"),
):
    if not jellyfin_client:
        raise HTTPException(status_code=503, detail="Jellyfin integration is disabled")

    url = jellyfin_client.build_stream_url(jellyfin_id)
    if not url:
        raise HTTPException(status_code=400, detail="Invalid Jellyfin item id")

    forward_headers = {}
    if request.headers.get("range"):
        forward_headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    upstream = await client.send(
        client.build_request("GET", url, headers=forward_headers), stream=True
    )

    if upstream.status_code >= 400:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=status, detail="Jellyfin stream unavailable")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in ("content-length", "content-range", "accept-ranges")
    }
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type", "video/mp4"),
    )


@app.get("/api/media/metadata", tags=["Media Stream"])
def get_metadata(path: str = Query(..., description="Absolute file path")):
    return extract_media_metadata(resolve_media_path(path))

@app.post("/api/plugin/search", tags=["Browser Plugin"])
def plugin_search(req: PluginSearchRequest):
    parts = [req.query or "", *req.strings]
    query = " ".join(p.strip() for p in parts if p and p.strip())
    if not query:
        raise HTTPException(status_code=400, detail="No searchable text supplied")
    limit = max(1, min(req.limit, 25))
    return search_engine.search(query=query, limit=limit)


@app.get("/api/ytdlp/options", tags=["Browser Plugin"])
def ytdlp_options():
    return ytdlp.options()


@app.post("/api/ytdlp/formats", tags=["Browser Plugin"])
def ytdlp_formats(req: FormatsRequest):
    return ytdlp.fetch_formats(req.url, cookies=req.cookies)


@app.post("/api/ytdlp/download", tags=["Browser Plugin"])
def ytdlp_download(req: YtDownloadRequest):
    data = req.model_dump()
    
    if "video_format_id" in data:
        v_id = data.pop("video_format_id", None)
        if v_id and "video_format" not in data:
            data["video_format"] = {"format_id": v_id}
            
    if "audio_format_id" in data:
        a_id = data.pop("audio_format_id", None)
        if a_id and "audio_format" not in data:
            data["audio_format"] = {"format_id": a_id}

    return ytdlp.start_download(**data)


@app.post("/api/ytdlp/target", tags=["Browser Plugin"])
def ytdlp_target(req: TargetRequest):
    return ytdlp.plan_target(**req.model_dump())


@app.get("/api/ytdlp/jobs/{job_id}", tags=["Browser Plugin"])
def ytdlp_job(job_id: str):
    return ytdlp.get_job(job_id)

@app.get("/api/ytdlp/stream/{job_id}", tags=["Browser Plugin"])
async def stream_ytdlp_job(job_id: str):
    async def event_generator():
        last_progress = None
        last_status = None
        
        while True:
            try:
                job = ytdlp.get_job(job_id)
            except HTTPException:
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                break

            status = job.get("status")
            progress = job.get("progress")

            if status != last_status or progress != last_progress:
                last_status = status
                last_progress = progress
                yield f"data: {json.dumps(job)}\n\n"

            if status in ("success", "failed", "completed"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/actions/bulk-normalize-underscores", tags=["Actions"])
def bulk_normalize_underscores(req: BulkRenameRequest):
    directory = get_mount_path(req.directory) if req.directory else None
    return MediaActions.bulk_rename_remove_underscores(directory=directory)


@app.post("/api/actions/rename", tags=["Actions"])
def rename_file(req: RenameRequest):
    old_path = get_mount_path(req.old_path)
    return MediaActions.rename_file(old_path=old_path, new_name=req.new_name)


@app.delete("/api/actions/file", tags=["Actions"])
def delete_file(path: str = Query(...)):
    mount_path = get_mount_path(path)
    return MediaActions.delete_file(file_path=mount_path)


@app.post("/api/admin/index/clean", tags=["Admin"])
def clean_index(
    mode: str = Query("truncate", pattern="^(truncate|recreate)$", description="truncate keeps the collection, recreate drops and rebuilds it"),
    clear_manifests: bool = Query(True, description="Also drop scan manifests so the next scan re-walks the disk"),
):
    removed = (
        db_instance.reset_collection() if mode == "recreate" else db_instance.truncate_collection()
    )

    # MySQL: wipe processed_files only. download_tracker and indexing_jobs
    # are separate tables and are intentionally left untouched.
    truncate_tables = mysql_db_instance.truncate_tables()

    # Redis: drop every cached mount tree so the Library tab doesn't keep
    # serving stale folder/file data after a clean.
    cleared_redis_trees = redis_db_instance.clear_all_mount_trees()
    _LIBRARY_SUMMARY_CACHE.clear()

    cleared = []
    if clear_manifests:
        for scanner in scanners.values():
            manifest = getattr(scanner, "manifest_path", None)
            if manifest and manifest.exists():
                manifest.unlink()
                cleared.append(str(manifest))

    return {
        "status": "success",
        "mode": mode,
        "collection": COLLECTION_NAME,
        "deleted_points": removed,
        "truncate_tables": truncate_tables,
        "redis_mount_trees_cleared": cleared_redis_trees,
        "cleared_manifests": cleared,
        "remaining_points": db_instance.count_items(),
    }


@app.get("/api/admin/index/stats", tags=["Admin"])
def index_stats():
    return {"collection": COLLECTION_NAME, "points": db_instance.count_items()}

@app.get("/api/scan/mounts", tags=["Mount Indexing"])
def list_mounts():
    return [
        {
            "mount_name": name,
            "path": mount.path,
            "media_type": mount.media_type,
            "folders": [
                {"folder": f.folder, "libraries": f.libraries} for f in mount.folders
            ],
        }
        for name, mount in MOUNT_REGISTRY.items()
    ]


@app.post("/api/scan/start")
async def start_scan(
    background_tasks: BackgroundTasks,
    mount_name: str = Query(..., description="Target mount name"),
    rescan_disk: bool = Query(False, description="Force re-walk directory tree"),
    incremental_scan: bool = Query(False, description="Run incremental scan for new/modified/deleted files")
):
    scanner = scanners.get(mount_name)
    if not scanner:
        raise HTTPException(status_code=404, detail=f"Mount '{mount_name}' not registered")

    _LIBRARY_SUMMARY_CACHE.clear()

    async def run_pipeline():
        # If incremental_scan is requested, make sure rescan_disk is False
        force_rescan = rescan_disk and not incremental_scan
        await scanner.process_media_queue(force_rescan=force_rescan, incremental_scan=incremental_scan)

    background_tasks.add_task(run_pipeline)
    return {
        "status": "queued",
        "message": f"Scan pipeline initiated for mount '{mount_name}' (incremental={incremental_scan})."
    }

@app.get("/api/scan/stream", tags=["Mount Indexing"])
async def stream_scan_progress(
    mount_name: str = Query(..., description="Mount name: 'songs' or 'movies'")
):
    scanner = scanners.get(mount_name)
    if not scanner:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mount name '{mount_name}'. Available: {list(MOUNT_MAP.keys())}",
        )

    return StreamingResponse(
        scanner.stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

class DownloadUpdate(BaseModel):
    status: str

@app.get("/api/actions/downloads", tags=["Download Tracker"])
def get_downloads():
    if not mysql_db_instance.enabled:
        raise HTTPException(status_code=503, detail="MySQL database is disabled")
    
    conn = mysql_db_instance._get_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT entry, status, updated_at, title, size, thumbnail FROM download_tracker ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            
            # Map database columns to the structure expected by the frontend UI
            results = []
            for row in rows:
                title = row.get("title", "") or ""
                entry_text = row.get("entry", "")
                parts = entry_text.split("|")
                url = parts[0] if parts else entry_text
                actress = parts[3] if len(parts) >= 4 else url
                quality = parts[1] if len(parts) >= 2 else None
                language = parts[2] if len(parts) >= 3 else None
                size = row.get("size")
                thumbnail = row.get("thumbnail")

                results.append({
                    "id": entry_text,  # Primary key string
                    "title": title,
                    "url": url,
                    "actress": actress,
                    "quality": quality,
                    "language": language,
                    "status": row.get("status", "PENDING"),
                    "created_at": row.get("updated_at"),
                    "size": size,
                    "thumbnail": thumbnail
                })
            return results
    finally:
        conn.close()

@app.delete("/api/actions/downloads/{download_id:path}", tags=["Download Tracker"])
def delete_download(download_id: str):
    removed = mysql_db_instance.remove_download_entry(download_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Download entry not found or already deleted")
    return {"status": "deleted", "entry": download_id}

class DownloadEntryRequest(BaseModel):
    entry: str
    title: str | None = None

class UpdateEntryStatusRequest(BaseModel):
    entry: str
    status: str
    size: int | None = None
    thumbnail: str | None = None


@app.post("/api/ytdlp/download-entry", tags=["Browser Plugin"])
def add_download_entry(req: DownloadEntryRequest):
    entry_text = req.entry.strip()
    if not entry_text:
        raise HTTPException(status_code=400, detail="Download entry text cannot be empty")

    target_dir = "/app/ytdlp"
    target_file = os.path.join(target_dir, "download.txt")

    # Insert or reset status in MySQL download_tracker back to PENDING
    mysql_db_instance.add_or_update_download_entry(entry_text, req.title or "")
    mysql_db_instance.update_download_status(entry_text, "PENDING")

    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(entry_text + "\n")

        return {
            "status": "success",
            "message": f"Entry logged and written to {target_file}",
            "file": target_file,
            "entry_status": "PENDING"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write to entry file: {str(e)}")


@app.post("/api/ytdlp/update-status", tags=["Browser Plugin"])
def update_entry_status(req: UpdateEntryStatusRequest):
    entry_text = req.entry.strip()
    status_text = req.status.strip().upper()
    size_value = req.size
    thumbnail_value = req.thumbnail

    if not entry_text or not status_text:
        raise HTTPException(status_code=400, detail="Entry and status are required")

    success = mysql_db_instance.update_download_status(entry_text, status_text, size_value or 0, thumbnail_value)
    if not success:
        raise HTTPException(status_code=404, detail="Entry not found or failed to update")

    return {
        "status": "success",
        "entry": entry_text,
        "updated_status": status_text
    }

@app.patch("/api/actions/downloads/{download_id:path}", tags=["Download Tracker"])
def update_download_status(download_id: str, update: DownloadUpdate):
    success = mysql_db_instance.update_download_status(download_id, update.status.upper())
    if not success:
        raise HTTPException(status_code=404, detail="Download entry not found or update failed")
    return {"status": "success", "entry": download_id, "updated_status": update.status.upper()}

class DeleteDownloadRequest(BaseModel):
    entry: str

@app.post("/api/actions/downloads/delete", tags=["Download Tracker"])
def delete_download_by_body(req: DeleteDownloadRequest):
    entry_text = req.entry.strip()
    if not entry_text:
        raise HTTPException(status_code=400, detail="Entry string cannot be empty")
        
    removed = mysql_db_instance.remove_download_entry(entry_text)
    if not removed:
        raise HTTPException(status_code=404, detail="Download entry not found or already deleted")
        
    return {"status": "deleted", "entry": entry_text}

@app.delete("/api/actions/clean-record", tags=["Actions"])
def clean_record_from_index(path: str = Query(..., description="Absolute file path")):
    mount_path = get_mount_path(path)
    return MediaActions.clean_record_from_index(file_path=mount_path)

@app.delete("/api/admin/duplicates/clean", tags=["Admin"])
def clean_duplicate_tables():
    results = mysql_db_instance.truncate_duplicate_tables()
    return {"status": "success", "results": results}


@app.get("/api/admin/duplicates/groups", tags=["Admin"])
def list_duplicate_groups(
    mount: str = Query(None),
    folder: str = Query(None, description="Filter by folder path prefix (e.g. /media/storage/songs/Artist)"),
    status: str = Query(None, regex="^(PENDING|DUPLICATE|REJECTED)$"),  # candidate status
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    groups = mysql_db_instance.get_duplicate_groups(
        mount=mount, folder=folder, status=status, limit=limit, offset=offset
    )
    # Optionally compute total count without limit/offset for pagination metadata
    return {"items": groups, "total": len(groups)}


@app.get("/api/admin/duplicates/group", tags=["Admin"])
def get_duplicate_group(group_id: str = Query(...)):
    group = mysql_db_instance.get_duplicate_group_by_id(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group

@app.get("/api/admin/duplicates/folder", tags=["Admin"])
def get_duplicates_for_folder(
    mount: str = Query(...),
    path: str = Query(""),
    status: str = Query(None, regex="^(PENDING|DUPLICATE|REJECTED)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    mnt = MOUNT_REGISTRY.get(mount)
    if not mnt:
        raise HTTPException(status_code=404, detail="Mount not found")
    folder_path = os.path.join(mnt.path, path).replace("\\", "/")
    groups = mysql_db_instance.get_duplicate_groups(
        mount=mount, folder=folder_path, status=status, limit=limit, offset=offset
    )
    return {"items": groups, "total": len(groups)}

@app.get("/api/admin/duplicates/file", tags=["Admin"])
def get_duplicates_for_file(
    file_path: str = Query(...),
):
    group = mysql_db_instance.get_duplicate_group_by_file(file_path)
    if not group:
        return {"group_id": None, "entries": []}   # empty instead of 404
    return group

@app.post("/api/admin/duplicates/action", tags=["Admin"])
def update_duplicate_action(body: dict):
    file_path = body.get("file_path")
    action = body.get("action")  # DUPLICATE, REJECTED (status values)
    if not file_path or not action:
        raise HTTPException(status_code=400, detail="Missing file_path or action")
    if action not in ("DUPLICATE", "REJECTED"):
        raise HTTPException(status_code=400, detail="Invalid action; must be DUPLICATE or REJECTED")
    success = mysql_db_instance.update_candidate_status(file_path, action)
    if not success:
        raise HTTPException(status_code=404, detail="Candidate not found or update failed")
    return {"status": "updated", "file_path": file_path, "new_status": action}

@app.post("/api/admin/duplicates/detect", tags=["Admin"])
def trigger_duplicate_detection(mount: str = Query(None), nameTierMinDf: int = Query(3, ge=3, description="Minimum document frequency for name tier")):
    if mount:
        if mount not in scanners:
            raise HTTPException(status_code=404, detail="Mount not found")
        detector = DuplicateDetector(
            qdrant_client, COLLECTION_NAME,
            similarity_threshold=settings.duplicates.similarity_threshold,
            media_type=MOUNT_REGISTRY[mount].media_type,
            nameTierMinDf=nameTierMinDf
        )
        detector.detect_for_mount(mount, MOUNT_REGISTRY[mount].path)
        return {"status": "success", "mount": mount}
    else:
        for name, mnt in MOUNT_REGISTRY.items():
            detector = DuplicateDetector(
                qdrant_client, COLLECTION_NAME,
                similarity_threshold=settings.duplicates.similarity_threshold,
                media_type=mnt.media_type,
                nameTierMinDf=nameTierMinDf
            )
            detector.detect_for_mount(name, mnt.path)
        return {"status": "success", "message": "Duplicate detection run for all mounts"}

@app.on_event("startup")
def setup_cron():
    auto_cfg = settings.indexing.auto_scan
    if auto_cfg.enabled:
        trigger = CronTrigger.from_crontab(auto_cfg.cron)
        
        async def scheduled_scan():
            for mount_name, scanner in scanners.items():
                scanner.load_or_create_manifest(incremental_scan=auto_cfg.incremental)
                await scanner.process_media_queue(incremental_scan=auto_cfg.incremental)

        scheduler.add_job(scheduled_scan, trigger)
        scheduler.start()

def start():
    uvicorn.run("media_indexer.main:app", host=settings.server.host, port=settings.server.port, reload=True)


if __name__ == "__main__":
    start()