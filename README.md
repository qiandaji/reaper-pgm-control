# PGM Reaper 控制端

面向现场 PGM 播放的 REAPER Web 控制端：通过 **region** 识别曲目，自由控制播放时长与
结束模式（如“播 3 分钟后停止并自动静音指定轨道，切歌后自动恢复这些轨道”），页面带
已播/剩余时间码。

> ⚠️ **本项目完全由 AI 生成**：全部代码、文档、配置文件与发布说明均由 AI 编写，
> 未经人工逐行审查，生产环境使用前请自行验证。
>
> 📄 本项目**暂未选择开源协议**（No License）：默认保留所有权利，他人复制、分发、
> 修改均需获得授权。

## 组成

- `app/` — Python 桥接服务（FastAPI + pydantic v2 + python-osc），REST + WebSocket
- `app/static/` — 纯静态前端（无构建、无 CDN，离线可用；中文，移动优先，深色主题）
- `pgm-control.ReaperOSC` — REAPER 内置 OSC 控制面的 pattern 文件
- `OSC_CONTRACT.md` — OSC 地址表、索引语义、REAPER 一次性设置步骤
- `tests/` — pytest（45 个用例：状态机 / REST / WS / OSC 编解码 / 网关生命周期回归）

## 环境要求

- REAPER 7（Windows 或 macOS）
- Python 3.10+（Windows 上若 `py -3` 不可用会回退到 `python`）
- 网络端口：61370（HTTP/WS）、9100（桥接→REAPER）、9101（REAPER→桥接）

## 本地部署

### 方式一：下载 Release（推荐）

1. 在 GitHub Releases 页面下载最新版本的 **Source code (zip 或 tar.gz)**。
2. 解压到任意目录。
3. 按下方「快速开始」启动。

### 方式二：克隆源码

```bash
git clone https://github.com/<你的用户名>/reaper-pgm-control.git
cd reaper-pgm-control
```

## 快速开始

1. **REAPER 一次性设置**（详见 `OSC_CONTRACT.md` §6，两种模式均可）：
   Preferences → Control/OSC/web → Add → OSC，Pattern 选择本项目的
   `pgm-control.ReaperOSC`；推荐「配置设备 IP + 本地监听端口」模式（监听 9100、
   设备 127.0.0.1:9101）；若只有「监听端口」字段（仅本地端口接收模式），只填 9100
   也能用（桥接每 2s 发心跳，反馈自动回到 9101）。改完 pattern 后需重新选择一次
   pattern 文件使其生效。
2. **启动桥接**：
   - macOS：双击 `start_macos.command`（或终端执行 `./start_macos.command`）
   - Windows：双击 `start_windows.bat`
   - 首次运行会自动创建 `.venv` 并安装 `requirements.txt`
   - 重复启动不会报错：脚本检测到已有实例在跑时，会直接打开控制页并退出；
     若提示 OSC 端口 9101 被占用，先按提示释放端口再重试
3. **打开控制页**：http://127.0.0.1:61370 （同机访问；如需手机/平板访问，用本机
   LAN IP 替换 127.0.0.1，并确保防火墙放行 61370）。

## 使用说明

- 顶部显示当前曲目与时间码：已播 M:SS / 剩余 M:SS（倒计时向上取整，输入 3:00 从
  0:03 开始倒数；剩余 <10s 变红闪烁；超时显示负值）。
- 停止、播放/暂停为走带按钮；自动连播开关控制到时/自然播完后是否自动切下一首
  （循环，最后一首回到第一首）。
- 曲目列表按 region 时间轴排序，当前播放行高亮；点击行切歌（默认弹确认层，可在全局
  设置中关闭）。全局设置「手动切歌后自动播放」默认开启；关闭后点击切歌只跳转到该曲
  起点并暂停（cue），按播放键从该点继续。
- 每首曲目可点“设置”：播放时长（秒，0=不限）、到时动作（不动 / 停止 / 停止+静音）、
  静音轨道多选；“恢复默认”继承全局设置。
- 轨道区显示全部轨道，点按切换手动静音（不影响切歌恢复逻辑）。
- WS 断开时顶部显示“已断开，重连中…”，重连后自动恢复状态。

## 配置

环境变量前缀 `RPC_`（可选 `.env` 文件，参考 `.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RPC_HOST` | 127.0.0.1 | HTTP/WS 监听地址（脚本固定用 0.0.0.0 启动） |
| `RPC_PORT` | 61370 | Web 控制端口 |
| `RPC_DATA_DIR` | data | 配置持久化目录（data/config.json） |
| `RPC_OSC_REAPER_HOST` | 127.0.0.1 | REAPER 所在主机 |
| `RPC_OSC_REAPER_PORT` | 9100 | REAPER OSC 监听端口（收命令） |
| `RPC_OSC_LISTEN_HOST` | 127.0.0.1 | 桥接 OSC 监听地址 |
| `RPC_OSC_LISTEN_PORT` | 9101 | 桥接 OSC 监听端口（收反馈） |
| `RPC_BROADCAST_INTERVAL_S` | 0.1 | WS state 广播间隔（约 10Hz） |

## 已知限制

- 地址表已在 REAPER 7 macOS 实机联调通过（配置方式见 `OSC_CONTRACT.md` §6）；
  Windows 侧 `start_windows.bat` 尚未实测。
- 轨道/region 容量固定为 **64**（pattern 与桥接握手一致）。REAPER 会把列表填充到
  该容量：空 region 为空槽（桥接已在快照中过滤 length<=0 且无名的占位），空轨道为
  默认名 `Track N`（无法与用户手动建的同名空轨区分，故不过滤）。超出 64 的
  轨道/region 不会反馈。
- 桥接重启后会自动发送 `/action 41743`（Refresh all surfaces）让 REAPER 重发全量
  反馈，无需重加控制面；但 **pattern 文件本身变更后需在 REAPER 中 Remove 再 Add**
  该控制面一次才会重新加载 pattern，并需**重启桥接服务**（旧轨道/region 列表只增
  不减，不会自动缩容）。
- region 列表由 REAPER OSC 反馈实时同步（名称/起点/长度）；桥接按 start_s 升序自建
  `region:0..N` 编号。

## 测试

```bash
cd reaper-pgm-control
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
```

## 维护

本项目由 AI 持续维护：每次功能修改后会自动更新 GitHub 仓库与 Release。
