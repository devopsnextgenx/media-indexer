#!/usr/bin/env bash
set -euo pipefail

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)  GOARCH=amd64 ;;
  aarch64|arm64) GOARCH=arm64 ;;
  armv7l)        GOARCH=armv7   ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

VER="$(curl -fsSL https://api.github.com/repos/nats-io/natscli/releases/latest \
        | grep -oP '"tag_name":\s*"v\K[^"]+')"
echo "installing nats CLI v$VER (linux/$GOARCH)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/nats.zip" \
  "https://github.com/nats-io/natscli/releases/download/v${VER}/nats-${VER}-linux-${GOARCH}.zip"

unzip -q "$TMP/nats.zip" -d "$TMP"

BIN="$(find "$TMP" -type f -name nats -perm -u+x | head -n1)"
[ -n "$BIN" ] || { echo "nats binary not found in archive" >&2; exit 1; }

mkdir -p "$HOME/.local/bin"
install -m 0755 "$BIN" "$HOME/.local/bin/nats"

# make sure it's on PATH for zsh users
if ! grep -qs 'HOME/.local/bin' "$HOME/.zshrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
  echo '→ added ~/.local/bin to PATH in ~/.zshrc (open a new shell or: source ~/.zshrc)'
fi

"$HOME/.local/bin/nats" --version