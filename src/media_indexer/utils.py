import os
import re
import uuid
import json
import subprocess
import logging

logger = logging.getLogger(__name__)

def generate_file_id(file_path: str) -> str:
    """Generate deterministic UUID based on relative file path."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, file_path))

def normalize_text(text: str) -> str:
    """
    Normalizes filenames by replacing underscores with spaces, removing 
    extraneous web tags, and collapsing multiple spaces.
    """
    # Replace underscores with space
    clean = text.replace("_", " ")
    # Remove common video extensions and bracketed tags like [1080p], [720p], [yt_id]
    clean = re.sub(r"\[.*?\]|\(.*?\)", " ", clean)
    clean = re.sub(r"\.(mp4|mkv|avi|webm|mp3|m4a|flv)$", "", clean, flags=re.I)
    # Collapse multiple whitespaces
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}

TICKS_PER_SECOND = 10_000_000


def format_file_size(size_bytes: int) -> str:
    if not size_bytes:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.2f} {units[i]}"

def format_duration(seconds: float | int | None) -> str:
    """Formats a duration in seconds as hh:mm:ss."""
    if not seconds or seconds <= 0:
        return "00:00:00"
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

def quality_label(width: int | None, height: int | None) -> str:
    """Maps a resolution onto a common quality bucket label."""
    h = height or 0
    if not h:
        return "N/A"
    if h >= 2000 or (width or 0) >= 3800:
        return "4K"
    if h >= 1400:
        return "1440p"
    if h >= 1000:
        return "1080p"
    if h >= 700:
        return "720p"
    if h >= 470:
        return "480p"
    return "SD"

def build_media_metadata(file_path: str, jf_meta: dict | None = None) -> dict:
    """Merges Jellyfin metadata with ffprobe/stat fallbacks for any missing field."""
    jf = jf_meta or {}
    is_audio = os.path.splitext(file_path)[1].lower() in AUDIO_EXTENSIONS

    width = jf.get("width") or 0
    height = jf.get("height") or 0
    size = jf.get("size") or 0
    ticks = jf.get("runtime_ticks") or 0
    duration = round(ticks / TICKS_PER_SECOND, 2) if ticks else 0

    if not size:
        try:
            size = os.stat(file_path).st_size
        except OSError as e:
            logger.debug(f"Could not stat {file_path}: {e}")
            size = 0

    # Audio files legitimately have no resolution, so only probe them for duration
    if not duration or (not is_audio and not (width and height)):
        probed = extract_media_metadata(file_path)
        width = width or probed["width"]
        height = height or probed["height"]
        duration = duration or probed["duration"]
        size = size or probed["file_size"]

    if duration and not ticks:
        ticks = int(duration * TICKS_PER_SECOND)

    if width and height:
        resolution = f"{width}x{height}"
    else:
        resolution = "Audio" if is_audio else "N/A"

    return {
        "width": width,
        "height": height,
        "resolution": resolution,
        "quality": "Audio" if is_audio else quality_label(width, height),
        "duration": duration,
        "duration_formatted": format_duration(duration),
        "runtime_ticks": ticks or None,
        "size": size,
        "file_size": size,
        "file_size_human": format_file_size(size),
        "primary_image_tag": jf.get("primary_image_tag"),
    }

def extract_media_metadata(file_path: str) -> dict:
    """Uses ffprobe to extract resolution, duration, bitrate, and format details."""
    try:
        file_size = os.stat(file_path).st_size
    except OSError:
        file_size = 0
    meta = {
        "file_size": file_size,
        "file_size_human": format_file_size(file_size),
        "duration": 0,
        "duration_formatted": "00:00:00",
        "resolution": "Audio/Unknown",
        "width": 0,
        "height": 0,
        "format_name": os.path.splitext(file_path)[1].lstrip('.').lower()
    }

    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        file_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            duration = float(fmt.get("duration", 0) or 0)
            meta["duration"] = round(duration, 2)
            meta["duration_formatted"] = format_duration(duration)

            if not meta["file_size"]:
                meta["file_size"] = int(fmt.get("size", 0) or 0)
                meta["file_size_human"] = format_file_size(meta["file_size"])

            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    w = stream.get("width", 0)
                    h = stream.get("height", 0)
                    if w and h:
                        meta["width"] = w
                        meta["height"] = h
                        meta["resolution"] = f"{w}x{h}"
                    break
    except Exception as e:
        logger.warning(f"Could not extract ffprobe metadata for {file_path}: {e}")

    return meta

def generate_thumbnail_bytes(file_path: str) -> bytes | None:
    """Generates JPEG thumbnail image buffer at 10% media position using FFmpeg."""
    cmd = [
        "ffmpeg", "-ss", "00:00:02", "-i", file_path,
        "-vframes", "1", "-vf", "scale=320:-1",
        "-f", "image2", "-c:v", "mjpeg", "pipe:1"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
        if res.returncode == 0 and len(res.stdout) > 0:
            return res.stdout
    except Exception as e:
        logger.error(f"Thumbnail generation error for {file_path}: {e}")
    return None