#!/bin/sh
# cpa-stats installer.
#
#   curl -fsSL https://raw.githubusercontent.com/maomaocun/cpa-stats/master/install.sh | sh
#
# Prefers the standalone binary from the latest GitHub release (no Python
# needed); falls back to pipx, then pip --user, installed from the tag.
# Env: CPA_STATS_REPO (fork), CPA_STATS_TAG (pin a version),
# CPA_STATS_DEST (binary install path, default ~/.local/bin/cpa-stats).
set -eu

REPO="${CPA_STATS_REPO:-maomaocun/cpa-stats}"
DEST="${CPA_STATS_DEST:-$HOME/.local/bin/cpa-stats}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$os" in linux|darwin) ;; *) os=linux ;; esac
arch="$(uname -m)"
case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
esac

TAG="${CPA_STATS_TAG:-}"
if [ -z "$TAG" ]; then
    TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1 || true)"
fi
[ -n "$TAG" ] || TAG=master

echo "cpa-stats ${TAG} (${os}-${arch})"

# 1) standalone binary from the release page
url="https://github.com/$REPO/releases/download/$TAG/cpa-stats-$os-$arch"
mkdir -p "$(dirname "$DEST")"
if curl -fsSL "$url" -o "$DEST"; then
    chmod +x "$DEST"
    "$DEST" --version && echo "installed: $DEST"
    exit 0
fi

# 2) python installers from the tag
pkg="git+https://github.com/$REPO@$TAG"
echo "no prebuilt binary for ${os}-${arch}; installing from source…"
if command -v pipx >/dev/null 2>&1; then
    pipx install "$pkg"
elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user "$pkg"
elif command -v python3 >/dev/null 2>&1; then
    python3 -m pip install --user "$pkg"
else
    echo "error: no binary for ${os}-${arch} and no pipx/pip3/python3 found" >&2
    exit 1
fi
cpa-stats --version
echo "done — try 'cpa-stats doctor' to check your CPA setup"
