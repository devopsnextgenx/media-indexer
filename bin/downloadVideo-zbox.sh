#!/bin/bash
# */15  * * * * ~/bin/downloadVideo.sh > /dev/null 2>&1

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

API_URL="http://minis.local:2345/api/ytdlp/update-status"

YT_DLP=~/bin/yt-dlp

"$YT_DLP" --cookies-from-browser chrome --cookies "$COOKIE_TARGET" --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >/dev/null 2>&1

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
log() {
    echo "$*" | tee -a "$LOG_FILE"
}

# ─────────────────────────────────────────────
#  Update Status API Call
# ─────────────────────────────────────────────
update_status() {
    local entry_string="$1"
    local status="$2"

    # Send POST request and capture response
    local response
    response=$(curl -s -X 'POST' \
      "$API_URL" \
      -H 'accept: application/json' \
      -H 'Content-Type: application/json' \
      -d "{\"entry\": \"${entry_string}\", \"status\": \"${status}\"}")

    # Parse JSON fields using jq
    local res_status res_entry
    res_status=$(echo "$response" | jq -r '.status // empty')
    res_entry=$(echo "$response" | jq -r '.entry // empty')

    # Log based on API response
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

    # Removes duplicate data lines while retaining comments and line order
    awk '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ {
            print; next
        }
        {
            # Normalize whitespace around fields for reliable duplicate checks
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
#  Map LANG → DLANG  (case-insensitive)
# ─────────────────────────────────────────────
resolve_dlang() {
    local lang
    lang=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    case "$lang" in
        hindi)                          echo "Hindi"   ;;
        marathi)                        echo "Marathi" ;;
        south|telugu|tamil|kannada|malyalam|malayalam) echo "South"   ;;
        bhojpuri)                       echo "Bhojpuri" ;;
        english)                        echo "English" ;;
        *)                              echo "Hindi"   ;;   # sensible default
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
#  format-id for the requested resolution.
# ─────────────────────────────────────────────
select_video_format() {
    local url="$1"
    local vformat="$2"

    local fmt_list
    fmt_list=$("$YT_DLP" --cookies-from-browser chrome --js-runtimes node \
        -F "$url" 2>/dev/null)

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

    local exact
    exact=$(printf "%b" "$candidates" | awk -F'|' -v h="$vformat" '$2==h {print}')

    local pool
    if [ -n "$exact" ]; then
        pool="$exact"
    else
        local best_diff=999999 best_height=""
        while IFS='|' read -r id height size; do
            local diff=$(( height > vformat ? height - vformat : vformat - height ))
            if [ "$diff" -lt "$best_diff" ]; then
                best_diff=$diff
                best_height=$height
            fi
        done < <(printf "%b" "$candidates" | awk -F'|' '{print}')
        pool=$(printf "%b" "$candidates" | awk -F'|' -v h="$best_height" '$2==h {print}')
    fi

    local best_id
    best_id=$(printf "%b" "$pool" | awk -F'|' 'BEGIN{min=999999;id=""} {if($3<min){min=$3;id=$1}} END{print id}')

    echo "$best_id"
}

# ─────────────────────────────────────────────
#  Select best audio-only format
# ─────────────────────────────────────────────
select_audio_format() {
    local url="$1"
    local fmt_list
    fmt_list=$("$YT_DLP" --cookies-from-browser chrome --js-runtimes node \
        -F "$url" 2>/dev/null)

    local best_id
    best_id=$(echo "$fmt_list" | awk '
        /audio only/ && /opus|m4a/ {
            id=$1
            if (!found) { found=1; best=id }
        }
        END { print (best ? best : "bestaudio") }
    ')
    echo "$best_id"
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

    "$YT_DLP" --cookies-from-browser chrome --js-runtimes node \
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
    local moved_name
    moved_name=$(basename "$downloaded_file")

    log "  [OK] Moved: $moved_name → $move_location/"
    notify-send "Download Completed" "Song '$moved_name' downloaded and moved to $move_location." 2>/dev/null || true

    update_status "$entry_string" "Completed"
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

    # Check external target download file and ingest entries
    if [ -s "$EXTERNAL_DOWNLOAD_FILE" ]; then
        log "New entries found in $EXTERNAL_DOWNLOAD_FILE. Syncing to $DOWNLOAD_FILE..."
        mkdir -p "$(dirname "$DOWNLOAD_FILE")"
        cat "$EXTERNAL_DOWNLOAD_FILE" >> "$DOWNLOAD_FILE"
        : > "$EXTERNAL_DOWNLOAD_FILE" # Truncate external file
    fi

    # Deduplicate entries before starting main file checks and loop
    deduplicate_download_file

    # Check if processing file exists and contains data
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

main