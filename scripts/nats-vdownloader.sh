#!/bin/bash
# NATS-driven video downloader.
# Subscribes to 'vsongs.request' and downloads one video per message received.
#
# Run as a long-lived service (systemd user unit / tmux):
#   ~/bin/nats-vdownloader.sh [-d|--debug] [-n|--no-cookies]
#
# Message payload (either form):
#   JSON : {"url":"...","vformat":1080,"lang":"hindi","actress":"name"}
#   Text : <url>|<vformat>|<lang>|<actress>
#
# Connection can be overridden via env:
#   NATS_HOST NATS_PORT NATS_USER NATS_PASS NATS_SUBJECT NATS_QUEUE

# ─────────────────────────────────────────────
#  Debug / Verbose & Cookie Flag CLI Support
# ─────────────────────────────────────────────
DEBUG=false
COOKIES=true

for arg in "$@"; do
    case "$arg" in
        -d|--debug)
            DEBUG=true
            shift
            ;;
        -n|--no-cookies)
            COOKIES=false
            shift
            ;;
    esac
done

# ─────────────────────────────────────────────
#  Paths & globals
# ─────────────────────────────────────────────
LOCK_FILE=~/Videos/vdownloader.lock
LOG_FILE=~/Videos/video_download.log
SONGS_DIR=/media/data/Crucial-X6/ShareMe/media/songs/target
COOKIE_TARGET="/media/data/Crucial-X6/ShareMe/media/songs/target/cookies-zbox.txt"
TMP_DIR=~/tmp
QUEUE_FILE=~/Videos/vsongs.queue
QUEUE_LOCK=~/Videos/vsongs.queue.lock
TRACKER_FILE=$(mktemp -p "$TMP_DIR")

# NATS connection
NATS_HOST="${NATS_HOST:-zbox.local}"
NATS_PORT="${NATS_PORT:-4222}"
NATS_USER="${NATS_USER:-zboxnats}"
NATS_PASS="${NATS_PASS:-zboxpswd}"
NATS_SUBJECT="${NATS_SUBJECT:-vsongs.request}"
NATS_QUEUE="${NATS_QUEUE:-vdownloader}"
SUB_PID=""

# Defaults applied when a message omits a field
DEFAULT_VFORMAT="${DEFAULT_VFORMAT:-1080}"
DEFAULT_LANG="${DEFAULT_LANG:-hindi}"
DEFAULT_ACTRESS="${DEFAULT_ACTRESS:-unknown}"

# Thumbnail generation settings
THUMB_WIDTH=256
THUMB_HEIGHT=256
THUMB_SEEK="00:00:03"

API_URL="http://minis.local:2345/api/ytdlp/update-status"

YT_DLP=~/bin/yt-dlp

# Prepare common cookie arguments array
COOKIE_ARGS=()
if [ "$COOKIES" = true ]; then
    COOKIE_ARGS=(--cookies-from-browser chrome --cookies "$COOKIE_TARGET")
fi

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
log() {
    echo "$*" | tee -a "$LOG_FILE"
}

# Send debug messages to STDERR so subshell outputs remain clean
log_debug() {
    if [ "$DEBUG" = true ]; then
        echo "[DEBUG] $*" | tee -a "$LOG_FILE" >&2
    fi
}

# ─────────────────────────────────────────────
#  Update Status API Call
# ─────────────────────────────────────────────
# Args:
#   $1 entry_string   (required)
#   $2 status         (required)
#   $3 size_bytes     (optional - integer, size on disk of the moved file)
#   $4 thumbnail_b64  (optional - base64-encoded 256x256 jpeg)
update_status() {
    local entry_string="$1"
    local status="$2"
    local size_bytes="$3"
    local thumbnail_b64="$4"

    local payload
    payload=$(jq -n \
        --arg entry "$entry_string" \
        --arg status "$status" \
        --argjson size "${size_bytes:-null}" \
        --arg thumb "$thumbnail_b64" \
        '{entry: $entry, status: $status}
         + (if $size != null then {size: $size} else {} end)
         + (if $thumb != "" then {thumbnail: $thumb} else {} end)')

    local response
    response=$(curl -s -X 'POST' \
      "$API_URL" \
      -H 'accept: application/json' \
      -H 'Content-Type: application/json' \
      -d "$payload")

    local res_status res_entry
    res_status=$(echo "$response" | jq -r '.status // empty')
    res_entry=$(echo "$response" | jq -r '.entry // empty')

    if [[ "$res_status" == "success" ]]; then
        local log_message="[INFO] Updated status for entry: ${res_entry:-$entry_string} → $status"
        echo -e "\n$log_message" | tee -a "$LOG_FILE"
    else
        local log_message="[ERROR] Failed to update status for entry: $entry_string. Response: $response"
        echo -e "\n$log_message" | tee -a "$LOG_FILE"
    fi
}

# ─────────────────────────────────────────────
#  Lock management
# ─────────────────────────────────────────────
acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        log "Lock file exists, another instance is running. Exiting."
        exit 0
    fi
    touch "$LOCK_FILE"
}

