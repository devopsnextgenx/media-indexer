import os
import httpx
import uvicorn
import json
import asyncio
from fastapi import BackgroundTasks, FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse, StreamingResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

from media_indexer.config import settings
from media_indexer.database import db_instance
from media_indexer.indexer import indexer_service
from media_indexer.search import search_engine
from media_indexer.actions import MediaActions
from media_indexer.utils import generate_thumbnail_bytes, extract_media_metadata
from media_indexer import ytdlp

from media_indexer.jellyfin import JellyfinClient
from media_indexer.scanner import DirectoryTreeScanner

app = FastAPI(
    title="Semantic Media Indexer & Search Engine",
    description="Vector search & management engine for local media mounts.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Browser extension popups call this API from a chrome-extension:// origin
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[a-z0-9]+$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Static UI mount
app.mount("/static", StaticFiles(directory="static"), name="static")

# Check if Jellyfin is enabled in config
jellyfin_cfg = getattr(settings, "jellyfin", None)

if jellyfin_cfg and getattr(jellyfin_cfg, "enabled", False):
    jellyfin_client = JellyfinClient(
        base_url=getattr(jellyfin_cfg, "url", "http://192.168.12.111:8096"),
        api_key=getattr(jellyfin_cfg, "api_key", "2f74464824354b2195e85757d4aaa723"),
        user_id=getattr(jellyfin_cfg, "user_id", None) or None,
    )
else:
    jellyfin_client = None  # Or disable client calls gracefully

v_cfg = getattr(settings, "vectordb", None)
qdrant_host = getattr(v_cfg, "host", "host.docker.internal")
qdrant_port = getattr(v_cfg, "port", 6333)

qdrant_client = QdrantClient(
    host=qdrant_host,
    port=qdrant_port,
)

embedding_model = SentenceTransformer(
    getattr(settings, "EMBEDDING_MODEL", "all-MiniLM-L6-v2")
)

MOUNT_REGISTRY = {
    name: mount
    for name, mount in settings.mounts.registry.items()
    if mount.enabled
}

MOUNT_MAP = {name: mount.path for name, mount in MOUNT_REGISTRY.items()}

# Extract target collection from config.yml
COLLECTION_NAME = getattr(
    getattr(settings, "vectordb", None), "collection_name", "media_library"
)

# Mount Registry: Instantiated after dependencies are available
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
    """Blocks traversal outside the configured media root."""
    real_path = os.path.realpath(path)
    root = os.path.realpath(settings.mounts.base_dir)
    if os.path.commonpath([real_path, root]) != root:
        raise HTTPException(status_code=403, detail="Path outside of media root")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="File not found")
    return real_path


@app.get("/", include_in_schema=False)
def serve_ui():
    return FileResponse("static/index.html")


@app.post("/api/index/scan", tags=["Indexing"])
async def trigger_scan(
    background_tasks: BackgroundTasks,
    rescan_disk: bool = Query(False, description="Re-walk disk to discover new files"),
):
    """Triggers manifest generation and queue processing across all mounts."""
    for mount_name, scanner in scanners.items():
        # Step 1: Ensure manifest exists or rescan disk
        if rescan_disk or not hasattr(scanner, "manifest_path") or not scanner.manifest_path.exists():
            if hasattr(scanner, "load_or_create_manifest"):
                scanner.load_or_create_manifest()

        # Step 2: Queue background indexing via DirectoryTreeScanner
        if hasattr(scanner, "process_media_queue"):
            background_tasks.add_task(scanner.process_media_queue)

    return {
        "status": "success",
        "message": f"Manifest scan and queue processing started for mounts: {list(scanners.keys())}",
    }

@app.get("/api/search", tags=["Search"])
def search_media(q: str = Query(..., description="Semantic search query"), limit: int = 50):
    return search_engine.search(query=q, limit=limit)


@app.get("/api/media/thumbnail", tags=["Media Stream"])
async def get_thumbnail(
    path: str = Query(None, description="Absolute file path"),
    jellyfin_id: str = Query(None, description="Jellyfin Item ID"),
    tag: str = Query(None, description="Jellyfin primary image tag"),
    width: int = Query(251, ge=32, le=1920, description="Target fill width"),
    height: int = Query(377, ge=32, le=1920, description="Target fill height"),
    quality: int = Query(96, ge=1, le=100),
):
    # 1. Proxy directly from Jellyfin if ID is provided
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

    # 2. Return fallback placeholder to prevent FFmpeg timeouts over SMB/NFS
    return Response(status_code=404, content="Thumbnail unavailable")


