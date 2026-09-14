#!/usr/bin/env bash
# 把本地 HEAD 发布到 GitHub：只取公开文件，不创建工作树。
# 用法：bash scripts/publish_github.sh ["提交说明"]
set -euo pipefail

cd "$(dirname "$0")/.."

remote="${MODEL_ROUTER_REMOTE:-github}"
branch="${MODEL_ROUTER_BRANCH:-main}"
message="${1:-publish: sync $(git rev-parse --short HEAD)}"

# 内部资料不进公开仓库：项目规范、跨会话上下文、阶段报告与设计稿
internal_paths=(
  CLAUDE.md
  docs/context.md
  docs/features
  docs/superpowers
)

index_file="$(mktemp "${TMPDIR:-/tmp}/model-router-index.XXXXXX")"
trap 'rm -f "$index_file"' EXIT
export GIT_INDEX_FILE="$index_file"

git read-tree HEAD
for path in "${internal_paths[@]}"; do
  git rm -r --cached --quiet --ignore-unmatch "$path"
done
for path in docs/optimization-audit-*; do
  [ -e "$path" ] || continue
  git rm -r --cached --quiet --ignore-unmatch "$path"
done

tree="$(git write-tree)"
parent="$(git rev-parse "$remote/$branch")"
if [ "$tree" = "$(git rev-parse "$parent^{tree}")" ]; then
  echo "公开仓库已是最新（树一致），跳过推送：${tree:0:7}"
  exit 0
fi
commit="$(git commit-tree "$tree" -p "$parent" -m "$message")"
git push "$remote" "$commit:refs/heads/$branch"
echo "已发布 ${commit:0:7} → ${remote}/${branch}（本地 HEAD $(git rev-parse --short HEAD)）"