release_lock() {
    rm -f "$LOCK_FILE"
}

# ─────────────────────────────────────────────
#  Map LANG → DLANG
# ─────────────────────────────────────────────
resolve_dlang() {
    local lang
    lang=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    case "$lang" in
        hindi)                                         echo "Hindi"   ;;
        marathi)                                       echo "Marathi" ;;
        south|telugu|tamil|kannada|malyalam|malayalam) echo "South"   ;;
        bhojpuri)                                      echo "Bhojpuri" ;;
        english)                                       echo "English" ;;
        *)                                             echo "Hindi"   ;;
    esac
}

# ─────────────────────────────────────────────
#  Map VFORMAT number → RESOLUTION label
# ─────────────────────────────────────────────
resolve_resolution() {
    local vformat="$1"
    if   [ "$vformat" -lt  720 ]; then echo "sd"
    elif [ "$vformat" -le 1080 ]; then echo "hd"
    else                               echo "xhd"
    fi
}

# ─────────────────────────────────────────────
#  Query available formats and return the best
# ─────────────────────────────────────────────
select_video_format() {
    local url="$1"
    local vformat="$2"

    log_debug "Fetching format list for URL: $url"

    local fmt_list
    if [ "$DEBUG" = true ]; then
        fmt_list=$("$YT_DLP" "${COOKIE_ARGS[@]}" --js-runtimes node -F "$url")
        log_debug "--- Raw Format List Output ---"
        log_debug "$fmt_list"
        log_debug "------------------------------"
    else
        fmt_list=$("$YT_DLP" "${COOKIE_ARGS[@]}" --js-runtimes node -F "$url" 2>/dev/null)
    fi

    if [ -z "$fmt_list" ]; then
        log "  [WARN] Could not retrieve format list for: $url"
        echo ""
        return 1
    fi

    local candidates=""
    while IFS= read -r line; do
        echo "$line" | grep -qiE '(audio only|storyboard|images)' && continue
        [[ "$line" =~ ^[0-9] ]] || continue

        local id height size_mb
        id=$(echo "$line" | awk '{print $1}')

        height=$(echo "$line" | grep -oP '\b([0-9]+)(?=p\b)' | head -1)
        [ -z "$height" ] && height=$(echo "$line" | grep -oP '(?<=x)[0-9]+' | head -1)
        [ -z "$height" ] && continue

        size_mb=$(echo "$line" | grep -oP '[~]?\s*[0-9]+(\.[0-9]+)?\s*MiB' | grep -oP '[0-9]+(\.[0-9]+)?' | head -1)
        if [ -z "$size_mb" ]; then
            local size_kib
            size_kib=$(echo "$line" | grep -oP '[~]?\s*[0-9]+(\.[0-9]+)?\s*KiB' | grep -oP '[0-9]+(\.[0-9]+)?' | head -1)
            [ -n "$size_kib" ] && size_mb=$(echo "scale=3; $size_kib/1024" | bc)
        fi
        [ -z "$size_mb" ] && size_mb=999999

        candidates="${candidates}${id}|${height}|${size_mb}\n"
    done <<< "$fmt_list"

    if [ -z "$candidates" ]; then
        log "  [WARN] No parseable video formats found for: $url"
        echo ""
        return 1
    fi

    log_debug "Parsed candidates (format_id|height|size_mb):"
    log_debug "$(printf "%b" "$candidates")"

    local exact
    exact=$(printf "%b" "$candidates" | awk -F'|' -v h="$vformat" '$2==h {print}')

    local pool
    if [ -n "$exact" ]; then
        log_debug "Found exact match for height: ${vformat}p"
        pool="$exact"
    else
        log_debug "No exact match for ${vformat}p. Finding closest match..."
        local best_diff=999999 best_height=""
        while IFS='|' read -r id height size; do
            local diff=$(( height > vformat ? height - vformat : vformat - height ))
            if [ "$diff" -lt "$best_diff" ]; then
                best_diff=$diff
                best_height=$height
            fi
        done < <(printf "%b" "$candidates" | awk -F'|' '{print}')
        log_debug "Closest height identified: ${best_height}p"
        pool=$(printf "%b" "$candidates" | awk -F'|' -v h="$best_height" '$2==h {print}')
    fi

    local best_id
    best_id=$(printf "%b" "$pool" | awk -F'|' 'BEGIN{min=999999;id=""} {if($3<min){min=$3;id=$1}} END{print id}')

    log_debug "Selected video format ID: $best_id"
    echo "$best_id"
}

