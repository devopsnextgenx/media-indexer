import logging
import os
import re
import subprocess
import tempfile
import threading
import uuid
import socket
import asyncio
from collections.abc import Callable
from urllib.parse import urlparse
from fastapi import HTTPException
import json
from media_indexer.config import settings
from media_indexer.utils import format_file_size

import nats

logger = logging.getLogger(__name__)

YTDLP_BIN = "/usr/local/bin/yt-dlp"

TARGET_HEIGHTS = (720, 1080, 1440, 2160)
LANGUAGES = ("Hindi", "South", "Marathi", "English", "Bhojpuri")
QUALITIES = ("xhd", "hd", "sd")
INDUSTRIES = ("bollywood", "hollywood")
MEDIA_TYPES = ("song", "movie")

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_LOG_LINES = 100

HOST_COOKIE_FILE = "/app/cookies/yt_cookies.txt"

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_NATS_TERMINAL_STATUSES = {"completed", "failed"}
_DOWNLOAD_SUBSCRIBERS: dict[
    str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]
] = {}
_DOWNLOAD_SUBSCRIBERS_LOCK = threading.Lock()
_DOWNLOAD_EVENTS: dict[str, list[dict]] = {}


def _tracker():
    from media_indexer.database import mysql_db_instance
    return mysql_db_instance

def _set_job(job_id: str, **updates) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(job_id, {"id": job_id})
        job.update(updates)
        return dict(job)

def get_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown download job")
    return dict(job)


