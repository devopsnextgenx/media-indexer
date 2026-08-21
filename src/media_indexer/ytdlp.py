import logging
import os
import re
import subprocess
import tempfile
import threading
import uuid
from urllib.parse import urlparse
from fastapi import HTTPException

from media_indexer.config import settings
from media_indexer.utils import format_file_size

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

def resolve_resolution(vformat: int) -> str:
    if vformat < 720:
        return "sd"
    elif vformat <= 1080:
        return "hd"
    return "xhd"

def _resolve_cookie_file(cookies: str | None = None) -> str | None:
    content = ""
    
    # 1. Read host mounted cookies if present
    if os.path.exists(HOST_COOKIE_FILE) and os.path.getsize(HOST_COOKIE_FILE) > 0:
        with open(HOST_COOKIE_FILE, "r") as f:
            content = f.read().strip()
            
    # 2. Fall back to passed cookies parameter if host file is empty/missing
    elif cookies and cookies.strip():
        content = cookies.strip()

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
    
def fetch_formats(url: str, cookies: str | None = None, verbose: bool = False) -> dict:
    """Extracts formats via yt-dlp using cookies, mapping non-exact heights to the nearest
    target height tier (480p, 720p, 1080p, 1440p, 2160p) and selecting the stream with
    the lowest file size for each tier.
    """
    if not _validate_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL provided")

    cookie_file = _resolve_cookie_file(cookies)

    cmd = [
        YTDLP_BIN,
        "--js-runtimes", "node",
        "-F", url
    ]
    if cookie_file:
        cmd.extend(["--cookies", cookie_file])

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        fmt_list = res.stdout
    except subprocess.CalledProcessError as exc:
        logger.error(f"yt-dlp format probe failed for {url}: {exc.stderr}")
        raise HTTPException(status_code=502, detail=f"Could not read media info: {exc.stderr}")
    finally:
        if cookie_file and cookie_file.startswith("/tmp/") and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass

    candidates_by_target: dict[int, list[dict]] = {}
    audio_candidates: list[dict] = []

    target_heights = (480, 720, 1080, 1440, 2160)

    for line in fmt_list.splitlines():
        if re.search(r'audio only|storyboard|images', line, re.IGNORECASE):
            if "audio only" in line:
                parts = line.split()
                if parts:
                    audio_candidates.append({"format_id": parts[0], "ext": "m4a", "acodec": "audio"})
            continue

        if not re.match(r'^[0-9]+', line):
            continue

        parts = line.split()
        fid = parts[0]

        # Extract actual height (e.g., 540p or 960x540)
        m_h = re.search(r'\b([0-9]+)p\b', line)
        if not m_h:
            m_h = re.search(r'(?<=x)[0-9]+', line)
        actual_height = int(m_h.group(1)) if m_h else None

        if actual_height:
            # Map actual height to the nearest available target height
            nearest_target = min(target_heights, key=lambda t: abs(t - actual_height))

            # Parse size in MiB or KiB to pick the lowest file size
            size_mb = 999999.0
            size_match = re.search(r'~?\s*([0-9]+(?:\.[0-9]+)?)\s*MiB', line)
            if size_match:
                size_mb = float(size_match.group(1))
            else:
                kib_match = re.search(r'~?\s*([0-9]+(?:\.[0-9]+)?)\s*KiB', line)
                if kib_match:
                    size_mb = float(kib_match.group(1)) / 1024.0

            candidates_by_target.setdefault(nearest_target, []).append({
                "format_id": fid,
                "actual_height": actual_height,
                "target_height": nearest_target,
                "size_mb": size_mb,
                "quality_tier": resolve_resolution(actual_height),
            })

    # Pick the stream with the smallest size_mb for each assigned target height bucket
    filtered_video_formats = []
    for target_h in sorted(candidates_by_target.keys()):
        best_candidate = min(candidates_by_target[target_h], key=lambda x: x["size_mb"])
        filtered_video_formats.append({
            "format_id": best_candidate["format_id"],
            "height": best_candidate["actual_height"],
            "categorized_as": f"{best_candidate['target_height']}p",
            "quality_tier": best_candidate["quality_tier"],
            "size_mb": best_candidate["size_mb"] if best_candidate["size_mb"] < 999999 else None
        })

    best_audio = audio_candidates[0] if audio_candidates else {"format_id": "bestaudio"}

    return {
        "url": url,
        "video_formats": filtered_video_formats,
        "audio_format": best_audio,
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
        ind = sanitize_component(industry, "bollywood")
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
    cookies: str | None = None,
) -> None:
    directory = target["directory"]
    output_template = os.path.join(directory, "%(title)s.%(ext)s")
    cookie_file = _resolve_cookie_file(cookies)

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
    cookies: str | None = None,
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
        args=(job_id, url, selector, target, cookies),
        daemon=True,
    )
    thread.start()
    return job