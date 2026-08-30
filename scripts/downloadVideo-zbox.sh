#!/bin/bash
# */15  * * * * ~/bin/downloadVideo.sh > /dev/null 2>&1

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
LOCK_FILE=~/Videos/video.lock
LOG_FILE=~/Videos/video_download.log
DOWNLOAD_FILE=~/Videos/songs.txt
DOWNLOAD_FILE_TMP=~/Videos/songs.txt.tmp
BACK_FILE=~/Videos/back.songs.txt
SONGS_DIR=/media/data/Crucial-X6/ShareMe/media/songs/target
EXTERNAL_DOWNLOAD_FILE=/media/data/Crucial-X6/ShareMe/media/songs/target/download.txt
COOKIE_TARGET="/media/data/Crucial-X6/ShareMe/media/songs/target/cookies-zbox.txt"
TMP_DIR=~/tmp
TRACKER_FILE=$(mktemp -p "$TMP_DIR")

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

# Cookie test query
"$YT_DLP" "${COOKIE_ARGS[@]}" --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >/dev/null 2>&1

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
#  Deduplication
# ─────────────────────────────────────────────
deduplicate_download_file() {
    [ ! -f "$DOWNLOAD_FILE" ] && return 0

    local dedup_tmp
    dedup_tmp=$(mktemp -p "$TMP_DIR")

    awk '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ {
            print; next
        }
        {
            split($0, fields, "|")
            key = ""
            for (i=1; i<=length(fields); i++) {
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", fields[i])
                key = key (i > 1 ? "|" : "") fields[i]
            }
            if (!seen[key]++) {
                print
            }
        }
    ' "$DOWNLOAD_FILE" > "$dedup_tmp" && mv "$dedup_tmp" "$DOWNLOAD_FILE"
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
        "$url" >> "$LOG_FILE" 2>&1

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
#  Parse & process the download file
# ─────────────────────────────────────────────
process_download_file() {
    local prev_vformat="" prev_lang="" prev_actress=""

    while IFS= read -r raw_line || [ -n "$raw_line" ]; do
        local line
        line=$(echo "$raw_line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [[ -z "$line" || "$line" == \#* ]] && continue

        local url vformat lang actress
        IFS='|' read -r url vformat lang actress <<< "$line"

        url=$(echo     "$url"     | xargs)
        vformat=$(echo "$vformat" | xargs)
        lang=$(echo    "$lang"    | xargs)
        actress=$(echo "$actress" | xargs)

        [ -z "$vformat" ] && vformat="$prev_vformat"
        [ -z "$lang"    ] && lang="$prev_lang"
        [ -z "$actress" ] && actress="$prev_actress"

        if [ -z "$url" ]; then
            log "  [SKIP] Empty URL on line: $raw_line"
            continue
        fi
        if [ -z "$vformat" ] || ! [[ "$vformat" =~ ^[0-9]+$ ]]; then
            log "  [SKIP] Invalid or missing VFORMAT '$vformat' for: $url"
            continue
        fi

        prev_vformat="$vformat"
        prev_lang="$lang"
        prev_actress="$actress"

        log "--- Processing entry ---"
        download_entry "$url" "$vformat" "$lang" "$actress"

    done < "$DOWNLOAD_FILE"
}

# ─────────────────────────────────────────────
#  Backup and rotate the download file
# ─────────────────────────────────────────────
backup_and_rotate() {
    {
        echo "-------------------------------------"
        echo "$(date +"%Y-%m-%d %T")"
        echo "-------------------------------------"
        cat "$DOWNLOAD_FILE"
        echo ""
        echo "-------------------------------------"
        echo "$(date +"%Y-%m-%d %T")"
        echo "-------------------------------------"
    } >> "$BACK_FILE"

    rm -f "$DOWNLOAD_FILE"

    if [ -f "$DOWNLOAD_FILE_TMP" ]; then
        mv "$DOWNLOAD_FILE_TMP" "$DOWNLOAD_FILE"
        log "Rotated $DOWNLOAD_FILE_TMP → $DOWNLOAD_FILE"
    else
        log "No $DOWNLOAD_FILE_TMP found to rotate into $DOWNLOAD_FILE"
    fi
}

USER_ID=$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_ID/bus"
export DISPLAY=:0

# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
main() {
    acquire_lock
    trap 'release_lock; rm -f "$TRACKER_FILE"' EXIT

    mkdir -p "$TMP_DIR"

    log "================================================================================================"
    log "File processing started at $(date +"%Y-%m-%d %T")"
    log_debug "Debug mode enabled."
    log_debug "Cookies enabled: $COOKIES"

    if [ -s "$EXTERNAL_DOWNLOAD_FILE" ]; then
        log "New entries found in $EXTERNAL_DOWNLOAD_FILE. Syncing to $DOWNLOAD_FILE..."
        mkdir -p "$(dirname "$DOWNLOAD_FILE")"
        cat "$EXTERNAL_DOWNLOAD_FILE" >> "$DOWNLOAD_FILE"
        : > "$EXTERNAL_DOWNLOAD_FILE"
    fi

    deduplicate_download_file

    if [ ! -s "$DOWNLOAD_FILE" ]; then
        log "No file or entries in $DOWNLOAD_FILE for Processing."
        log "File processing completed at $(date +"%Y-%m-%d %T")"
        log "================================================================================================"
        exit 0
    fi

    notify-send "Video Download Started" "Processing video downloads..." 2>/dev/null || true

    process_download_file

    backup_and_rotate

    log "File processing completed at $(date +"%Y-%m-%d %T")"
    log "================================================================================================"
}

main "$@"