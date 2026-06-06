#!/usr/bin/env bash
# Install cyclopsctl globally on macOS/Linux.
set -euo pipefail

SOURCE="pypi"
GIT_URL="git+https://github.com/OlaProeis/Cyclopsctl.git"
LOCAL_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install cyclopsctl globally. Default path: Python 3.10+ only.

Options:
  --source pypi|git|local   Install source (default: pypi)
  --git-url URL             Git URL when --source git
  --local-path PATH         Project path when --source local
  -h, --help                Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="$2"
      shift 2
      ;;
    --git-url)
      GIT_URL="$2"
      shift 2
      ;;
    --local-path)
      LOCAL_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "Python was not found on PATH. Install Python 3.10+ first." >&2
  exit 1
fi

PYTHON="$(command -v python3 2>/dev/null || command -v python)"

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
  VERSION="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  echo "Python 3.10+ is required (found ${VERSION})." >&2
  exit 1
fi

case "$SOURCE" in
  pypi) TARGET="cyclopsctl" ;;
  git) TARGET="$GIT_URL" ;;
  local) TARGET="$LOCAL_PATH" ;;
  *)
    echo "Unsupported source: $SOURCE" >&2
    exit 2
    ;;
esac

echo "Installing cyclopsctl from ${SOURCE}..."
"$PYTHON" -m pip install --upgrade "$TARGET"

VERIFY_ARGS=(-m cyclopsctl.installer --verify-only)

"$PYTHON" "${VERIFY_ARGS[@]}"