@app.get("/api/media/stream", tags=["Media Stream"])
def stream_media(path: str = Query(..., description="Absolute file path")):
    return FileResponse(resolve_media_path(path))


@app.get("/api/media/jellyfin/stream", tags=["Media Stream"])
async def stream_jellyfin_media(
    request: Request,
    jellyfin_id: str = Query(..., description="Jellyfin Item ID"),
):
    """Proxies Jellyfin direct-play so the API key is never exposed to the browser."""
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
    """Semantic lookup driven by the yt-formatted-string text scraped by the browser plugin."""
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
    
    # Normalize legacy payload format IDs if present
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
    """Server-Sent Events (SSE) endpoint to stream real-time yt-dlp download logs and format status."""
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

            # Push event if status or progress changes
            if status != last_status or progress != last_progress:
                last_status = status
                last_progress = progress
                yield f"data: {json.dumps(job)}\n\n"

            # Terminate SSE stream when job completes or fails
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
    return MediaActions.bulk_rename_remove_underscores(directory=req.directory)


@app.post("/api/actions/rename", tags=["Actions"])
def rename_file(req: RenameRequest):
    return MediaActions.rename_file(old_path=req.old_path, new_name=req.new_name)


@app.delete("/api/actions/file", tags=["Actions"])
def delete_file(path: str = Query(...)):
    return MediaActions.delete_file(file_path=path)


@app.post("/api/admin/index/clean", tags=["Admin"])
def clean_index(
    mode: str = Query("truncate", pattern="^(truncate|recreate)$", description="truncate keeps the collection, recreate drops and rebuilds it"),
    clear_manifests: bool = Query(True, description="Also drop scan manifests so the next scan re-walks the disk"),
):
    """Empties the Qdrant collection so a fresh indexing run can start clean."""
    removed = (
        db_instance.reset_collection() if mode == "recreate" else db_instance.truncate_collection()
    )

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
        "cleared_manifests": cleared,
        "remaining_points": db_instance.count_items(),
    }


@app.get("/api/admin/index/stats", tags=["Admin"])
def index_stats():
    return {"collection": COLLECTION_NAME, "points": db_instance.count_items()}

@app.get("/api/scan/mounts", tags=["Mount Indexing"])
def list_mounts():
    """Enabled mounts with their folder -> Jellyfin library mapping."""
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
    rescan_disk: bool = Query(False, description="Force re-walk directory tree")
):
    scanner = scanners.get(mount_name)
    if not scanner:
        raise HTTPException(status_code=404, detail=f"Mount '{mount_name}' not registered")

    async def run_pipeline():
        # Force rescan pipeline resets manifest and bookmark if rescan_disk=True
        await scanner.process_media_queue(force_rescan=rescan_disk)

    background_tasks.add_task(run_pipeline)

    return {
        "status": "queued",
        "message": f"Scan pipeline initiated in background for mount '{mount_name}'."
    }

@app.get("/api/scan/stream", tags=["Mount Indexing"])
async def stream_scan_progress(
    mount_name: str = Query(..., description="Mount name: 'songs' or 'movies'")
):
    """Server-Sent Events (SSE) progress endpoint."""
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

class DownloadEntryRequest(BaseModel):
    entry: str

@app.post("/api/ytdlp/download-entry", tags=["Browser Plugin"])
def add_download_entry(req: DownloadEntryRequest):
    entry_text = req.entry.strip()
    if not entry_text:
        raise HTTPException(status_code=400, detail="Download entry text cannot be empty")

    target_dir = "/app/ytdlp"
    target_file = os.path.join(target_dir, "download.txt")

    try:
        os.makedirs(target_dir, exist_ok=True)
        
        # Append entry with newline
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(entry_text + "\n")

        return {
            "status": "success",
            "message": f"Entry added to {target_file}",
            "file": target_file
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write to entry file: {str(e)}")

def start():
    uvicorn.run("media_indexer.main:app", host=settings.server.host, port=settings.server.port, reload=True)


if __name__ == "__main__":
    start()