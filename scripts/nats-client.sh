#!/bin/bash

# nats service is running on zbox.local host, with user zboxnats and password zboxpswd
# ports:
#       - "4222:4222"
#       - "6222:6222"
#       - "8222:8222"
#
# Requires the NATS CLI: https://github.com/nats-io/natscli
#
# Usage:
#   ./nats-client.sh                      # interactive menu
#   ./nats-client.sh pub <topic> [msg]    # publish (msg omitted => read stdin lines)
#   ./nats-client.sh sub <topic>          # subscribe
#   ./nats-client.sh req <topic> <msg>    # request/reply
#   ./nats-client.sh reply <topic> <msg>  # run a reply service
#   ./nats-client.sh ping                 # test connectivity
#
# Connection details can be overridden via environment variables:
#   NATS_HOST NATS_PORT NATS_USER NATS_PASS NATS_TOPIC

set -uo pipefail

NATS_HOST="${NATS_HOST:-zbox.local}"
NATS_PORT="${NATS_PORT:-4222}"
NATS_USER="${NATS_USER:-zboxnats}"
NATS_PASS="${NATS_PASS:-zboxpswd}"
NATS_TOPIC="${NATS_TOPIC:-}"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
err() { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

require_cli() {
    if ! command -v nats >/dev/null 2>&1; then
        err "'nats' CLI not found. Install from https://github.com/nats-io/natscli/releases"
        return 1
    fi
}

nats_url() {
    printf 'nats://%s:%s' "$NATS_HOST" "$NATS_PORT"
}

# Credentials go through --user/--password so the password is never embedded in
# a URL that could end up in logs or process listings of other tools.
nats_args() {
    local args=(--server "$(nats_url)")
    if [[ -n "$NATS_USER" ]]; then
        args+=(--user "$NATS_USER" --password "$NATS_PASS")
    fi
    printf '%s\n' "${args[@]}"
}

nats_run() {
    local args=()
    mapfile -t args < <(nats_args)
    nats "${args[@]}" "$@"
}

require_topic() {
    if [[ -z "$NATS_TOPIC" ]]; then
        err "No topic set. Choose 'Set topic' from the menu or pass one as an argument."
        return 1
    fi
}

# ----------------------------------------------------------------------------
# Reusable core functions
# ----------------------------------------------------------------------------

# set_connection [host] [port] [user] [pass] - prompts for anything not passed.
set_connection() {
    local host="${1:-}" port="${2:-}" user="${3:-}" pass="${4:-}"

    [[ -z "$host" ]] && read -r -p "Host [$NATS_HOST]: " host
    [[ -z "$port" ]] && read -r -p "Port [$NATS_PORT]: " port
    [[ -z "$user" ]] && read -r -p "User [$NATS_USER]: " user
    if [[ -z "$pass" ]]; then
        read -r -s -p "Password [keep current]: " pass
        echo
    fi

    NATS_HOST="${host:-$NATS_HOST}"
    NATS_PORT="${port:-$NATS_PORT}"
    NATS_USER="${user:-$NATS_USER}"
    NATS_PASS="${pass:-$NATS_PASS}"

    log "Connection set to $(nats_url) (user: ${NATS_USER:-anonymous})"
}

# set_topic [topic] - prompts when no argument is given.
set_topic() {
    local topic="${1:-}"
    [[ -z "$topic" ]] && read -r -p "Topic [${NATS_TOPIC:-none}]: " topic
    if [[ -z "$topic" && -z "$NATS_TOPIC" ]]; then
        err "Topic cannot be empty."
        return 1
    fi
    NATS_TOPIC="${topic:-$NATS_TOPIC}"
    log "Topic set to '$NATS_TOPIC'"
}

check_connection() {
    require_cli || return 1
    log "Pinging $(nats_url) ..."
    nats_run server ping --timeout 3s
}

# publish_message <topic> <message> [count] [interval]
publish_message() {
    require_cli || return 1
    local topic="${1:?topic required}" message="${2:?message required}"
    local count="${3:-1}" interval="${4:-0}"

    local opts=(--count "$count")
    [[ "$interval" != "0" ]] && opts+=(--sleep "$interval")

    nats_run pub "$topic" "$message" "${opts[@]}"
}

# publish_stream <topic> - publishes each line read from stdin as a message.
publish_stream() {
    require_cli || return 1
    local topic="${1:?topic required}" line
    log "Reading messages from stdin. Blank line or Ctrl-D to stop."
    while IFS= read -r line; do
        [[ -z "$line" ]] && break
        publish_message "$topic" "$line" >/dev/null || return 1
        log "-> $line"
    done
}

# subscribe_topic <topic> [queue_group] [max_msgs]
subscribe_topic() {
    require_cli || return 1
    local topic="${1:?topic required}" queue="${2:-}" max="${3:-}"

    local opts=()
    [[ -n "$queue" ]] && opts+=(--queue "$queue")
    [[ -n "$max" ]] && opts+=(--count "$max")

    log "Subscribing to '$topic'${queue:+ (queue group: $queue)}. Ctrl-C to stop."
    nats_run sub "$topic" "${opts[@]}"
}

# request_reply <topic> <message> [timeout]
request_reply() {
    require_cli || return 1
    local topic="${1:?topic required}" message="${2:?message required}" timeout="${3:-5s}"
    log "Requesting on '$topic' (timeout $timeout)"
    nats_run request "$topic" "$message" --timeout "$timeout"
}

# reply_service <topic> <response> - responds to every request on the topic.
reply_service() {
    require_cli || return 1
    local topic="${1:?topic required}" response="${2:?response required}"
    log "Serving replies on '$topic'. Ctrl-C to stop."
    nats_run reply "$topic" "$response"
}

# ----------------------------------------------------------------------------
# Menu flows
# ----------------------------------------------------------------------------

run_publisher() {
    require_topic || return 1
    echo
    echo "  1) Send a single message"
    echo "  2) Send a message N times"
    echo "  3) Stream messages from stdin (one per line)"
    echo "  4) Request and wait for a reply"
    echo "  b) Back"
    read -r -p "Publisher option: " opt

    local message count interval timeout
    case "$opt" in
        1)
            read -r -p "Message: " message
            publish_message "$NATS_TOPIC" "$message" && log "Sent."
            ;;
        2)
            read -r -p "Message: " message
            read -r -p "Count [10]: " count
            read -r -p "Interval between sends [1s]: " interval
            publish_message "$NATS_TOPIC" "$message" "${count:-10}" "${interval:-1s}"
            ;;
        3) publish_stream "$NATS_TOPIC" ;;
        4)
            read -r -p "Message: " message
            read -r -p "Timeout [5s]: " timeout
            request_reply "$NATS_TOPIC" "$message" "${timeout:-5s}"
            ;;
        b|B) return 0 ;;
        *) err "Unknown option '$opt'" ;;
    esac
}

