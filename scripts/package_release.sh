#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

app="dist/ModelRouter.app"
[[ -d "$app" ]] || { echo "错误: 未找到 $app，请先运行 scripts/build_app.sh" >&2; exit 1; }

arch="$(uname -m)"
release="ModelRouter-macos-${arch}"

codesign --verify --deep --strict "$app"
ditto -c -k --sequesterRsrc --keepParent "$app" "dist/${release}.zip"

staging="$(mktemp -d "${TMPDIR:-/tmp}/model-router-dmg.XXXXXX")"
ditto "$app" "$staging/ModelRouter.app"
ln -s /Applications "$staging/Applications"
hdiutil create \
  -volname "ModelRouter" \
  -srcfolder "$staging" \
  -ov \
  -format UDZO \
  "dist/${release}.dmg"

echo "已生成: dist/${release}.dmg"
echo "已生成: dist/${release}.zip"
