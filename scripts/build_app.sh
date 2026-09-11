#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv sync --extra build
uv run pyinstaller --noconfirm --clean ModelRouter.spec
bash scripts/package_release.sh

echo "已生成 dist/ModelRouter.app"
