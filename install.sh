#!/bin/sh
# cpa-stats installer.
#
#   curl -fsSL https://raw.githubusercontent.com/maomaocun/cpa-stats/master/install.sh | sh
#
# Downloads the standalone static binary from the latest GitHub release (no
# Python, no CGO — a single file). When your platform has no prebuilt binary,
# falls back to `go build` from the source tarball (Go ≥ 1.21 needed).
# Env: CPA_STATS_REPO (fork), CPA_STATS_TAG (pin a version),
# CPA_STATS_DEST (binary install path, default ~/.local/bin/cpa-stats),
# CPA_STATS_PROXY (http proxy for GitHub; else https_proxy/HTTPS_PROXY;
# else auto-detected 127.0.0.1:7890 when a local proxy is listening).
set -eu

REPO="${CPA_STATS_REPO:-maomaocun/cpa-stats}"
DEST="${CPA_STATS_DEST:-$HOME/.local/bin/cpa-stats}"

PROXY="${CPA_STATS_PROXY:-${https_proxy:-${HTTPS_PROXY:-}}}"
if [ -z "$PROXY" ]; then
    # common clash/mihomo local proxy — auto-use when it is listening
    if curl -fsS -m 2 -o /dev/null --proxy http://127.0.0.1:7890 \
            https://api.github.com 2>/dev/null; then
        PROXY="http://127.0.0.1:7890"
    fi
fi
gh_curl() {
    if [ -n "$PROXY" ]; then
        curl --proxy "$PROXY" "$@"
    else
        curl "$@"
    fi
}

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$os" in linux|darwin) ;; *) os=linux ;; esac
arch="$(uname -m)"
case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
esac

TAG="${CPA_STATS_TAG:-}"
if [ -z "$TAG" ]; then
    TAG="$(gh_curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1 || true)"
fi
[ -n "$TAG" ] || TAG=master

echo "cpa-stats ${TAG} (${os}-${arch})"
[ -n "$PROXY" ] && echo "via proxy $PROXY"

# 1) standalone binary from the release page
url="https://github.com/$REPO/releases/download/$TAG/cpa-stats-$os-$arch"
mkdir -p "$(dirname "$DEST")"
if gh_curl -fsSL --retry 3 --retry-all-errors -C - "$url" -o "$DEST"; then
    chmod +x "$DEST"
    "$DEST" --version && echo "installed: $DEST"
    exit 0
fi

# 2) build from source with Go
echo "no prebuilt binary for ${os}-${arch}; building from source…"
if ! command -v go >/dev/null 2>&1; then
    echo "error: no binary for ${os}-${arch} and no 'go' toolchain found" >&2
    exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
gh_curl -fsSL "https://github.com/$REPO/archive/$TAG.tar.gz" \
    -o "$tmp/src.tar.gz"
tar -xzf "$tmp/src.tar.gz" -C "$tmp"
srcdir="$(find "$tmp" -maxdepth 2 -name go.mod -path '*/go/*' | head -1)"
[ -n "$srcdir" ] || { echo "error: go module not found in tarball" >&2; exit 1; }
cd "$(dirname "$srcdir")"
CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X main.Version=${TAG#v}" -o "$DEST" .
chmod +x "$DEST"
"$DEST" --version
echo "done — try 'cpa-stats doctor' to check your CPA setup"