def subscribe_download_events(
    entry: str,
) -> tuple[asyncio.Queue, Callable[[], None]]:
    """Subscribe an SSE client to events for one tracked download."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    subscriber = (loop, queue)
    with _DOWNLOAD_SUBSCRIBERS_LOCK:
        _DOWNLOAD_SUBSCRIBERS.setdefault(entry, set()).add(subscriber)

    def unsubscribe() -> None:
        with _DOWNLOAD_SUBSCRIBERS_LOCK:
            subscribers = _DOWNLOAD_SUBSCRIBERS.get(entry)
            if subscribers:
                subscribers.discard(subscriber)
                if not subscribers:
                    _DOWNLOAD_SUBSCRIBERS.pop(entry, None)

    return queue, unsubscribe


def _publish_download_event(entry: str, event: dict) -> None:
    """Fan out a NATS event to browser SSE subscribers in other event loops."""
    with _DOWNLOAD_SUBSCRIBERS_LOCK:
        subscribers = list(_DOWNLOAD_SUBSCRIBERS.get(entry, ()))
    for loop, queue in subscribers:
        if loop.is_closed():
            continue
        loop.call_soon_threadsafe(queue.put_nowait, event)


def get_download_events(entry: str) -> list[dict]:
    """Return the in-memory event snapshot for an ongoing download."""
    with _DOWNLOAD_SUBSCRIBERS_LOCK:
        return list(_DOWNLOAD_EVENTS.get(entry, ()))


def processor_mode() -> str:
    configured = getattr(settings.downloads, "processor", "legacy")
    nats_cfg = getattr(settings.downloads, "nats", None)
    if configured == "nats" and nats_cfg and nats_cfg.enabled:
        return "nats"
    return "legacy"


def _nats_config():
    cfg = settings.downloads.nats
    return cfg


def _nats_size_label(size_mb) -> str:
    if size_mb is None:
        return "Unknown size"
    try:
        return f"{float(size_mb):.1f} MiB"
    except (TypeError, ValueError):
        return "Unknown size"


def _nats_format_size(item: dict) -> float:
    try:
        size_mb = float(item.get("size_mb"))
    except (TypeError, ValueError):
        return float("inf")
    return size_mb if 0 <= size_mb < 999999 else float("inf")


def _nats_size_value(item: dict):
    return item.get("size_mb") if _nats_format_size(item) != float("inf") else None


async def _connect_nats():
    cfg = _nats_config()
    options = {"servers": [cfg.url]}
    if cfg.user:
        options.update(user=cfg.user, password=cfg.password or "")
    return await nats.connect(**options)


def _nats_subject(media_type: str, query: bool = False) -> str:
    action = "query" if query else "download"
    category = "movies" if media_type == "movie" else "songs"
    return f"nats.{action}.request.{category}"


def _nats_download_payload(job_id: str, data: dict) -> tuple[str, dict]:
    media_type = data.get("media_type", "song")
    video_format = data.get("video_format") or {}
    height = video_format.get("height") or data.get("quality")
    payload = {
        "id": job_id,
        "url": data["url"],
        "format": str(height) if height else None,
        "resolution": str(height) if height else None,
    }
    if media_type == "movie":
        # Service rule: lang "english" -> hollywood folder, anything else -> bollywood.
        lang, _ = resolve_movie_target(data.get("industry"), data.get("language"))
        payload["lang"] = lang
    else:
        payload["lang"] = data.get("language") or data.get("industry")
        payload["actress name"] = data.get("actress")
    payload["movie name"] = data.get("movie_name") or data.get("title")
    return _nats_subject(media_type), {key: value for key, value in payload.items() if value is not None}


def _nats_query_payload(request_id: str, url: str, media_type: str) -> tuple[str, dict]:
    return _nats_subject(media_type, query=True), {"id": request_id, "url": url}


def _record_download_event(entry: str, event: dict, host: str | None = None):
    status = str(event.get("status", "")).upper()
    mapped_status = "COMPLETED" if status == "COMPLETED" else "FAILED" if status == "FAILED" else status
    _tracker().update_download_status(
        entry,
        mapped_status or "DOWNLOADING",
        size=int(event.get("size") or 0),
        thumbnail=event.get("thumbnail"),
        processor="nats",
        service_host=event.get("hostname") or host,
    )
    with _DOWNLOAD_SUBSCRIBERS_LOCK:
        _DOWNLOAD_EVENTS.setdefault(entry, []).append(event)
        _DOWNLOAD_EVENTS[entry] = _DOWNLOAD_EVENTS[entry][-200:]
    _publish_download_event(entry, event)
    if status in {"COMPLETED", "FAILED"}:
        with _DOWNLOAD_SUBSCRIBERS_LOCK:
            _DOWNLOAD_EVENTS.pop(entry, None)


async def fetch_formats_nats(url: str, media_type: str = "song") -> dict:
    request_id = str(uuid.uuid4())
    subject, payload = _nats_query_payload(request_id, url, media_type)
    nc = await _connect_nats()
    result = asyncio.get_running_loop().create_future()

    async def on_message(msg):
        try:
            event = json.loads(msg.data.decode())
            logger.info("NATS format response for %s: %s", request_id, event)
            if event.get("status") in _NATS_TERMINAL_STATUSES and not result.done():
                result.set_result(event)
        except Exception as exc:
            if not result.done():
                result.set_exception(exc)

    try:
        await nc.subscribe(f"nats.query.response.{request_id}", cb=on_message)
        await nc.publish(subject, json.dumps(payload).encode())
        await nc.flush()
        event = await asyncio.wait_for(result, timeout=_nats_config().timeout_seconds)
    finally:
        await nc.drain()

    if event.get("status") == "failed":
        raise HTTPException(status_code=502, detail=event.get("error") or "NATS format lookup failed")

    # The service may return multiple streams at one height. Keep the smallest
    # known candidate so the UI and download request refer to the same choice.
    formats_by_height = {}
    for item in event.get("formats") or []:
        if item.get("kind") not in (None, "video", "both") or not item.get("height"):
            continue
        height = item["height"]
        current = formats_by_height.get(height)
        if current is None or _nats_format_size(item) < _nats_format_size(current):
            formats_by_height[height] = item
    formats = [formats_by_height[height] for height in sorted(formats_by_height)]

    audio = event.get("audio") or {}
    audio_id = audio.get("format_id") or event.get("audio_format")
    return {
        "url": event.get("url") or url,
        "title": event.get("title") or "",
        "suggested_filename": event.get("title") or "download",
        "video_formats": [
            {
                "format_id": item.get("format_id"),
                "height": item.get("height"),
                "width": item.get("width"),
                "ext": item.get("ext") or "mp4",
                "size_mb": _nats_size_value(item),
                "filesize_human": _nats_size_label(_nats_size_value(item)),
                "categorized_as": f"{item.get('height')}p" if item.get("height") else "",
            }
            for item in formats
        ],
        "audio_format": {
            "format_id": audio_id,
            "ext": audio.get("ext") or "m4a",
            "size_mb": _nats_size_value(audio),
            "filesize_human": _nats_size_label(_nats_size_value(audio)),
        } if audio_id else None,
        "nats_host": event.get("hostname"),
        "processor": "nats",
        "service_host": event.get("hostname"),
    }


async def _run_nats_download(job_id: str, data: dict, entry: str):
    subject, payload = _nats_download_payload(job_id, data)
    nc = await _connect_nats()
    terminal = asyncio.get_running_loop().create_future()
    async def on_message(msg):
        try:
            event = json.loads(msg.data.decode())
            status = event.get("status", "progress")
            updates = {
                "status": "completed" if status == "completed" else "failed" if status == "failed" else status,
                "nats_status": status,
                "progress": event.get("progress"),
                "message": event.get("message") or status,
                "service_host": event.get("hostname"),
            }
            _set_job(job_id, **updates)
            _record_download_event(entry, event)
            if status in _NATS_TERMINAL_STATUSES and not terminal.done():
                terminal.set_result(event)
        except Exception as exc:
            logger.exception("Invalid NATS download response for %s", job_id)
            if not terminal.done():
                terminal.set_exception(exc)

    try:
        await nc.subscribe(f"nats.download.response.{job_id}", cb=on_message)
        await nc.publish(subject, json.dumps(payload).encode())
        await nc.flush()
        await asyncio.wait_for(terminal, timeout=_nats_config().timeout_seconds)
    except Exception as exc:
        event = {"status": "failed", "error": str(exc), "hostname": socket.gethostname()}
        _set_job(job_id, status="failed", message=str(exc))
        _record_download_event(entry, event)
    finally:
        await nc.drain()


def start_nats_download(data: dict, entry: str) -> dict:
    job_id = str(uuid.uuid4())
    _set_job(job_id, status="queued", processor="nats", url=data.get("url"), progress=0)
    _tracker().update_download_status(
        entry, "PENDING", processor="nats", request_payload=data
    )
    _record_download_event(
        entry,
        {"status": "queued", "processor": "nats", "hostname": socket.gethostname()},
    )
    threading.Thread(
        target=lambda: asyncio.run(_run_nats_download(job_id, data, entry)), daemon=True
    ).start()
    return _set_job(job_id)

def sanitize_component(value: str | None, fallback: str = "") -> str:
    clean = _UNSAFE_PATH_CHARS.sub(" ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip().strip(".")
    return clean or fallback

def resolve_dlang(lang: str) -> str:
    l = (lang or "").strip().lower()
    if l == "hindi":
        return "Hindi"
    elif l == "marathi":
        return "Marathi"
    elif l in ("south", "telugu", "tamil", "kannada", "malyalam", "malayalam"):
        return "South"
    elif l == "bhojpuri":
        return "Bhojpuri"
    elif l == "english":
        return "English"
    return "Hindi"

def resolve_movie_target(industry: str | None, language: str | None) -> tuple[str, str]:
    """Return a consistent (lang, industry) pair for a movie download.

    English -> hollywood, anything else -> bollywood. An explicit, valid
    ``industry`` wins; otherwise the industry is inferred from ``language``.
    The returned ``lang`` always agrees with the returned industry, which is
    what the NATS movie service expects ("english" -> hollywood folder, else
    bollywood), so the legacy and NATS paths land in the same folder.
    """
    ind = (industry or "").strip().lower()
    lang = (language or "").strip().lower()
    if ind not in INDUSTRIES:
        ind = "hollywood" if lang == "english" else "bollywood"
    if ind == "hollywood":
        lang = "english"
    elif not lang or lang == "english":
        lang = "hindi"
    return lang, ind

def resolve_resolution(vformat: int) -> str:
    if vformat < 720:
        return "sd"
    elif vformat <= 1080:
        return "hd"
    return "xhd"

def _resolve_cookie_file() -> str | None:
    content = ""
    
    # 1. Read host mounted cookies if present
    if os.path.exists(HOST_COOKIE_FILE) and os.path.getsize(HOST_COOKIE_FILE) > 0:
        with open(HOST_COOKIE_FILE, "r") as f:
            content = f.read().strip()

    if not content:
        return None

    # Write content to a writable temporary file in /tmp so yt-dlp can modify/update it
    if not content.startswith("# Netscape HTTP Cookie File"):
        content = f"# Netscape HTTP Cookie File\n{content}"

    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir="/tmp", prefix="yt_cookies_", suffix=".txt", delete=False
    )
    tmp.write(content)
    tmp.close()
    return tmp.name

def options() -> dict:
    """Returns available categorization choices and metadata target options."""
    return {
        "target_heights": list(TARGET_HEIGHTS),
        "languages": list(LANGUAGES),
        "qualities": list(QUALITIES),
        "industries": list(INDUSTRIES),
        "media_types": list(MEDIA_TYPES),
        "songs_root": settings.downloads.songs_root,
        "movies_root": settings.downloads.movies_root,
    }


def _validate_url(url: str) -> bool:
    """Validates if the provided string is a properly formatted HTTP/HTTPS URL."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False
    