# ─────────────────────────────────────────────
#  Select best audio-only format
# ─────────────────────────────────────────────
select_audio_format() {
    local url="$1"
    local fmt_list

    if [ "$DEBUG" = true ]; then
        fmt_list=$("$YT_DLP" "${COOKIE_ARGS[@]}" --js-runtimes node -F "$url")
    else
        fmt_list=$("$YT_DLP" "${COOKIE_ARGS[@]}" --js-runtimes node -F "$url" 2>/dev/null)
    fi

    local best_id
    best_id=$(echo "$fmt_list" | awk '
        /audio only/ && /opus|m4a/ {
            id=$1
            if (!found) { found=1; best=id }
        }
        END { print (best ? best : "bestaudio") }
    ')

    log_debug "Selected audio format ID: $best_id"
    echo "$best_id"
}

# ─────────────────────────────────────────────
#  Find the stream index of the thumbnail that
#  yt-dlp embedded via --embed-thumbnail (stored
#  as a video stream with disposition=attached_pic)
# ─────────────────────────────────────────────
find_attached_pic_stream() {
    local video_file="$1"

    ffprobe -v error -select_streams v \
        -show_entries stream=index:stream_disposition=attached_pic \
        -of csv=p=0 "$video_file" 2>/dev/null \
        | awk -F',' '$2==1 {print $1; exit}'
}

# ─────────────────────────────────────────────
#  Extract the thumbnail yt-dlp already embedded
#  in the mp4 (via --embed-thumbnail), resize it
#  to 256x256, and base64-encode it. Cleans up
#  its own temp file before returning. Falls back
#  to grabbing a video frame only if no embedded
#  thumbnail stream is found.
# ─────────────────────────────────────────────
generate_thumbnail_b64() {
    local video_file="$1"

    if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
        log_debug "ffmpeg/ffprobe not found; skipping thumbnail extraction."
        echo ""
        return 1
    fi

    local thumb_file
    thumb_file=$(mktemp -p "$TMP_DIR" --suffix=.jpg)

    local pic_stream
    pic_stream=$(find_attached_pic_stream "$video_file")

    if [ -n "$pic_stream" ]; then
        log_debug "Found embedded thumbnail at stream index $pic_stream in: $video_file"
        ffmpeg -y -i "$video_file" -map "0:${pic_stream}" \
            -vf "scale=${THUMB_WIDTH}:${THUMB_HEIGHT}:force_original_aspect_ratio=increase,crop=${THUMB_WIDTH}:${THUMB_HEIGHT}" \
            -frames:v 1 "$thumb_file" >/dev/null 2>&1
    else
        log_debug "No embedded thumbnail found in $video_file; falling back to frame grab."
        ffmpeg -y -ss "$THUMB_SEEK" -i "$video_file" -vframes 1 \
            -vf "scale=${THUMB_WIDTH}:${THUMB_HEIGHT}:force_original_aspect_ratio=increase,crop=${THUMB_WIDTH}:${THUMB_HEIGHT}" \
            "$thumb_file" >/dev/null 2>&1
    fi

    if [ ! -s "$thumb_file" ]; then
        log "  [WARN] Failed to obtain thumbnail for: $video_file"
        rm -f "$thumb_file"
        echo ""
        return 1
    fi

    local b64
    b64=$(base64 -w 0 "$thumb_file")

    # Delete the local thumbnail file/data now that it's encoded for upload
    rm -f "$thumb_file"

    echo "$b64"
}

# ─────────────────────────────────────────────
#  Download one entry
# ─────────────────────────────────────────────
download_entry() {
    local url="$1" vformat="$2" lang="$3" actress="$4"
    local entry_string="${url}|${vformat}|${lang}|${actress}"

    log ""
    log "  URL:     $url"
    log "  VFORMAT: $vformat  |  LANG: $lang  |  ACTRESS: $actress"

    update_status "$entry_string" "Downloading"

    local dlang resolution move_location
    dlang=$(resolve_dlang "$lang")
    resolution=$(resolve_resolution "$vformat")
    move_location="$SONGS_DIR/$dlang/$resolution/$actress"

    log "  DLANG=$dlang  RESOLUTEION=$resolution"
    log "  MOVE → $move_location"

    local vfmt_id afmt_id
    vfmt_id=$(select_video_format "$url" "$vformat")
    afmt_id=$(select_audio_format  "$url")

    if [ -z "$vfmt_id" ]; then
        log "  [ERROR] Could not determine video format id. Skipping."
        update_status "$entry_string" "Failed"
        return 1
    fi

    log "  Selected: video=$vfmt_id  audio=$afmt_id"

    > "$TRACKER_FILE"

    local extra_ytdlp_args=()
    if [ "$DEBUG" = true ]; then
        extra_ytdlp_args+=("-v")
    fi

    "$YT_DLP" "${COOKIE_ARGS[@]}" "${extra_ytdlp_args[@]}" \
        --js-runtimes node \
        -f "${vfmt_id}+${afmt_id}" \
        --embed-thumbnail \
        --progress-delta 0.5 \
        --progress-template 'download: ━► %(progress._percent_str)s of %(progress._total_bytes_str,progress._total_bytes_estimate_str)s | Speed: %(progress._speed_str)s | ETA: %(progress._eta_str)s' \
        --merge-output-format mp4 -c \
        -o "${TMP_DIR}/%(title)s.%(ext)s" \
        --exec "echo {} > '${TRACKER_FILE}'" \
        "$url" >> "$LOG_FILE" 2>&1 </dev/null

    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        log "  [ERROR] yt-dlp exited with code $exit_code"
        update_status "$entry_string" "Failed"
        return 1
    fi

    local downloaded_file
    downloaded_file=$(cat "$TRACKER_FILE" 2>/dev/null)
    if [ -z "$downloaded_file" ] || [ ! -f "$downloaded_file" ]; then
        log "  [ERROR] Tracker file empty or downloaded file not found."
        update_status "$entry_string" "Failed"
        return 1
    fi

    mkdir -p "$move_location"
    mv "$downloaded_file" "$move_location/"
    local moved_name moved_path
    moved_name=$(basename "$downloaded_file")
    moved_path="$move_location/$moved_name"

    log "  [OK] Moved: $moved_name → $move_location/"
    notify-send "Download Completed" "Song '$moved_name' downloaded and moved to $move_location." 2>/dev/null || true

    # Size on disk (actual blocks allocated, in bytes) for the moved file
    local file_size
    file_size=$(du -B1 --apparent-size=off "$moved_path" 2>/dev/null | awk '{print $1}')
    [ -z "$file_size" ] && file_size=$(stat -c%s "$moved_path" 2>/dev/null)
    log_debug "Size on disk for $moved_name: ${file_size:-unknown} bytes"

    # 256x256 base64 thumbnail, generated then deleted locally once encoded
    local thumb_b64
    thumb_b64=$(generate_thumbnail_b64 "$moved_path")
    log_debug "Thumbnail generated: $([ -n "$thumb_b64" ] && echo yes || echo no)"

    update_status "$entry_string" "Completed" "$file_size" "$thumb_b64"

    # Explicitly drop the in-memory thumbnail data now that it's been sent
    unset thumb_b64

    return 0
}

# ─────────────────────────────────────────────
#  NATS plumbing
# ─────────────────────────────────────────────
nats_args() {
    local args=(--server "nats://${NATS_HOST}:${NATS_PORT}")
    if [ -n "$NATS_USER" ]; then
        args+=(--user "$NATS_USER" --password "$NATS_PASS")
    fi
    printf '%s\n' "${args[@]}"
}

require_nats_cli() {
    if ! command -v nats >/dev/null 2>&1; then
        log "[ERROR] 'nats' CLI not found. Install from https://github.com/nats-io/natscli/releases"
        exit 1
    fi
}

# Subscriber runs in the background so downloads never block message intake.
# Each received payload is flattened to a single line and appended to the queue
# file under an flock, which the main loop drains one entry at a time.
start_subscriber() {
    local args=()
    mapfile -t args < <(nats_args)

    log "Subscribing to '$NATS_SUBJECT' (queue group: $NATS_QUEUE) on nats://${NATS_HOST}:${NATS_PORT}"

    nats "${args[@]}" sub "$NATS_SUBJECT" --queue "$NATS_QUEUE" --raw 2>>"$LOG_FILE" \
        | while IFS= read -r payload; do
              [ -z "$payload" ] && continue
              log_debug "Received: $payload"
              push_queued_message "$payload"
          done &

    SUB_PID=$!
}

stop_subscriber() {
    [ -z "$SUB_PID" ] && return 0
    pkill -P "$SUB_PID" 2>/dev/null
    kill "$SUB_PID" 2>/dev/null
    return 0
}

push_queued_message() {
    {
        flock 9
        printf '%s\n' "$1" >> "$QUEUE_FILE"
    } 9>"$QUEUE_LOCK"
}

# Atomically pop the oldest queued payload; prints nothing when the queue is empty.
pop_queued_message() {
    {
        flock 9
        if [ -s "$QUEUE_FILE" ]; then
            head -n 1 "$QUEUE_FILE"
            tail -n +2 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
        fi
    } 9>"$QUEUE_LOCK"
}

# Normalise a payload (JSON object or 'url|vformat|lang|actress') into
# a single pipe-delimited line with defaults filled in.
parse_message() {
    local payload="$1"
    local url vformat lang actress

    if echo "$payload" | jq -e 'type == "object"' >/dev/null 2>&1; then
        url=$(echo     "$payload" | jq -r '.url // .URL // empty')
        vformat=$(echo "$payload" | jq -r '.vformat // .VFORMAT // empty')
        lang=$(echo    "$payload" | jq -r '.lang // .LANG // empty')
        actress=$(echo "$payload" | jq -r '.actress // .ACTRESS // empty')
    else
        IFS='|' read -r url vformat lang actress <<< "$payload"
    fi

    url=$(echo     "$url"     | xargs)
    vformat=$(echo "$vformat" | xargs)
    lang=$(echo    "$lang"    | xargs)
    actress=$(echo "$actress" | xargs)

    [ -z "$vformat" ] && vformat="$DEFAULT_VFORMAT"
    [ -z "$lang"    ] && lang="$DEFAULT_LANG"
    [ -z "$actress" ] && actress="$DEFAULT_ACTRESS"

    if [ -z "$url" ]; then
        log "  [SKIP] Message has no URL: $payload"
        return 1
    fi
    if ! [[ "$vformat" =~ ^[0-9]+$ ]]; then
        log "  [SKIP] Invalid VFORMAT '$vformat' for: $url"
        return 1
    fi

    printf '%s|%s|%s|%s\n' "$url" "$vformat" "$lang" "$actress"
}

# Drain the queue, downloading one entry at a time.
process_queue() {
    local payload entry url vformat lang actress

    while :; do
        payload=$(pop_queued_message)
        [ -z "$payload" ] && break

        entry=$(parse_message "$payload") || continue
        IFS='|' read -r url vformat lang actress <<< "$entry"

        log "--- Processing message ---"
        download_entry "$url" "$vformat" "$lang" "$actress"
    done
}

USER_ID=$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_ID/bus"
export DISPLAY=:0

# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
main() {
    require_nats_cli
    acquire_lock
    trap 'stop_subscriber; release_lock; rm -f "$TRACKER_FILE"' EXIT

    mkdir -p "$TMP_DIR" "$(dirname "$QUEUE_FILE")"
    touch "$QUEUE_FILE" "$QUEUE_LOCK"

    log "================================================================================================"
    log "NATS video downloader started at $(date +"%Y-%m-%d %T")"
    log_debug "Debug mode enabled."
    log_debug "Cookies enabled: $COOKIES"

    # Cookie warm-up query
    "$YT_DLP" "${COOKIE_ARGS[@]}" --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >/dev/null 2>&1

    start_subscriber

    while :; do
        if ! kill -0 "$SUB_PID" 2>/dev/null; then
            log "[ERROR] NATS subscriber exited. Restarting in 5s..."
            sleep 5
            start_subscriber
        fi

        process_queue
        sleep 2
    done
}

main "$@"