run_receiver() {
    require_topic || return 1
    echo
    echo "  1) Subscribe (all messages)"
    echo "  2) Subscribe as queue group member"
    echo "  3) Subscribe for N messages then exit"
    echo "  4) Run as reply service"
    echo "  b) Back"
    read -r -p "Receiver option: " opt

    local queue max response
    case "$opt" in
        1) subscribe_topic "$NATS_TOPIC" ;;
        2)
            read -r -p "Queue group: " queue
            subscribe_topic "$NATS_TOPIC" "$queue"
            ;;
        3)
            read -r -p "Message count [1]: " max
            subscribe_topic "$NATS_TOPIC" "" "${max:-1}"
            ;;
        4)
            read -r -p "Reply body: " response
            reply_service "$NATS_TOPIC" "$response"
            ;;
        b|B) return 0 ;;
        *) err "Unknown option '$opt'" ;;
    esac
}

show_menu() {
    local choice
    while true; do
        echo
        echo "================ NATS Client ================"
        echo " Server : $(nats_url)"
        echo " User   : ${NATS_USER:-anonymous}"
        echo " Topic  : ${NATS_TOPIC:-<not set>}"
        echo "---------------------------------------------"
        echo "  1) Set topic"
        echo "  2) Set connection details"
        echo "  3) Run as publisher"
        echo "  4) Run as receiver"
        echo "  5) Test connection"
        echo "  q) Quit"
        echo "============================================="
        read -r -p "Select an option: " choice

        case "$choice" in
            1) set_topic ;;
            2) set_connection ;;
            3) run_publisher ;;
            4) run_receiver ;;
            5) check_connection ;;
            q|Q) log "Bye."; return 0 ;;
            *) err "Unknown option '$choice'" ;;
        esac
    done
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

main() {
    case "${1:-}" in
        pub|publish)
            NATS_TOPIC="${2:?topic required}"
            if [[ -n "${3:-}" ]]; then
                publish_message "$NATS_TOPIC" "$3" "${4:-1}" "${5:-0}"
            else
                publish_stream "$NATS_TOPIC"
            fi
            ;;
        sub|subscribe) subscribe_topic "${2:?topic required}" "${3:-}" "${4:-}" ;;
        req|request)   request_reply "${2:?topic required}" "${3:?message required}" "${4:-5s}" ;;
        reply)         reply_service "${2:?topic required}" "${3:?response required}" ;;
        ping)          check_connection ;;
        ""|menu)
            require_cli || exit 1
            # Topic is asked for up front, then the user picks publisher/receiver.
            set_topic
            show_menu
            ;;
        -h|--help|help) sed -n '3,21p' "$0" ;;
        *)
            err "Unknown command '$1'. Try --help."
            exit 1
            ;;
    esac
}

main "$@"