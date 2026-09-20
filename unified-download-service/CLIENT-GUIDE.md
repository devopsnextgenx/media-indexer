# NATS Download Service — Client Guide

## Connection
* NATS URL: `nats://<host>:4222` (default `nats://192.168.12.111:4222`)
* All requests/responses are JSON-encoded UTF-8.
* Every request **must include a unique `id`** string. It is echoed back and
  used to build the response subject.

## Authentication
The service is configured with NATS auth by default. The built-in credentials are:

```text
NATS_USER=zboxnats
NATS_PASSWORD=zboxpswd
```

These values are also used in the service deployer and are exported as env vars
when the service starts. The server accepts the following auth methods, in order
of precedence:

1. `NATS_CREDS` / user credentials file
2. `NATS_NKEY_SEED`
3. `NATS_TOKEN`
4. `NATS_USER` + `NATS_PASSWORD`
5. anonymous access

If you are connecting with the Python client, use the username/password pair:

```python
nc = await nats.connect(
    "nats://192.168.12.111:4222",
    user="zboxnats",
    password="zboxpswd",
)
```

If you already have a NATS creds file, use that instead:

```python
nc = await nats.connect(
    "nats://192.168.12.111:4222",
    user_credentials="/path/to/client.creds",
)
```

If you are using a token or NKey, pass the matching nats.py option instead.

## Subjects

### Requests (publish)
| Topic                              | Purpose                                   |
|------------------------------------|-------------------------------------------|
| `nats.download.request.songs`      | Download a song (video+audio → mp4)       |
| `nats.download.request.movies`     | Download a movie                          |
| `nats.download.request.serials`    | Download a serial episode                 |
| `nats.download.request.aria2c`     | Direct aria2c download (url) or yt-dlp+aria2c (vid) |
| `nats.download.request.mp3`        | Extract audio → mp3                       |
| `nats.query.request.songs`         | List available formats for a song URL     |
| `nats.query.request.movies`        | List available formats for a movie URL    |

### Responses (subscribe)
| Topic                                | When                                     |
|--------------------------------------|------------------------------------------|
| `nats.download.response.<id>`        | Progress/status for a download request   |
| `nats.query.response.<id>`           | Progress/status for a query request      |

Subscribe **before** publishing so you don't miss the first `started` event.

## Request schemas

### songs
```json
{
  "id": "song-1",
  "url": "https://www.youtube.com/watch?v=…",
  "format": "720",           // OR "resolution": "720"
  "resolution": "720",
  "lang": "hindi",           // free-form; mapped to DLang folder
  "actress name": "Jane Doe"
}
```

### movies
```json
{
  "id": "movie-42",
  "url": "https://ok.ru/video/123",   // OR "vid": "123"
  "vid": "123",
  "format": "1080",
  "resolution": "1080",
  "lang": "english",                   // english → hollywood; else bollywood
  "movie name": "Some Title"           // used as folder + file name
}
```
Send **either** `url` **or** `vid`, not both.

### aria2c
```json
{
  "id": "a2c-7",
  "url": "https://cdn.example.com/file.mp4",  // direct download
  "format": "mp4",
  "lang": "english",
  "movie name": "Some Title"
}
```
If you supply `vid` instead of `url` the service behaves like a movie
download but routes the transfer through aria2c as yt-dlp's external downloader.

### mp3
```json
{ "id": "mp3-1", "url": "https://www.youtube.com/watch?v=…" }
```

### query.songs / query.movies
```json
{ "id": "q-1", "url": "https://ok.ru/video/123" }
// or
{ "id": "q-2", "vid": "123" }
```

## Response schema (all statuses)

```json
{
  "id": "song-1",
  "hostname": "<service host name>",
  "status": "started",
  "ts": 12345.67,

  "progress": 30,                    // present when status=progress
  "message": "moving to /media/…",   // human readable
  "error": "403 Forbidden",          // present when status=failed
  "file": "/media/…/Some Title.mp4", // present when status=completed
  "size": 123456789,                 // bytes, status=completed
  "thumbnail": "data:image/jpeg;base64,…"   // status=completed (may be null)

  // query responses only:
  "audio_format": "251",
  "audio": {"format_id": "251", "ext": "m4a", "size_mb": 7.22},
  "formats": [
    {"format_id": "137", "height": 1080, "width": 1920,
     "ext": "mp4", "size_mb": 210.4, "kind": "video"}
  ]
}
```

Query responses contain at most one video format per height, selected by the
smallest known size. `size_mb` is measured in MiB and is `null` when yt-dlp
does not report a size. `audio_format` remains available as a compatibility
shortcut; use `audio` for the extension and size.

Every response includes `hostname`, identifying the host that published the
response. The field is present on all download and query statuses, including
failures.

### Status values
| Status      | Meaning                                                                 |
|-------------|-------------------------------------------------------------------------|
| `started`   | Worker accepted the job and began resolving formats.                    |
| `progress`  | Emitted at 10 % buckets during download. Carries `progress` (0–100).    |
| `merging`   | `yt-dlp` is merging video + audio (or extracting audio for mp3).        |
| `moving`    | Final file is being moved from temp to its destination folder.          |
| `completed` | Finished. Carries `file`, `size`, and (for video) `thumbnail`.          |
| `failed`    | Job failed. Carries `error`. Common reasons: stale cookies, geo-block, HTTP 403/404, unsupported site. |

## Handling "stale cookies"
The service refreshes cookies every 15 min. If a request fails with
`error` containing `cookies` or `403`, wait for the next refresh cycle
(≤ 15 min) and re-submit. Because cookies are shared, one refresh fixes all
subsequent requests.

On hosts without an accessible Chrome profile, deploy with
`NATS_COOKIE_FILE=/path/to/cookies.txt`. This uses an exported Netscape cookie
file and disables browser refresh for that service instance.

## Example (Python)

```python
import asyncio, json, uuid, nats

async def main():
    nc = await nats.connect("nats://192.168.12.111:4222")
    rid = str(uuid.uuid4())
    await nc.subscribe(f"nats.download.response.{rid}", cb=on_status)
    await nc.publish("nats.download.request.songs",
        json.dumps({
            "id": rid,
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "resolution": "720",
            "lang": "english",
            "actress name": "Rick"
        }).encode())
    await asyncio.sleep(600)

async def on_status(msg):
    print(json.loads(msg.data))

asyncio.run(main())
```

## Example (Go)

```go
nc, _ := nats.Connect("nats://192.168.12.111:4222")
id  := uuid.NewString()
nc.Subscribe("nats.download.response."+id, func(m *nats.Msg) {
    fmt.Println(string(m.Data))
})
payload, _ := json.Marshal(map[string]any{
    "id": id, "url": "https://ok.ru/video/123",
    "resolution": "1080", "lang": "english",
    "movie name": "Some Title",
})
nc.Publish("nats.download.request.movies", payload)
```
