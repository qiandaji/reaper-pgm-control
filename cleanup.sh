#!/usr/bin/env bash
# 清理测试与调试产生的文件。
# 注意：不删除运行时配置 data/、虚拟环境 .venv/，不影响用户设置。
set -euo pipefail
cd "$(dirname "$0")"

rm -rf .pytest_cache
find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
find . -type f -name '*.pyc' -not -path './.venv/*' -delete 2>/dev/null || true
rm -f screenshots/mock*.html

echo "清理完成：已删除 .pytest_cache、__pycache__、*.pyc、screenshots/mock*.html（data/ 运行时配置保留）"
