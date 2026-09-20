#!/usr/bin/env bash
# nats-query-songs.sh — shell NATS client for the Download Service
# Queries available formats for a song URL and streams the response.
#
# Usage:
#   ./nats-query-songs.sh [request-id]
#
# Env overrides:
#   NATS_URL     default nats://192.168.12.111:4222
#   SUBJECT      default nats.query.request.songs
#   URL          default the Rick roll URL
#   RESOLUTION   default 720
#   LANG         default english
#   ACTRESS      default Rick
#   TIMEOUT      default 120 (seconds)

set -euo pipefail

NATS_URL="${NATS_URL:-nats://192.168.12.111:4222}"
NATS_CONTEXT="${NATS_CONTEXT-zbox}"         # or leave empty to use flags
NATS_USER="${NATS_USER:-zboxnats}"
NATS_PASS="${NATS_PASS:-zboxpswd}"
SUBJECT="${SUBJECT:-nats.query.request.songs}"
URL="${URL:-https://www.youtube.com/watch?v=dQw4w9WgXcQ}"
RESOLUTION="${RESOLUTION:-720}"
LANG="${LANG:-english}"
ACTRESS="${ACTRESS:-Rick}"
TIMEOUT="${TIMEOUT:-120}"

# build the CLI prefix once
nats_cmd() {
  if [ -n "$NATS_CONTEXT" ]; then
    nats --context "$NATS_CONTEXT" "$@"
  else
    nats --server "$NATS_URL" --user "$NATS_USER" --password "$NATS_PASS" "$@"
  fi
}

# ── dependency check ────────────────────────────────────────────────
for c in nats jq; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "Error: '$c' not found in PATH." >&2
    echo "  nats : https://github.com/nats-io/natscli" >&2
    echo "  jq   : apt-get install jq" >&2
    exit 1
  fi
done

# ── request id ──────────────────────────────────────────────────────
if [[ $# -ge 1 && -n "$1" ]]; then
  RID="$1"
else
  RID="$(cat /proc/sys/kernel/random/uuid)"
fi
RESP_SUBJECT="nats.query.response.$RID"

cat <<EOF
────────────────────────────────────────────────────────
 NATS URL    : $NATS_URL
 Request id  : $RID
 Request on  : $SUBJECT
 Response on : $RESP_SUBJECT
 Timeout     : ${TIMEOUT}s
────────────────────────────────────────────────────────
EOF
echo

# ── build payload ───────────────────────────────────────────────────
PAYLOAD=$(jq -nc \
  --arg id      "$RID"       \
  --arg url     "$URL"       \
  --arg res     "$RESOLUTION" \
  --arg lang    "$LANG"      \
  --arg actress "$ACTRESS"   \
  '{id:$id, url:$url, resolution:$res, lang:$lang, "actress name":$actress}')

echo "→ payload:"
echo "$PAYLOAD" | jq .
echo

# ── start subscriber on FD 3 ────────────────────────────────────────
# --raw prints raw payload to stdout, everything else to stderr.
# subscriber:
if [ -n "$NATS_CONTEXT" ]; then
  exec 3< <(timeout "$TIMEOUT" nats --context "$NATS_CONTEXT" sub "$RESP_SUBJECT" --raw 2>/dev/null)
else
  exec 3< <(timeout "$TIMEOUT" nats --server "$NATS_URL" --user "$NATS_USER" --password "$NATS_PASS" \
    sub "$RESP_SUBJECT" --raw 2>/dev/null)
fi

cleanup() { exec 3<&- 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# wait for the subscriber to actually bind
sleep 1

# ── publish ─────────────────────────────────────────────────────────
nats_cmd pub "$SUBJECT" "$PAYLOAD" >/dev/null

echo "→ request published, awaiting responses..."
echo

# ── consume stream ──────────────────────────────────────────────────
while IFS= read -r line <&3; do
  [[ -z "$line" ]] && continue

  if ! pretty=$(echo "$line" | jq . 2>/dev/null); then
    echo "$line"
    continue
  fi
  echo "$pretty"

  status=$(echo "$line" | jq -r '.status // ""')
  case "$status" in
    completed|failed)
      echo
      echo "← terminal status: $status"
      break
      ;;
  esac
done

echo "── done ──"