def fetch_formats(url: str, verbose: bool = False) -> dict:
    """Extracts formats via yt-dlp, mapping non-exact heights to the nearest
    target height tier and formatting metadata properly for the extension UI.
    """
    if not _validate_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL provided")

    cookie_file = _resolve_cookie_file()
    cookies_verified = bool(cookie_file and os.path.exists(cookie_file))

    # Fetch complete format and video metadata as JSON
    cmd = [
        YTDLP_BIN,
        "--js-runtimes", "node",
        "-J", url
    ]
    if cookie_file:
        cmd.extend(["--cookies", cookie_file])

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        info = json.loads(res.stdout)
    except Exception as exc:
        stderr_msg = getattr(exc, "stderr", str(exc))
        logger.error(f"yt-dlp format probe failed for {url}: {stderr_msg}")
        raise HTTPException(status_code=502, detail=f"Could not read media info: {stderr_msg}")
    finally:
        if cookie_file and cookie_file.startswith("/tmp/") and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass

    target_heights = (480, 720, 1080, 1440, 2160)
    candidates_by_target: dict[int, list[dict]] = {}
    audio_candidates: list[dict] = []

    formats = info.get("formats", [])
    for f in formats:
        # Check for audio-only streams
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        
        if vcodec == "none" and acodec != "none":
            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            audio_candidates.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext", "m4a"),
                "filesize": filesize,
                "filesize_human": format_file_size(filesize) if filesize else "Unknown size",
                "acodec": acodec,
            })
            continue

        actual_height = f.get("height")
        if actual_height:
            nearest_target = min(target_heights, key=lambda t: abs(t - actual_height))
            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            size_mb = filesize / (1024 * 1024) if filesize else 999999.0

            candidates_by_target.setdefault(nearest_target, []).append({
                "format_id": f.get("format_id"),
                "actual_height": actual_height,
                "target_height": nearest_target,
                "ext": f.get("ext", "mp4"),
                "size_mb": size_mb,
                "filesize": filesize,
                "filesize_human": format_file_size(filesize) if filesize else "Unknown size",
                "quality_tier": resolve_resolution(actual_height),
            })

    # Pick the stream with the smallest size for each assigned target height bucket
    filtered_video_formats = []
    for target_h in sorted(candidates_by_target.keys()):
        best_candidate = min(candidates_by_target[target_h], key=lambda x: x["size_mb"])
        filtered_video_formats.append({
            "format_id": best_candidate["format_id"],
            "height": best_candidate["actual_height"],
            "categorized_as": f"{best_candidate['target_height']}p",
            "quality_tier": best_candidate["quality_tier"],
            "ext": best_candidate["ext"],
            "filesize_human": best_candidate["filesize_human"],
            "size_mb": best_candidate["size_mb"] if best_candidate["size_mb"] < 999999 else None
        })

    # Pick best audio track
    best_audio = None
    if audio_candidates:
        best_audio = max(audio_candidates, key=lambda x: x["filesize"])

    duration = info.get("duration")

    return {
        "url": url,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "suggested_filename": sanitize_component(info.get("title"), "download"),
        "video_formats": filtered_video_formats,
        "audio_format": best_audio,
        "cookies_received": cookies_verified,
        "processor": "legacy",
        "service_host": socket.gethostname(),
    }

