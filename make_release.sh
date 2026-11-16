#!/usr/bin/env bash
# 一键制作新版本：测试通过 → 发布前清理 → 提交 → 打 tag → 推送 → 创建 GitHub Release → 发布后再次清理。
# 用法: GH_TOKEN=xxx ./make_release.sh v0.1.1
# GH_TOKEN 可省略（脚本会从 /tmp/pgm-git-credentials 读取）。
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:?用法: ./make_release.sh <版本号，如 v0.1.1>}"
GH_TOKEN="${GH_TOKEN:-}"

# 1. 依赖与测试（测试必须全绿才继续）
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
fi
.venv/bin/python -m pytest tests/ -q

# 2. 发布前清理测试产物
./cleanup.sh

# 3. 提交 + 打 tag + 推送
git add -A
if git diff --cached --quiet; then
  echo "没有需要提交的改动"
else
  git commit -m "Release ${VERSION}" --quiet
fi
git tag -a "${VERSION}" -m "${VERSION}（AI 生成）" --force
git push origin main --tags

# 4. 创建 GitHub Release（说明取自 CHANGELOG.md 对应版本节）
if [ -z "${GH_TOKEN}" ] && [ -f /tmp/pgm-git-credentials ]; then
  GH_TOKEN="$(sed -n 's#^https://x-access-token:\([^@]*\)@github.com$#\1#p' /tmp/pgm-git-credentials | head -1)"
fi
if [ -z "${GH_TOKEN}" ]; then
  echo "缺少 GitHub token：请设置环境变量 GH_TOKEN，或确保 /tmp/pgm-git-credentials 存在" >&2
  exit 1
fi

python3 - "${VERSION}" "${GH_TOKEN}" <<'PY'
import json, re, sys, urllib.request

version, token = sys.argv[1], sys.argv[2]
repo = "qiandaji/reaper-pgm-control"

with open("CHANGELOG.md", encoding="utf-8") as f:
    changelog = f.read()
m = re.search(rf"(?ms)^## {re.escape(version)}\n(.*?)(?=^## |\Z)", changelog)
section = m.group(1).strip() if m else "（本次版本说明见 CHANGELOG.md）"

body = (
    "> ⚠️ 本项目完全由 AI 生成。📄 暂无 License，保留所有权利。\n\n"
    + section
    + "\n\n## 部署\n"
    "1. 下载本 Release 的 Source code (zip/tar.gz) 并解压\n"
    "2. 按 README「快速开始」完成 REAPER OSC 设置并启动桥接\n"
    "3. 浏览器打开 http://127.0.0.1:61370\n"
)
data = json.dumps({
    "tag_name": version,
    "name": version,
    "body": body,
    "target_commitish": "main",
    "draft": False,
    "prerelease": False,
}).encode()
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases",
    data=data,
    method="POST",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    d = json.load(resp)
print("Release:", d.get("html_url"))
PY

# 5. 制作完成后再次清理
./cleanup.sh
echo "完成：${VERSION} 已发布，工作区已清理"
