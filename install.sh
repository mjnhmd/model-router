#!/usr/bin/env bash
# model-router 一键启动：uv sync + 配置准备 + 原生 App
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 1/4 安装依赖 (uv sync)"
command -v uv >/dev/null 2>&1 || { echo "错误: 未找到 uv，请先安装 https://docs.astral.sh/uv/"; exit 1; }
uv sync

echo "==> 2/4 准备配置"
if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  echo "已生成 config.yaml，请编辑填入服务器地址与 api_key"
  echo "（其他服务/模型按需增删，参考 config.example.yaml 注释）"
fi

echo "==> 3/3 启动原生 App"
echo "App 运行期间临时接管 Codex，退出后自动恢复；不会留下指向死代理的配置。"
exec uv run model-router gui -c config.yaml
