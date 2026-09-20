# NATS Download Service — Architecture

## Goal
Replace four ad-hoc shell scripts (`downloadAria.sh`, `downloadMovies.sh`,
`downloadMp3.sh`, `downloadVideos.sh`) with **one long-running service** that:

* subscribes to NATS download and query topics,
* processes requests **strictly one at a time** (FIFO queue — no locks needed),
* streams progress back to `nats.download.response.<id>` / `nats.query.response.<id>`,
* keeps a **single refreshed cookie jar** (every 15 min) shared by all requests,
* is deployed and controlled through a single `deploy-nats-download.sh` script.

## Components

```
 ┌──────────── NATS broker ─────────────┐
 │  nats.download.request.*             │
 │  nats.query.request.*                │
 └─────────────┬────────────────────────┘
               │  (all topics funnelled into one queue)
               ▼
      ┌─────────────────────┐
      │  DownloadService    │  single asyncio worker
      │  asyncio.Queue      │  → guarantees serial execution
      └──────┬──────────────┘
             │
   ┌─────────┼─────────────┬─────────────────┐
   ▼         ▼             ▼                 ▼
CookieManager  YtDlp      Aria2          Reporter
(refresh/15m)  (fmt/pick/ (direct URL)  (pub JSON status)
                download)
```

### 3.1 Request intake
`nats_download_service.py` subscribes to seven subjects
(`nats.download.request.{songs,movies,aria2c,mp3,serials}` and
`nats.query.request.{songs,movies}`). Every message is validated as JSON and
pushed, unchanged, onto a single `asyncio.Queue`.

Because the queue is single-consumer, **requests run sequentially**, in the
order they were received. No locking or inter-process coordination is needed.

### 3.2 Cookie lifecycle (`CookieManager`)
* One background task refreshes cookies every 15 min from the Chrome profile
  into `~/tmp/cookies.txt`.
* Every downloader call receives this path (via `--cookies` for `yt-dlp` and
  `--load-cookies` for `aria2c`). Requests never refresh cookies themselves.

### 3.3 Format resolution
* `parse_formats()` normalises `yt-dlp -F` output into `(id, height, size_mb, ext)`.
* `pick_video_format()` selects the format closest to the caller's
  `resolution` (or numeric part of `format`), preferring the smallest size at
  that height.
* `pick_audio_format()` returns the smallest opus/m4a audio-only format id;
  query responses also include its `ext` and `size_mb` in an `audio` object.
* The same helpers back **both** download handlers and query handlers.

### 3.4 Download pipeline (songs / movies / serials / aria2c-vid)
`_download_video_pipeline()` is the single shared routine:

```
started ──► progress (10% buckets) ──► merging ──► moving ──► completed
                                              └─► failed (any step)
```

* `progress` is emitted only when the integer percent crosses a 10 % bucket,
  so a 4 GB download emits ~10 progress messages instead of thousands.
* `merging` is emitted when `yt-dlp` logs `[Merger] Merging formats…`.
* `moving` is emitted before the file is moved from `~/tmp/…` to final storage.
* `completed` carries `file`, `size` (bytes) and a 256×256 base64 `thumbnail`
  extracted from the video file (embedded thumb → ffprobe lookup → fallback
  frame grab at `00:00:03`).
* `failed` carries `error` (e.g. stale cookies, geo-block, 403) as reported by
  `yt-dlp` / `aria2c`.

Songs and movies share the pipeline and differ only in the `dest_builder`
lambda that resolves the final folder:

| Kind     | Destination                                              |
|----------|----------------------------------------------------------|
| songs    | `SONGS_ROOT/<DLang>/<sd\|hd\|xhd>/<actress>/`            |
| movies   | `MOVIES_ROOT/<hollywood\|bollywood>/<movie name>/`       |
| serials  | `SERIALS_ROOT/<DLang>/<name>/`                           |
| aria2c   | `MOVIES_ROOT/<hollywood\|bollywood>/<movie name>/`       |

