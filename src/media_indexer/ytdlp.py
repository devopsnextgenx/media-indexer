import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from urllib.parse import urlparse

import yt_dlp
from fastapi import HTTPException

from media_indexer.config import settings
from media_indexer.utils import format_file_size

logger = logging.getLogger(__name__)

TARGET_HEIGHTS = (720, 1080, 1440, 2160)
LANGUAGES = ("Hindi", "South", "Marathi", "English", "Bhojpuri")
QUALITIES = ("xhd", "hd", "sd")
INDUSTRIES = ("bollywood", "hollywood")
MEDIA_TYPES = ("song", "movie")

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_LOG_LINES = 100

# Probe and download must use the same clients, otherwise the format IDs shown to the
# user do not exist at download time and yt-dlp silently falls back to a muxed 360p stream.
#
# IMPORTANT: "tv" authenticates via a separate device-code OAuth flow, not cookies. If a
# cookie jar is supplied alongside "tv", YouTube's innertube backend gets a mismatched
# session context and yt-dlp surfaces it as: ERROR: [youtube] <id>: The page needs to be
# reloaded. So the client list must drop "tv" whenever cookies are present, and probe/
# download/retry must all derive the list the same way or format IDs stop matching again.
YOUTUBE_PLAYER_CLIENTS_ANON = ("web", "mweb", "web_safari", "android_vr", "tv")
YOUTUBE_PLAYER_CLIENTS_AUTH = ("web", "mweb", "web_safari", "android_vr")
# "web" is the client that actually reads and sends the browser cookie jar for login;
# clients like "android_vr"/"tv" authenticate (if at all) through separate token flows
# and largely ignore cookies. Without "web" here, a signed-in-only video (age-restricted,
# members-only, etc.) can come back with zero usable formats even though cookies were
# verified and attached - which surfaces as "Requested format is not available" rather
# than an auth error. web_safari/android_vr are kept as fallbacks for nsig/PO-token
# issues that occasionally affect plain "web".
YOUTUBE_PLAYER_CLIENTS_AUTH = ("web", "web_safari", "android_vr")

# Kept as the anonymous default for any external caller still importing this name.
YOUTUBE_PLAYER_CLIENTS = YOUTUBE_PLAYER_CLIENTS_ANON

HOST_COOKIE_FILE = "/app/cookies/yt_cookies.txt"

def _resolve_cookie_file(cookies: str | None) -> str | None:
    if os.path.exists(HOST_COOKIE_FILE) and os.path.getsize(HOST_COOKIE_FILE) > 0:
        return HOST_COOKIE_FILE
    if cookies and cookies.strip():
            return _create_temp_cookie_file(cookies)
    return None

def _player_clients(has_cookies: bool) -> tuple[str, ...]:
    return YOUTUBE_PLAYER_CLIENTS_AUTH if has_cookies else YOUTUBE_PLAYER_CLIENTS_ANON


def _extractor_args_py(has_cookies: bool) -> dict:
    return {
        "youtube": {
            "player_client": list(_player_clients(has_cookies)),
            "player_skip": ["js"], # Skip JS execution locks where applicable
        }
    }


def _extractor_args_cli(has_cookies: bool) -> str:
    return f"youtube:player_client={','.join(_player_clients(has_cookies))}"


# A CDN 403 usually means the media URL was signed for a client whose headers/PO token no
# longer match, so retry with a single client at a time before giving up. "tv" is only
# offered as a retry option when there are no cookies, for the same reason as above.
_RETRY_PLAYER_CLIENTS_ANON = (("android_vr",), ("web_safari",), ("tv",))
_RETRY_PLAYER_CLIENTS_AUTH = (("web",), ("android_vr",), ("web_safari",))


def _retry_player_clients(has_cookies: bool) -> tuple[tuple[str, ...], ...]:
    return _RETRY_PLAYER_CLIENTS_AUTH if has_cookies else _RETRY_PLAYER_CLIENTS_ANON


_FORBIDDEN_RE = re.compile(r"HTTP Error 403|403: Forbidden", re.IGNORECASE)

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

