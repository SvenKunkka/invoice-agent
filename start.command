#!/bin/zsh
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null; then
  print "未找到 uv。请从 https://docs.astral.sh/uv/ 安装后重新打开。"
  read -r "?按回车关闭…"
  exit 1
fi
uv sync --locked --python 3.13
if [[ ! -f web/dist/index.html ]]; then
  (cd web && npm ci --ignore-scripts && npm run build)
fi
exec uv run --frozen python run.py