`DLang` mapping and resolution mapping reuse the same tables as the old
`downloadVideos.sh`.

### 3.5 Direct aria2c path
If the `aria2c` topic receives a direct `url` (not `vid`) the service skips
`yt-dlp` entirely and runs `aria2c` with the same referer/UA/retry settings
the old `downloadAria.sh` used, still honouring the shared cookie jar.

### 3.6 Query handlers
`nats.query.request.{songs,movies}` runs `yt-dlp -F` only, then returns:

```json
{
  "id": "…",
  "status": "completed",
  "audio_format": "251",
  "audio": {"format_id": "251", "ext": "m4a", "size_mb": 7.22},
  "formats": [
    {"format_id": "137", "height": 1080, "width": 1920,
     "ext": "mp4", "size_mb": 210.4, "kind": "video"}
  ]
}
```

Query results contain one smallest-known video candidate per height. Unknown
sizes are returned as `null`; `size_mb` values are in MiB.

No download happens, no destination is written.

### 3.7 Status payload
All status messages share the same envelope, so clients can use one parser:

```json
{
  "id": "<request id>",
  "hostname": "<service host name>",
  "status": "started|progress|merging|moving|completed|failed",
  "progress": 30,          // only for status=progress
  "message": "moving to /media/…",
  "error": "…",            // only for status=failed
  "file": "/media/…/x.mp4",
  "size": 123456789,
  "thumbnail": "<base64 jpeg>",
  "ts": 12345.6
}
```

Every response includes `hostname`, the host name of the service instance that
published it. This is present for download and query responses, including
success and failure statuses.

### 3.8 Deployment (`deploy-nats-download.sh`)
| Flag         | Effect                                                |
|--------------|-------------------------------------------------------|
| `install`    | Create venv, `pip install nats-py yt-dlp`, symlink `yt-dlp` into `~/bin`, copy `nats_download_service.py` → `~/bin/`, write `~/.config/systemd/user/nats-download-service.service`, `systemctl --user enable --now`. |
| `redeploy`   | Same as install (idempotent).                         |
| `reload`     | Re-copy service file, `daemon-reload`, restart.       |
| `restart`    | Restart running service.                              |
| `stop`       | Stop without removing unit.                           |
| `uninstall`  | Stop, disable, remove unit and `~/bin` copy.          |
| `status`     | systemctl status.                                     |
| `help`/`-h`  | Usage.                                                |

Runtime configuration is via `NATS_URL` env var, baked into the unit at
install time.

### 3.9 Why no locks
The old scripts used `*.lock` files because each cron invocation was a new
process that could race with the previous one. Here a single long-lived
process owns the queue, so ordering is intrinsic. Locking is only needed
across processes, and there is only one.

### 3.10 Reuse map (old → new)
| Old location                                        | New location                                    |
|-----------------------------------------------------|-------------------------------------------------|
| `downloadVideos.sh::resolve_dlang`                  | `resolve_dlang()`                               |
| `downloadVideos.sh::resolve_resolution`             | `resolve_resolution()`                          |
| `downloadVideos.sh::select_video_format`            | `parse_formats()` + `pick_video_format()`       |
| `downloadVideos.sh::select_audio_format`            | `pick_audio_format()`                           |
| `downloadVideos.sh::generate_thumbnail_b64`         | `generate_thumbnail_b64()`                      |
| `downloadVideos.sh` progress args                   | `YtDlp.download(..., on_progress)`              |
| `downloadVideos.sh::update_status` (HTTP)           | `Reporter.send` (NATS)                          |
| `downloadAria.sh` aria2c arg set                    | `Aria2.download()`                              |
| `downloadMovies.sh` `--aria2c` external downloader  | `_download_video_pipeline(use_aria2_external)`  |
| `downloadMp3.sh` yt-dlp audio flags                 | `handle_mp3_dl`                                 |
| Chrome cookie refresh + test ping                   | `CookieManager.refresh()`                       |