def _set_job(job_id: str, **updates) -> dict:
    """Thread-safe updates to the in-memory download job registry."""
    with _JOBS_LOCK:
        job = _JOBS.setdefault(job_id, {"id": job_id})
        job.update(updates)
        return dict(job)


def get_job(job_id: str) -> dict:
    """Thread-safe lookup for download job state."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown download job")
    return dict(job)

def sanitize_component(value: str | None, fallback: str = "") -> str:
    clean = _UNSAFE_PATH_CHARS.sub(" ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip().strip(".")
    return clean or fallback


def quality_for_height(height: int | None) -> str:
    if not height:
        return "sd"
    if height > 1080:
        return "xhd"
    if height >= 720:
        return "hd"
    return "sd"


def options() -> dict:
    """Static config exposed to the extension/web UI for populating the
    language/quality/industry dropdowns and the target-path preview.
    Referenced by main.py's GET /api/ytdlp/options - was missing entirely,
    which is why that route 500'd with AttributeError."""
    return {
        "languages": list(LANGUAGES),
        "qualities": list(QUALITIES),
        "industries": list(INDUSTRIES),
        "media_types": list(MEDIA_TYPES),
        "songs_root": settings.downloads.songs_root,
        "movies_root": settings.downloads.movies_root,
    }


def _validate_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only http(s) media URLs are supported")
    return parsed.geturl()


def _format_bytes(fmt: dict, duration: float | None) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if not size and duration and fmt.get("tbr"):
        size = int(fmt["tbr"] * 1000 / 8 * duration)
    return int(size or 0)


def _describe(fmt: dict, duration: float | None) -> dict:
    size = _format_bytes(fmt, duration)
    return {
        "format_id": fmt.get("format_id"),
        "ext": fmt.get("ext"),
        "height": fmt.get("height"),
        "fps": fmt.get("fps"),
        "vcodec": fmt.get("vcodec"),
        "acodec": fmt.get("acodec"),
        "abr": fmt.get("abr"),
        "filesize": size,
        "filesize_human": format_file_size(size) if size else "unknown",
        "has_audio": fmt.get("acodec") not in (None, "none"),
    }


