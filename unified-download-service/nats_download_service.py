#!/usr/bin/env python3
"""
nats_download_service.py
------------------------
Single systemd-managed worker that consumes NATS download/query requests,
processes them sequentially (no locks; FIFO queue) and publishes live status
back on nats.download.response.<id> / nats.query.response.<id>.

Subscribes:
  nats.download.request.{songs,movies,aria2c,mp3,serials}
  nats.query.request.{songs,movies}

Publishes:
  nats.download.response.<id>
  nats.query.response.<id>

Events emitted for yt-dlp based downloads:
  started   (Resolving formats / Download started)
  started   (Downloading <video|audio> stream i/n)   phase=video|audio
  progress  (progress=0..100, phase=video|audio)
  merging
  moving
  completed | failed
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import nats
from nats.aio.client import Client as NATS

# ─────────────────────────────── config ────────────────────────────────
HOME        = Path.home()
BIN         = HOME / "bin"
YTDLP       = str(BIN / "yt-dlp")           # or set absolute path
ARIA2C      = "/usr/bin/aria2c"

HOSTNAME    = socket.gethostname()
# zbox mounts the disks under /media/data, every other host under /media/zbox
MEDIA_BASE  = (Path("/media/data")
               if HOSTNAME.split(".")[0].lower() == "zbox"
               else Path("/media/zbox"))

STORAGE_SNG = MEDIA_BASE / "Crucial-X6" / "ShareMe" / "media"
STORAGE_MOV = MEDIA_BASE / "storage" / "ShareMe" / "media"
SONGS_ROOT  = STORAGE_SNG / "songs" / "target"
MOVIES_ROOT = STORAGE_MOV / "movies"
SERIALS_ROOT= STORAGE_SNG / "serials" / "TV Shows"
MUSIC_ROOT  = HOME / "Music" / "audio"
TMP_DIR     = HOME / "tmp"

COOKIE_SOURCE = os.environ.get("NATS_COOKIE_FILE")
COOKIE_FILE = Path(COOKIE_SOURCE) if COOKIE_SOURCE else TMP_DIR / "cookies.txt"

NATS_URL             = os.environ.get("NATS_URL", "nats://192.168.12.111:4222")
NATS_USER            = os.environ.get("NATS_USER") or None
NATS_PASSWORD        = os.environ.get("NATS_PASSWORD") or None
NATS_TOKEN           = os.environ.get("NATS_TOKEN") or None
NATS_CREDS_FILE      = os.environ.get("NATS_CREDS") or None   # path to .creds
NATS_NKEY_SEED       = os.environ.get("NATS_NKEY_SEED") or None
NATS_QUEUE_GROUP     = os.environ.get("NATS_QUEUE_GROUP", "nats-download-service")
COOKIE_REFRESH_SEC   = 15 * 60
PROGRESS_STEP        = 10        # percent increments for progress events

DOWNLOAD_TOPICS = {
    "songs":   "nats.download.request.songs",
    "movies":  "nats.download.request.movies",
    "aria2c":  "nats.download.request.aria2c",
    "mp3":     "nats.download.request.mp3",
    "serials": "nats.download.request.serials",
}
QUERY_TOPICS = {
    "songs":  "nats.query.request.songs",
    "movies": "nats.query.request.movies",
}

LOG = logging.getLogger("nats-download")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ───────────────────────────── cookie manager ──────────────────────────
class CookieManager:
    """Refreshes cookies from the browser into one shared file every 15 min."""

    def __init__(self, dest: Path, interval: int = COOKIE_REFRESH_SEC):
        self.dest = dest
        self.interval = interval
        self.browser_refresh = COOKIE_SOURCE is None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def refresh(self) -> bool:
        if not self.browser_refresh:
            return self.dest.exists()
        async with self._lock:
            self.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.dest.with_suffix(".new")
            try:
                proc = await asyncio.create_subprocess_exec(
                    YTDLP, "--cookies-from-browser", "chrome",
                    "--cookies", str(tmp),
                    "--skip-download", "--no-warnings",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0 and tmp.exists():
                    tmp.replace(self.dest)
                    LOG.info("cookies refreshed -> %s", self.dest)
                    return True
                LOG.warning("cookie refresh failed rc=%s", proc.returncode)
            except Exception as e:  # noqa
                LOG.exception("cookie refresh error: %s", e)
            finally:
                tmp.unlink(missing_ok=True)
            return False

    async def _loop(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                LOG.exception("cookie loop error")
            await asyncio.sleep(self.interval)

    def start(self):
        if self.browser_refresh:
            self._task = asyncio.create_task(self._loop())
        else:
            LOG.info("using configured cookie file -> %s", self.dest)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def path(self) -> Optional[str]:
        return str(self.dest) if self.dest.exists() else None


# ──────────────────────────── storage helpers ──────────────────────────
def resolve_dlang(lang: str) -> str:
    l = (lang or "").lower()
    if l == "hindi":     return "Hindi"
    if l == "marathi":   return "Marathi"
    if l in ("south", "telugu", "tamil", "kannada", "malyalam", "malayalam"):
        return "South"
    if l == "bhojpuri":  return "Bhojpuri"
    if l == "english":   return "English"
    return "Hindi"


def resolve_resolution(height: int) -> str:
    if height < 720:   return "sd"
    if height <= 1080: return "hd"
    return "xhd"


def songs_dest(lang: str, height: int, actress: str) -> Path:
    return SONGS_ROOT / resolve_dlang(lang) / resolve_resolution(height) / (actress or "Unknown")


def movies_dest(lang: str, movie_name: str) -> Path:
    folder = "hollywood" if (lang or "").lower() == "english" else "bollywood"
    return MOVIES_ROOT / folder / (movie_name or "Untitled")


def serials_dest(lang: str, name: str) -> Path:
    return SERIALS_ROOT / resolve_dlang(lang) / (name or "Untitled")


# ───────────────────────── format list parsing ─────────────────────────
def parse_formats(fmt_list: str) -> list[tuple[str, int, Optional[float], str]]:
    """Return [(format_id, height, size_mb, ext)] for video formats."""
    out: list[tuple[str, int, float, str]] = []
    for line in fmt_list.splitlines():
        if re.search(r"audio only|storyboard|images", line, re.I):
            continue
        if not re.match(r"^\d", line):
            continue
        parts = line.split()
        if not parts:
            continue
        fid = parts[0]
        ext = parts[1] if len(parts) > 1 else "mp4"
        m = re.search(r"\b(\d+)p\b", line) or re.search(r"x(\d+)", line)
        if not m:
            continue
        height = int(m.group(1))
        m2 = re.search(r"~?\s*([\d.]+)\s*MiB", line)
        if m2:
            size_mb = float(m2.group(1))
        else:
            m3 = re.search(r"~?\s*([\d.]+)\s*KiB", line)
            size_mb = float(m3.group(1)) / 1024 if m3 else None
        out.append((fid, height, size_mb, ext))
    return out


def _format_size_key(size_mb: Optional[float]) -> float:
    return size_mb if size_mb is not None else float("inf")


def pick_video_format(formats: list[tuple[str, int, float]], target_h: int) -> Optional[str]:
    if not formats:
        return None
    exact = [f for f in formats if f[1] == target_h]
    pool = exact or [f for f in formats if f[1] == min(formats, key=lambda f: abs(f[1] - target_h))[1]]
    return min(pool, key=lambda f: _format_size_key(f[2]))[0]


def pick_audio_format(fmt_list: str) -> str:
    for line in fmt_list.splitlines():
        if "audio only" in line and re.search(r"opus|m4a", line, re.I):
            return line.split()[0]
    return "bestaudio"


def parse_audio_format(fmt_list: str) -> dict:
    """Return the smallest known opus/m4a audio candidate and its metadata."""
    candidates = []
    for line in fmt_list.splitlines():
        if "audio only" not in line or not re.search(r"opus|m4a", line, re.I):
            continue
        parts = line.split()
        if not parts:
            continue
        match = re.search(r"~?\s*([\d.]+)\s*(MiB|KiB)", line)
        size_mb = None
        if match:
            size_mb = float(match.group(1)) / (1024 if match.group(2) == "KiB" else 1)
        candidates.append({
            "format_id": parts[0],
            "ext": parts[1] if len(parts) > 1 else "m4a",
            "size_mb": size_mb,
        })
    return min(candidates, key=lambda item: item["size_mb"] if item["size_mb"] is not None else float("inf")) if candidates else {
        "format_id": "bestaudio", "ext": "m4a", "size_mb": None,
    }


def target_height_from(req: dict) -> int:
    for key in ("resolution", "format"):
        v = req.get(key)
        if v:
            m = re.search(r"\d+", str(v))
            if m:
                return int(m.group())
    return 720


# ──────────────────────────── thumbnail util ───────────────────────────
def generate_thumbnail_b64(video_file: str) -> str:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return ""
    try:
        # Prefer the embedded stream (added by --embed-thumbnail)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=index:stream_disposition=attached_pic",
             "-of", "csv=p=0", video_file],
            capture_output=True, text=True,
        )
        pic_idx = next(
            (line.split(",")[0] for line in probe.stdout.splitlines()
             if line.endswith(",1")), None
        )
        out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir=TMP_DIR).name
        vf = "scale=256:256:force_original_aspect_ratio=increase,crop=256:256"
        if pic_idx:
            cmd = ["ffmpeg", "-y", "-i", video_file, "-map", f"0:{pic_idx}", "-vf", vf, "-frames:v", "1", out]
        else:
            cmd = ["ffmpeg", "-y", "-ss", "00:00:03", "-i", video_file, "-vframes", "1", "-vf", vf, out]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        with open(out, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        os.unlink(out)
        return b64
    except Exception:
        return ""


# ───────────────────────────── subprocess wrappers ─────────────────────
# Progress line emitted by our --progress-template:
#   dlprog|<format_id>|<vcodec>|<acodec>|<percent>%
DLPROG_RE = re.compile(r"dlprog\|([^|]*)\|([^|]*)\|([^|]*)\|\s*([\d.]+)\s*%")


def stream_kind(vcodec: str, acodec: str, index: int) -> str:
    """Classify the stream yt-dlp is currently downloading: video | audio."""
    v, a = vcodec.strip().lower(), acodec.strip().lower()
    if v == "none":
        return "audio"
    if a == "none":
        return "video"
    if v in ("", "na") and a in ("", "na"):     # codecs unknown: video first, audio second
        return "video" if index == 1 else "audio"
    return "video"                               # muxed stream


class YtDlp:
    @staticmethod
    async def list_formats(url: str, cookie_file: Optional[str]) -> str:
        cmd = [YTDLP, "--js-runtimes", "node", "-F", url, "--no-warnings"]
        if cookie_file:
            cmd[1:1] = ["--cookies", cookie_file]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="ignore").strip() or out.decode(errors="ignore").strip()
            raise RuntimeError(f"yt-dlp format probe failed rc={proc.returncode}: {detail[-1000:]}")
        return out.decode(errors="ignore")

    @staticmethod
    async def download(
        url: str, fmt: str, outdir: Path, cookie_file: Optional[str],
        on_progress: Callable[[int], Awaitable[None]],
        on_merge: Callable[[], Awaitable[None]],
        extra_args: Optional[list[str]] = None,
        on_stream: Optional[Callable[[str, str, int, int], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """
        Run yt-dlp and translate its output into callbacks.

        on_progress(bucket)                    -> every PROGRESS_STEP percent, per stream
        on_merge()                             -> [Merger] / [ExtractAudio] postprocessing
        on_stream(kind, fid, index, total)     -> a new stream (video/audio) started
        """
        outdir.mkdir(parents=True, exist_ok=True)

        # NOTE: --print implies --quiet, which silences progress and [Merger]
        # lines, so --no-quiet is required. The "download:" prefix in
        # --progress-template is only the template *type* selector and is not
        # part of the output, hence our own "dlprog|" marker.
        cmd = [YTDLP, "--newline", "--no-color", "--no-quiet", "--progress"]
        if cookie_file:
            cmd += ["--cookies", cookie_file]
        cmd += [
            "--js-runtimes", "node",
            "-f", fmt,
            "--embed-thumbnail",
            "--merge-output-format", "mp4",
            "-c",
            "--progress-template",
            "download:dlprog|%(info.format_id)s|%(info.vcodec)s|%(info.acodec)s|%(progress._percent_str)s",
            "-o", f"{outdir}/%(title)s.%(ext)s",
            "--print", "after_move:filepath",
        ]
        if extra_args:
            cmd += extra_args
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        total = len(fmt.split("+"))
        cur_fid: Optional[str] = None
        stream_idx = 0
        last_bucket = -1
        last_pct = -1.0
        final_path: Optional[str] = None

        async for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode(errors="ignore")).strip()

            if "[Merger]" in line or "[ExtractAudio]" in line:
                await on_merge()
                continue

            m = DLPROG_RE.search(line)
            if m:
                fid, vcodec, acodec, pct_s = m.groups()
                try:
                    pct = float(pct_s)
                except ValueError:
                    continue

                # New stream = format id changed (or pct dropped when ids are unknown)
                if fid != cur_fid or (fid in ("", "NA") and pct < last_pct):
                    cur_fid = fid
                    stream_idx += 1
                    last_bucket = -1
                    if on_stream:
                        await on_stream(stream_kind(vcodec, acodec, stream_idx),
                                        fid, stream_idx, total)
                elif pct < last_pct:
                    last_bucket = -1
                last_pct = pct

                bucket = int(pct // PROGRESS_STEP) * PROGRESS_STEP
                if bucket > last_bucket:
                    last_bucket = bucket
                    await on_progress(bucket)
            elif line.startswith("/"):
                final_path = line

        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp exited rc={proc.returncode}")
        return final_path


class Aria2:
    @staticmethod
    async def download(
        url: str, outdir: Path, filename: str, cookie_file: Optional[str],
        on_progress: Callable[[int], Awaitable[None]],
    ) -> Optional[str]:
        outdir.mkdir(parents=True, exist_ok=True)
        referer = re.match(r"^https?://[^/]+/?", url)
        referer = referer.group(0) if referer else ""
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        cmd = [
            ARIA2C, "-x4", "-s4", "-k1M",
            "--timeout=30", "--connect-timeout=10",
            f"--referer={referer}",
            f"--header=User-Agent: {ua}",
            "--header=Accept: */*",
            f"--dir={outdir}",
            "--retry-wait=5",
            "-o", filename,
            "--summary-interval=1",
            "--console-log-level=warn",
        ]
        if cookie_file:
            cmd.append(f"--load-cookies={cookie_file}")
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        last_bucket = -1
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").rstrip()
            m = re.search(r"\((\d+)%\)", line)
            if m:
                pct = int(m.group(1))
                bucket = (pct // PROGRESS_STEP) * PROGRESS_STEP
                if bucket > last_bucket:
                    last_bucket = bucket
                    await on_progress(bucket)
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"aria2c exited rc={proc.returncode}")
        return str(outdir / filename)


# ───────────────────────────── reporter ────────────────────────────────
class Reporter:
    def __init__(self, nc: NATS, subject: str, request_id: str):
        self.nc, self.subject, self.id = nc, subject, request_id

    async def send(self, status: str, **extra: Any) -> None:
        payload = {"id": self.id, "hostname": HOSTNAME, "status": status,
                   "ts": int(asyncio.get_event_loop().time()), **extra}
        await self.nc.publish(self.subject, json.dumps(payload).encode())
        LOG.info("-> %s %s %s", self.subject, status, {k: v for k, v in extra.items() if k != "thumbnail"})


# ──────────────────────────── shared handlers ──────────────────────────
@dataclass
class Ctx:
    cookies: CookieManager
    nc: NATS


async def _resolve_video_audio(url: str, ctx: Ctx, target_h: int) -> tuple[Optional[str], Optional[str]]:
    """List formats, pick best video+audio pair. Returns (None, None) on failure."""
    fmt_list = await YtDlp.list_formats(url, ctx.cookies.path)
    formats = parse_formats(fmt_list)
    return pick_video_format(formats, target_h), pick_audio_format(fmt_list)


async def _download_video_pipeline(
    req: dict, rep: Reporter, ctx: Ctx,
    dest_builder: Callable[[dict, int], Path],
    extra_ytdlp_args: Optional[list[str]] = None,
    use_aria2_external: bool = False,
    name_from_folder: bool = False,
) -> None:
    """
    Shared pipeline for songs / movies / serials / aria2c(vid):
      resolve -> download (video stream, audio stream, progress each)
              -> merge -> move -> thumbnail -> done

    name_from_folder: name the final file after its destination folder
    (<folder>/<folder>.<ext>) instead of the yt-dlp title. Used for movies
    so the layout matches Jellyfin/Plex conventions.
    """
    url = req.get("url") or (
        f"https://ok.ru/video/{req['vid']}" if req.get("vid") else None)
    if not url:
        await rep.send("failed", error="missing url or vid")
        return

    target_h = target_height_from(req)
    await rep.send("started", message="Resolving formats")

    vfmt, afmt = await _resolve_video_audio(url, ctx, target_h)
    if not vfmt:
        await rep.send("failed", error="no suitable video format found")
        return

    await rep.send("started", message="Download started", phase="download")

    phase = {"name": "download"}

    async def on_stream(kind: str, fid: str, idx: int, total: int):
        phase["name"] = kind
        await rep.send(
            "started",
            message=f"Downloading {kind} stream ({idx}/{total}, format {fid})",
            phase=kind, stream=idx, total_streams=total,
        )

    async def on_progress(pct: int):
        await rep.send("progress", progress=pct, phase=phase["name"])

    async def on_merge():
        await rep.send("merging", message="merging video + audio")

    tmp = Path(tempfile.mkdtemp(dir=TMP_DIR))
    try:
        extra = list(extra_ytdlp_args or [])
        if use_aria2_external:
            extra += ["--downloader", "aria2c",
                      "--downloader-args", "aria2c:-x 16 -s 16 -k 1M"]

        final = await YtDlp.download(
            url, f"{vfmt}+{afmt}", tmp, ctx.cookies.path,
            on_progress, on_merge, extra_args=extra, on_stream=on_stream,
        )
        if not final or not Path(final).exists():
            await rep.send("failed", error="no output produced")
            return

        dest = dest_builder(req, target_h)
        dest.mkdir(parents=True, exist_ok=True)
        await rep.send("moving", message=f"moving to {dest}")

        src = Path(final)
        # Movies: "<Movie Folder>/<Movie Folder>.<ext>" (Jellyfin/Plex style)
        dst = dest / (f"{dest.name}{src.suffix}" if name_from_folder else src.name)
        shutil.move(str(src), str(dst))

        size  = dst.stat().st_size
        thumb = generate_thumbnail_b64(str(dst))
        await rep.send("completed", file=str(dst), size=size, thumbnail=thumb or None)
    except Exception as e:  # noqa
        LOG.exception("pipeline error")
        await rep.send("failed", error=str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def handle_songs_dl(req, rep, ctx):
    await _download_video_pipeline(
        req, rep, ctx,
        dest_builder=lambda r, h: songs_dest(r.get("lang", ""), h,
                                              r.get("actress") or r.get("actress name") or "Unknown"),
    )


async def handle_movies_dl(req, rep, ctx):
    await _download_video_pipeline(
        req, rep, ctx,
        dest_builder=lambda r, h: movies_dest(r.get("lang", ""),
                                              r.get("movie name") or r.get("name") or "Untitled"),
        name_from_folder=True,
    )


async def handle_serials_dl(req, rep, ctx):
    await _download_video_pipeline(
        req, rep, ctx,
        dest_builder=lambda r, h: serials_dest(r.get("lang", ""),
                                               r.get("movie name") or r.get("name") or "Untitled"),
    )


async def handle_aria2c_dl(req, rep, ctx):
    """Direct URL → aria2c. If only vid is given, fall back to yt-dlp+aria2c external."""
    if req.get("vid") and not req.get("url"):
        await _download_video_pipeline(
            req, rep, ctx,
            dest_builder=lambda r, h: movies_dest(r.get("lang", ""),
                                                  r.get("movie name") or "Untitled"),
            use_aria2_external=True,
            name_from_folder=True,
        )
        return

    url = req.get("url")
    if not url:
        await rep.send("failed", error="missing url")
        return

    name = req.get("movie name") or req.get("name") or "download"
    ext  = (req.get("format") or "mp4").lstrip(".")
    dest = movies_dest(req.get("lang", ""), name)
    dest.mkdir(parents=True, exist_ok=True)
    # File name always mirrors the movie folder: "<Movie Folder>/<Movie Folder>.<ext>"
    filename = f"{dest.name}.{ext}"

    tmp = Path(tempfile.mkdtemp(dir=TMP_DIR))
    await rep.send("started", message="aria2c started")

    async def on_progress(pct): await rep.send("progress", progress=pct, phase="download")

    try:
        out = await Aria2.download(url, tmp, filename, ctx.cookies.path, on_progress)
        if not out or not Path(out).exists():
            await rep.send("failed", error="aria2c produced no file")
            return
        await rep.send("moving", message=f"moving to {dest}")
        dst = dest / Path(out).name
        shutil.move(out, str(dst))
        await rep.send("completed", file=str(dst), size=dst.stat().st_size)
    except Exception as e:
        LOG.exception("aria2c error")
        await rep.send("failed", error=str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def handle_mp3_dl(req, rep, ctx):
    url = req.get("url")
    if not url:
        await rep.send("failed", error="missing url")
        return

    await rep.send("started", message="mp3 download started")
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=TMP_DIR))

    async def on_progress(pct): await rep.send("progress", progress=pct, phase="download")
    async def on_merge():       await rep.send("merging",  message="extracting audio")

    try:
        final = await YtDlp.download(
            url, "bestaudio/best", tmp, ctx.cookies.path,
            on_progress, on_merge,
            extra_args=["--extract-audio", "--audio-format", "mp3",
                        "--audio-quality", "0", "--add-metadata", "--xattrs",
                        "--restrict-filenames"],
        )
        if not final or not Path(final).exists():
            await rep.send("failed", error="no mp3 produced")
            return
        await rep.send("moving", message=f"moving to {MUSIC_ROOT}")
        dst = MUSIC_ROOT / Path(final).name
        shutil.move(final, str(dst))
        await rep.send("completed", file=str(dst), size=dst.stat().st_size)
    except Exception as e:
        LOG.exception("mp3 error")
        await rep.send("failed", error=str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────── query handlers ──────────────────────────
async def _query_video(req, rep, ctx):
    url = req.get("url") or (
        f"https://ok.ru/video/{req['vid']}" if req.get("vid") else None)
    if not url:
        await rep.send("failed", error="missing url or vid")
        return

    try:
        fmt_list = await YtDlp.list_formats(url, ctx.cookies.path)
    except Exception as e:
        LOG.exception("format query failed for %s", url)
        await rep.send("failed", url=url, error=str(e))
        return
    formats  = parse_formats(fmt_list)
    audio    = parse_audio_format(fmt_list)
    smallest_by_height = {}
    for fid, height, size_mb, ext in formats:
        current = smallest_by_height.get(height)
        if current is None or _format_size_key(size_mb) < _format_size_key(current["size_mb"]):
            smallest_by_height[height] = {
                "format_id": fid, "height": height, "size_mb": size_mb, "ext": ext,
            }

    await rep.send(
        "completed",
        url=url,
        audio_format=audio["format_id"],
        audio=audio,
        formats=[
            {**item, "width": int(item["height"] * 16 / 9),
             "size_mb": round(item["size_mb"], 2) if item["size_mb"] is not None else None,
             "kind": "video"}
            for item in smallest_by_height.values()
        ],
    )


async def handle_query_songs(req, rep, ctx):  await _query_video(req, rep, ctx)
async def handle_query_movies(req, rep, ctx): await _query_video(req, rep, ctx)


# ─────────────────────────────── service ───────────────────────────────
class DownloadService:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.nc: Optional[NATS] = None
        self.cookies = CookieManager(COOKIE_FILE)
        self.ctx: Optional[Ctx] = None
        self._stop = asyncio.Event()

    async def start(self):
        connect_opts: dict[str, Any] = {"servers": [NATS_URL]}

        # Auth precedence:
        #   creds file  >  nkey seed  >  token  >  user+password  >  anonymous
        if NATS_CREDS_FILE:
            connect_opts["user_credentials"] = NATS_CREDS_FILE
            auth_kind = "creds"
        elif NATS_NKEY_SEED:
            connect_opts["nkeys_seed"] = NATS_NKEY_SEED
            auth_kind = "nkey"
        elif NATS_TOKEN:
            connect_opts["token"] = NATS_TOKEN
            auth_kind = "token"
        elif NATS_USER and NATS_PASSWORD:
            connect_opts["user"] = NATS_USER
            connect_opts["password"] = NATS_PASSWORD
            auth_kind = "user/pass"
        else:
            auth_kind = "none"

        self.nc = await nats.connect(**connect_opts)
        self.ctx = Ctx(cookies=self.cookies, nc=self.nc)
        LOG.info("connected to NATS %s (auth=%s) host=%s media_base=%s",
                 NATS_URL, auth_kind, HOSTNAME, MEDIA_BASE)

        self.cookies.start()

        for kind, subj in DOWNLOAD_TOPICS.items():
            await self.nc.subscribe(subj, queue=NATS_QUEUE_GROUP, cb=self._dl_cb(kind))
            LOG.info("subscribed download topic %s (%s)", subj, kind)
        for kind, subj in QUERY_TOPICS.items():
            await self.nc.subscribe(subj, queue=NATS_QUEUE_GROUP, cb=self._q_cb(kind))
            LOG.info("subscribed query topic %s (%s)", subj, kind)

        await self._worker()

    def _dl_cb(self, kind: str):
        async def cb(msg): await self.queue.put(("download", kind, msg))
        return cb

    def _q_cb(self, kind: str):
        async def cb(msg): await self.queue.put(("query", kind, msg))
        return cb

    async def _worker(self):
        while not self._stop.is_set():
            try:
                mode, kind, msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                req = json.loads(msg.data.decode() or "{}")
                rid = str(req.get("id", "unknown"))
                subject = (f"nats.download.response.{rid}" if mode == "download"
                           else f"nats.query.response.{rid}")
                rep = Reporter(self.nc, subject, rid)
                LOG.info("processing %s:%s id=%s", mode, kind, rid)

                if mode == "download":
                    handler = {
                        "songs":   handle_songs_dl,
                        "movies":  handle_movies_dl,
                        "serials": handle_serials_dl,
                        "aria2c":  handle_aria2c_dl,
                        "mp3":     handle_mp3_dl,
                    }[kind]
                else:
                    handler = {
                        "songs":  handle_query_songs,
                        "movies": handle_query_movies,
                    }[kind]

                await handler(req, rep, self.ctx)
            except Exception as e:
                LOG.exception("worker error")
            finally:
                self.queue.task_done()

    async def stop(self):
        self._stop.set()
        await self.cookies.stop()
        if self.nc:
            await self.nc.drain()


async def main():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    svc = DownloadService()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(svc.stop()))

    try:
        await svc.start()
    except asyncio.CancelledError:
        pass
    finally:
        await svc.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)