def plan_target(
    media_type: str,
    title: str | None = None,
    language: str | None = None,
    quality: str | int | None = None,
    actress: str | None = None,
    industry: str | None = None,
    movie_name: str | None = None,
) -> dict:
    root = settings.downloads.songs_root if media_type == "song" else settings.downloads.movies_root

    if media_type == "song":
        dlang = resolve_dlang(language or "Hindi")
        try:
            vfmt = int(quality) if quality else 720
        except ValueError:
            vfmt = 720
        resolution = resolve_resolution(vfmt)
        artist = sanitize_component(actress, "Unknown")
        
        directory = os.path.join(root, dlang, resolution, artist)
        stem = sanitize_component(title, "download")
    else:
        movie = sanitize_component(movie_name, "movie")
        _, ind = resolve_movie_target(industry, language)
        directory = os.path.join(root, ind, movie)
        stem = movie

    return {
        "media_type": media_type,
        "directory": os.path.normpath(directory),
        "filename": f"{stem}.mp4",
        "path": os.path.join(os.path.normpath(directory), f"{stem}.mp4"),
        "stem": stem,
    }

def _run_download(
    job_id: str,
    url: str,
    selector: str,
    target: dict,
) -> None:
    directory = target["directory"]
    # yt-dlp treats '%' specially in output templates, so escape literal ones.
    safe_dir = directory.replace("%", "%%")
    if target.get("media_type") == "movie":
        # Movies are named after the movie (matches the previewed target path),
        # not the source video's title.
        safe_stem = target["stem"].replace("%", "%%")
        output_template = os.path.join(safe_dir, f"{safe_stem}.%(ext)s")
    else:
        output_template = os.path.join(safe_dir, "%(title)s.%(ext)s")
    cookie_file = _resolve_cookie_file()

    os.makedirs(directory, exist_ok=True)

    cmd = [
        YTDLP_BIN,
        "--js-runtimes", "node",
        "-f", selector,
        "--embed-thumbnail",
        "--merge-output-format", "mp4",
        "-c",
        "-o", output_template,
        url
    ]

    if cookie_file:
        cmd.extend(["--cookies", cookie_file])

    logger.info(f"Executing: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    logs = []
    for line in process.stdout:
        clean_line = line.strip()
        if clean_line:
            logs.append(clean_line)
            match = re.search(r'(\d+\.\d+)%', clean_line)
            if match:
                pct = float(match.group(1))
                _set_job(job_id, status="running", progress=pct, message=clean_line)

    process.wait()

    if process.returncode == 0:
        _set_job(
            job_id,
            status="success",
            progress=100,
            message="Download completed successfully",
            path=target["path"]
        )
    else:
        _set_job(
            job_id,
            status="failed",
            message=f"yt-dlp exited with code {process.returncode}",
            log="\n".join(logs[-_MAX_LOG_LINES:])
        )

def start_download(
    url: str,
    video_format: dict | None,
    audio_format: dict | None,
    media_type: str,
    title: str | None = None,
    language: str | None = None,
    quality: str | None = None,
    actress: str | None = None,
    industry: str | None = None,
    movie_name: str | None = None,
    verbose: bool = False,
) -> dict:
    v_id = video_format.get("format_id") if video_format else None
    a_id = audio_format.get("format_id") if audio_format else "bestaudio"

    selector = f"{v_id}+{a_id}" if v_id else "bestvideo+bestaudio/best"

    target = plan_target(
        media_type=media_type,
        title=title,
        language=language,
        quality=quality,
        actress=actress,
        industry=industry,
        movie_name=movie_name,
    )

    job_id = str(uuid.uuid4())
    job = _set_job(job_id, status="queued", url=url, selector=selector, progress=0)

    thread = threading.Thread(
        target=_run_download,
        args=(job_id, url, selector, target),
        daemon=True,
    )
    thread.start()
    return job