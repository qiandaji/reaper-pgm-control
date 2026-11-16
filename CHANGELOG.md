# 更新日志

## v0.1.1
- 新增 `cleanup.sh`：一键清理测试产物（`.pytest_cache`、`__pycache__`、`*.pyc`、`screenshots/mock*.html`），不影响运行时配置 `data/`
- 新增 `make_release.sh`：版本发布全流程自动化——测试 → 清理 → 提交 → tag → 推送 → 创建 GitHub Release → 再次清理
- README 补充「清理与发布」说明

## v0.1.0
- 首发：REAPER PGM Web 控制端（region 识别曲目、播放时长与结束模式、静音轨道管理、时间码、自动连播）
- 修复：手动停止后时间码被编辑光标反馈覆盖；倒计时比设定少一秒；切歌随机自动播放；新增手动切歌后自动播放开关