def _create_temp_cookie_file(cookies: str | None) -> str | None:
    if not cookies or not cookies.strip():
        return None

    content = cookies.strip()
    if not content.startswith("# Netscape HTTP Cookie File"):
        content = f"# Netscape HTTP Cookie File\n{content}"

    tmp = tempfile.NamedTemporaryFile(mode="w", dir="/tmp", prefix="yt_cookies_", suffix=".txt", delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


def _verify_cookies(cookies: str | None, cookie_file: str | None, context: str) -> bool:
    """Confirms cookies actually reached this request and produced a usable cookie file.

    Never logs cookie contents/values - only presence, entry count, and the temp file path.
    Returns True only if cookies were supplied AND resulted in a non-empty cookie file.
    """
    if cookies is None or not cookies.strip():
        logger.warning(f"{context}: no cookies were included in the request body")
        return False

    if not cookie_file or not os.path.exists(cookie_file):
        logger.error(f"{context}: cookies string was present but no cookie file was created")
        return False

    try:
        with open(cookie_file, "r") as f:
            entry_count = sum(
                1 for line in f
                if line.strip() and not line.strip().startswith("#")
            )
    except OSError as exc:
        logger.error(f"{context}: failed to read back cookie file {cookie_file}: {exc}")
        return False

    if entry_count == 0:
        logger.warning(
            f"{context}: cookie file {cookie_file} was created but contains no cookie "
            f"entries - check the pasted content is Netscape cookies.txt format (tab-separated)"
        )
        return False

    logger.info(f"{context}: cookies verified - {entry_count} entries in {cookie_file}")
    return True


def fetch_formats(url: str, cookies: str | None = None, verbose: bool = False) -> dict:
    url = _validate_url(url)
    cookie_file = _resolve_cookie_file(cookies)
    cookies_verified = _verify_cookies(cookies, cookie_file, f"Format probe for {url}")

    opts = {
        "quiet": not verbose,
        "verbose": verbose,
        "no_warnings": not verbose,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "noplaylist": True,
        "extractor_args": _extractor_args_py(cookies_verified),
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.error(f"yt-dlp format probe failed for {url}: {exc}")
        raise HTTPException(status_code=502, detail=f"Could not read media info: {exc}")
    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise HTTPException(status_code=404, detail="No downloadable entry found at that URL")
        info = entries[0]

    duration = info.get("duration")
    formats = info.get("formats") or []

    # Format Logging to Server Console
    logger.info(f"--- Discovered Formats for: {url} ---")
    for fmt in formats:
        logger.info(
            f"ID: {fmt.get('format_id'):<8} | "
            f"Ext: {fmt.get('ext'):<5} | "
            f"Res: {fmt.get('height')}p | "
            f"VCodec: {fmt.get('vcodec')} | "
            f"ACodec: {fmt.get('acodec')} | "
            f"Protocol: {fmt.get('protocol')} | "
            f"HasURL: {bool(fmt.get('url'))} | "
            f"FormatNote: {fmt.get('format_note')}"
        )

    video_candidates: dict[int, list[dict]] = {}
    audio_candidates: list[dict] = []

    for fmt in formats:
        if fmt.get("format_note") == "storyboard" or fmt.get("ext") == "mhtml":
            continue
        has_video = fmt.get("vcodec") not in (None, "none")
        has_audio = fmt.get("acodec") not in (None, "none")

        if has_video:
            height = fmt.get("height")
            if height:
                video_candidates.setdefault(height, []).append(fmt)
        elif has_audio:
            audio_candidates.append(fmt)

    def smallest(items: list[dict]) -> dict:
        return min(items, key=lambda f: _format_bytes(f, duration) or float("inf"))

    best_video = {}
    selected_heights = [h for h in TARGET_HEIGHTS if h in video_candidates]
    if not selected_heights:
        selected_heights = sorted(video_candidates.keys(), reverse=True)[:4]

    for height in selected_heights:
        items = video_candidates[height]
        video_only = [f for f in items if f.get("acodec") in (None, "none")]
        best_video[height] = smallest(video_only or items)

    best_audio = smallest(audio_candidates) if audio_candidates else None
    video_formats = [_describe(best_video[h], duration) for h in sorted(best_video)]

    return {
        "url": url,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "suggested_filename": sanitize_component(info.get("title"), "download"),
        "video_formats": video_formats,
        "audio_format": _describe(best_audio, duration) if best_audio else None,
        "cookies_received": cookies_verified,
    }


def _fallback_selector(height: int | None) -> str:
    """Resolution-bounded fallback so a missing format ID cannot degrade to 360p."""
    if not height:
        return "bestvideo*+bestaudio/best"
    return (
        f"bestvideo[height={height}]+bestaudio/"
        f"bestvideo[height>={height}]+bestaudio/"
        f"best[height>={height}]"
    )


def _clear_partials(directory: str, stem: str) -> None:
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name.startswith(stem) and (name.endswith(".part") or name.endswith(".ytdl")):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def _stream_ytdlp(
    job_id: str,
    cmd: list[str],
    selector: str,
    verbose: bool,
    v_desc: str,
    a_desc: str,
) -> tuple[int, list[str], str | None]:
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    logs: list[str] = []
    chosen_formats = None
    for line in process.stdout:
        clean_line = line.strip()
        if not clean_line:
            continue

        logs.append(clean_line)
        if verbose:
            logger.info(f"[yt-dlp {job_id[-6:]}] {clean_line}")

        chosen = re.search(r"Downloading \d+ format\(s\): (\S+)", clean_line)
        if chosen:
            chosen_formats = chosen.group(1)
            if selector and chosen_formats != selector:
                logger.warning(
                    f"Job {job_id}: requested formats [{selector}] unavailable, "
                    f"yt-dlp fell back to [{chosen_formats}]"
                )

        match = re.search(r"\[download\]\s+(\d+\.\d+)%", clean_line)
        if match:
            pct = float(match.group(1))
            _set_job(
                job_id,
                status="running",
                progress=pct,
                message=f"Downloading [{v_desc} + {a_desc}] - {pct:.1f}%"
            )

    process.wait()
    return process.returncode, logs, chosen_formats


def _run_download(
    job_id: str,
    url: str,
    selector: str,
    target: dict,
    video_info: dict | None = None,
    audio_info: dict | None = None,
    cookies: str | None = None,
    verbose: bool = True,
) -> None:
    verbose = True
    directory = target["directory"]
    output_template = os.path.join(directory, f"{target['stem']}.%(ext)s")
    cookie_file = _resolve_cookie_file(cookies)
    cookies_verified = _verify_cookies(cookies, cookie_file, f"Download job {job_id}")
    if not cookies_verified:
        logger.warning(
            f"Job {job_id}: proceeding WITHOUT verified cookies - YouTube is likely to "
            f"return HTTP 403 partway through the download for this client"
        )

    fallback = _fallback_selector(video_info.get("height") if video_info else None)

    # If specific formats were chosen, use explicit selector first:
    if selector and selector != "bestvideo+bestaudio/best":
        robust_selector = f"{selector}/{fallback}"
    else:
        robust_selector = fallback

    v_desc = f"{video_info.get('height')}p ({video_info.get('format_id')})" if video_info else "Best Video"
    a_desc = f"{audio_info.get('format_id')} ({audio_info.get('ext')})" if audio_info else "Best Audio / Muxed"

    start_msg = f"Downloading Video: [{v_desc}] | Audio: [{a_desc}] [{robust_selector}]"
    logger.info(f"Job {job_id} starting -> {start_msg}")

    _set_job(
        job_id,
        status="running",
        message=start_msg,
        video_format=video_info,
        audio_format=audio_info,
        progress=0,
        cookies_received=cookies_verified,
    )

    attempts = [_extractor_args_cli(cookies_verified)] + [
        f"youtube:player_client={','.join(clients)}"
        for clients in _retry_player_clients(cookies_verified)
    ]

    try:
        os.makedirs(directory, exist_ok=True)

        for attempt, extractor_args in enumerate(attempts):
            if attempt:
                # Signed URLs in the partial file are dead once we switch clients.
                _clear_partials(directory, target["stem"])
                logger.warning(
                    f"Job {job_id}: retrying after HTTP 403 with [{extractor_args}]"
                )
                _set_job(job_id, status="running", progress=0,
                         message=f"Retrying after 403 with {extractor_args.split('=')[-1]}")

            cmd = [
                sys.executable, "-m", "yt_dlp",
                "-f", robust_selector,
                "--no-playlist",
                "--newline",
                "--no-continue" if attempt else "-c",
                "--retries", "10",
                "--fragment-retries", "10",
                "--extractor-retries", "5",
                "--embed-thumbnail",
                "--add-metadata",
                "--merge-output-format", "mp4",
                "--extractor-args", extractor_args,
                "-o", output_template,
            ]

            if verbose:
                cmd.append("-v")

            if cookie_file:
                cmd.extend(["--cookies", cookie_file])

            cmd.append(url)

            returncode, logs, chosen_formats = _stream_ytdlp(
                job_id, cmd, selector, verbose, v_desc, a_desc
            )

            if returncode == 0:
                break
            if not any(_FORBIDDEN_RE.search(line) for line in logs):
                break
    except Exception as exc:
        logger.error(f"Download job {job_id} encountered exception: {exc}")
        _set_job(job_id, status="failed", message=str(exc))
        return
    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass

    tail = "\n".join(logs[-_MAX_LOG_LINES:])

    if returncode != 0:
        logger.error(f"Download job {job_id} failed: {tail}")
        forbidden = any(_FORBIDDEN_RE.search(line) for line in logs)
        message = (
            "Media host returned HTTP 403 for every player client; supply fresh cookies or retry later"
            if forbidden else "yt-dlp exited with an error"
        )
        _set_job(job_id, status="failed", message=message, log=tail)
        return

    final_path = target["path"]
    exists = os.path.isfile(final_path)
    downgraded = bool(selector and chosen_formats and chosen_formats != selector)
    suffix = f" (requested {selector}, got {chosen_formats})" if downgraded else ""
    _set_job(
        job_id,
        status="success" if exists else "completed",
        progress=100,
        downgraded=downgraded,
        chosen_formats=chosen_formats,
        message=f"Finished! Video [{v_desc}] + Audio [{a_desc}] saved to {final_path}{suffix}" if exists else "Completed",
        path=final_path,
        size_human=format_file_size(os.path.getsize(final_path)) if exists else None,
        log=tail,
    )

def _root_for(media_type: str) -> str:
    if media_type == "song":
        return settings.downloads.songs_root
    if media_type == "movie":
        return settings.downloads.movies_root
    raise HTTPException(status_code=400, detail=f"media_type must be one of {list(MEDIA_TYPES)}")


def plan_target(
    media_type: str,
    title: str | None = None,
    language: str | None = None,
    quality: str | None = None,
    actress: str | None = None,
    industry: str | None = None,
    movie_name: str | None = None,
) -> dict:
    """Maps plugin metadata onto the on-disk folder convention for songs and movies."""
    root = _root_for(media_type)

    if media_type == "song":
        if language not in LANGUAGES:
            raise HTTPException(status_code=400, detail=f"language must be one of {list(LANGUAGES)}")
        if quality not in QUALITIES:
            raise HTTPException(status_code=400, detail=f"quality must be one of {list(QUALITIES)}")
        artist = sanitize_component(actress)
        if not artist:
            raise HTTPException(status_code=400, detail="actress name is required for songs")
        directory = os.path.join(root, language, quality, artist)
        stem = sanitize_component(title, "download")
    else:
        if industry not in INDUSTRIES:
            raise HTTPException(status_code=400, detail=f"industry must be one of {list(INDUSTRIES)}")
        movie = sanitize_component(movie_name)
        if not movie:
            raise HTTPException(status_code=400, detail="movie name is required for movies")
        directory = os.path.join(root, industry, movie)
        stem = movie

    directory = os.path.normpath(directory)
    if os.path.commonpath([directory, os.path.normpath(root)]) != os.path.normpath(root):
        raise HTTPException(status_code=400, detail="Resolved target path escapes the media root")

    return {
        "media_type": media_type,
        "directory": directory,
        "filename": f"{stem}.mp4",
        "path": os.path.join(directory, f"{stem}.mp4"),
        "stem": stem,
    }

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
    verbose: bool = False,
) -> dict:
    url = _validate_url(url)
    v_id = video_format.get("format_id") if video_format else None
    a_id = audio_format.get("format_id") if audio_format else None

    cookies_present = bool(cookies and cookies.strip())
    if not cookies_present:
        logger.warning(f"start_download for {url}: request received with no cookies")

    target = plan_target(
        media_type=media_type,
        title=title,
        language=language,
        quality=quality,
        actress=actress,
        industry=industry,
        movie_name=movie_name,
    )

    if v_id and a_id:
        selector = f"{v_id}+{a_id}"
    elif v_id:
        selector = v_id
    else:
        selector = "bestvideo+bestaudio/best"

    job_id = str(uuid.uuid4())
    job = _set_job(
        job_id,
        status="queued",
        url=url,
        selector=selector,
        message="Queued",
        video_format=video_format,
        audio_format=audio_format,
        # Provisional flag from the request itself; _run_download overwrites this with
        # the verified result (cookie file actually created + non-empty) once it starts.
        cookies_received=cookies_present,
        **{k: target[k] for k in ("media_type", "directory", "filename", "path")},
    )

    thread = threading.Thread(
        target=_run_download,
        args=(job_id, url, selector, target, video_format, audio_format, cookies, verbose),
        daemon=True,
    )
    thread.start()
    return job