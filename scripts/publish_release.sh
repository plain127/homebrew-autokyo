#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="${1:-$(python3 - <<'PY'
from autokyo import __version__
print(__version__)
PY
)}"
TAG="v${VERSION}"
REPO_URL="https://github.com/plain127/AutoKyo-ebook-script"
ARCHIVE_URL="${REPO_URL}/archive/refs/tags/${TAG}.tar.gz"
TAP_DIR="${2:-}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag ${TAG} already exists locally."
else
  git tag "$TAG"
  echo "Created local tag ${TAG}"
fi

echo "Pushing main..."
git push origin main

echo "Pushing ${TAG}..."
git push origin "$TAG"

echo "Computing SHA256 for ${ARCHIVE_URL}..."
SHA256="$(curl -L "$ARCHIVE_URL" | shasum -a 256 | awk '{print $1}')"
echo "SHA256=${SHA256}"

FORMULA_OUTPUT="-"
if [[ -n "$TAP_DIR" ]]; then
  mkdir -p "${TAP_DIR}/Formula"
  FORMULA_OUTPUT="${TAP_DIR}/Formula/autokyo.rb"
fi

python3 scripts/render_homebrew_formula.py \
  --version "$VERSION" \
  --sha256 "$SHA256" \
  --output "$FORMULA_OUTPUT"

if [[ "$FORMULA_OUTPUT" != "-" ]]; then
  echo
  echo "Formula written to ${FORMULA_OUTPUT}"
  echo "Next:"
  echo "  cd ${TAP_DIR}"
  echo '  git add Formula/autokyo.rb'
  echo "  git commit -m \"Release autokyo ${TAG}\""
  echo "  git push origin main"
else
  echo
  echo "Copy the rendered formula into your Homebrew tap repository."
fi
