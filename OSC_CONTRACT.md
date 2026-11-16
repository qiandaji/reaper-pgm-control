# OSC 契约（pgm-control.ReaperOSC）

> 状态声明：**本地址表按 REAPER 7 macOS 官方默认配置
> /Applications/REAPER.app/Contents/InstallFiles/OSC/Default.ReaperOSC 推导，
> 未经实机联调。** 真实部署时若 REAPER 侧反馈缺失，优先核对本文端口与 pattern 文件是否被正确选用。

## 1. 端口约定

| 方向 | 端口 | 说明 |
| --- | --- | --- |
| 桥接 → REAPER | 9100 | REAPER OSC 控制面的「监听端口」。REAPER 在这个 UDP 端口收命令 |
| REAPER → 桥接 | 9101 | 桥接自身监听端口。REAPER 把走带与 region/轨道反馈发到这里 |

环境变量：RPC_OSC_REAPER_PORT=9100、RPC_OSC_LISTEN_PORT=9101（见 .env.example）。

## 2. 最终 OSC 地址表

### 2.1 桥接 → REAPER（命令）

| OSC 地址 | 参数 | 用途 |
| --- | --- | --- |
| /play | 无参（trigger） | 播放 |
| /stop | 无参（trigger） | 停止 |
| /pause | 无参（trigger） | 暂停 |
| /time | float（秒） | 设置走带位置（play_song 用 region.start_s 跳转） |
| /track/@/mute | int 0/1 | 设置轨道静音；@ 见 §3 |
| /device/track/follows/device | 无参（trigger） | 握手①（每 2s，无副作用）：让「仅监听（接收）」模式的 REAPER 学到反馈回送地址（= 最后消息源）；重新确认 DEVICE_TRACK_FOLLOWS=DEVICE |
| /device/track/count | int | 握手②：声明设备轨道容量 64，促使 REAPER 重发当前轨道列表 |
| /device/region/count | int | 握手③：声明设备 region 容量 64，促使 REAPER 重发当前 region 列表 |
| /action | int 41743 | 启动刷新（仅网关启动时发一次）：触发 REAPER「Control surface: Refresh all surfaces」重发全量反馈，桥接重启后可自愈 |

### 2.2 REAPER → 桥接（反馈）

| OSC 地址 | 参数 | 桥接动作 |
| --- | --- | --- |
| /time | float（秒） | 更新 transport.position_s |
| /play | int 1/0 | 1 → playing=true；0 → 视为停止 |
| /stop | —（trigger） | playing=false |
| /pause | —（trigger） | playing=false |
| /track/@/name | string | 更新轨道名 |
| /track/@/mute | int 0/1 | 更新轨道静音状态 |
| /region/@/name | string | 更新 region 名称 |
| /region/@/time | float（秒） | 更新 region 起点 |
| /region/@/length | float（秒） | 更新 region 长度（桥接据此重算 end_s） |

## 3. @ 索引语义

- 桥接内部 region_id = "region:{index}"，index 从 0 起，按时间轴起点升序排列（桥接侧在
  region 反馈到达后按 start_s 排序自建）。
- OSC 地址里的 @ 是 **REAPER 编号，1 起**：
  - track：@ = REAPER 轨道号（1 起，与 REAPER 界面/路由面板一致）。Default.ReaperOSC
    行 186-188 的 bank 示例可推导：bank 偏移 + @ 才是目标轨道（本配置
    DEVICE_TRACK_BANK_FOLLOWS DEVICE 且不做 bank 切换，故 @ 即轨道号）。
  - region：@ = REAPER region 编号（1 起，与 REAPER Region Manager 的 "#" 列一致）。
    桥接收到 /region/@/time 时把 @ 作为 reaper_number 关联到内部 region。
  - 换算：reaper_number = index + 1；REAPER 轨道号 = WS track.index + 1。
- CONTRACT.md 的 mute_tracks 是 REAPER 轨道号（1-based）。桥接 WS 命令 set_track_mute
  的 index 同为 1-based REAPER 轨道号，原样发 /track/@/mute（不做 +1），语义与 REAPER 界面一致。

## 4. DEVICE_* 取值（pgm-control.ReaperOSC）

- DEVICE_TRACK_COUNT 64 —— 控制面覆盖的轨道数上限（超出不反馈）。REAPER 会把它填充到
  该容量（空轨道为默认名 "Track N"），因此该值取现场工程实际需要的最小值。
- DEVICE_REGION_COUNT 64 —— 控制面覆盖的 region 数上限（超出不反馈）。REAPER 会把它
  填充到该容量（空槽 start/length 为 0）；桥接层在快照中过滤 length<=0 的空 region。
- DEVICE_SEND_COUNT 0 / DEVICE_RECEIVE_COUNT 0 / DEVICE_FX_COUNT 0 /
  DEVICE_FX_PARAM_COUNT 0 / DEVICE_FX_INST_PARAM_COUNT 0 / DEVICE_MARKER_COUNT 0 ——
  本桥接不需要 send/receive/FX/marker 反馈，置 0 减少报文量。

## 5. 业务语义在 OSC 上的映射

- play_song：先 unmute 上一首 mute_tracks 记录的轨道（/track/@/mute 0），再 /time 跳到
  region.start_s，再 /play。
- stop_mute：/stop + 对本曲 mute_tracks 逐条 /track/@/mute 1，并记录已静音集合供下次
  切歌恢复。
- auto_next 触发结束动作时：直接 /stop 后切下一首（不做 stop_mute 静音）。
- 自然播完（无 limit，position ≥ region.end_s）且 auto_next=true：/stop 后直接下一首。
- 手动 stop：仅 /stop，不静音、不切歌，current_song 保留，elapsed 冻结。
- auto_next 的最后一首策略：**循环（最后一首 → 第一首，index 回到 0）**。

## 6. REAPER 一次性设置步骤

桥接现在**从 9101 端口发出**所有命令，因此无论 REAPER 用下面哪种模式都能收到反馈。

### 方式 A（推荐，双向明确）

1. Preferences → Control/OSC/web → Add → OSC。
2. Pattern config 选本项目的 pgm-control.ReaperOSC（点 "..." 浏览选择）。
3. **Control surface mode 选「配置设备 IP + 本地监听端口」**
   （英文："Configure device IP + local listen port"）——这个模式才有发送目标字段。
4. Device IP 填 **127.0.0.1**；设备端口（发反馈目标）填 **9101**；
   本地监听端口（收命令）填 **9100**。
5. 确定生效（建议重启 REAPER）。

### 方式 B（你当前只有「监听端口」的模式，也可以直接用）

REAPER 的「仅本地端口（接收）」模式（英文："Local port (receive only)"）只有监听端口
字段，反馈会回发给**最后一次发来消息的源地址**。桥接已改为从 9101 发命令并每 2s 发
一次心跳，所以：监听端口填 **9100** 即可，反馈会自动回到桥接的 9101，无需（也没有）
发送端口字段。

> 注意：改完 pattern 文件后，需要在 REAPER 里重新选择一次
> pgm-control.ReaperOSC（或删除再 Add 该控制面），REAPER 才会加载新版 pattern。

## 7. 运行桥接

```bash
cd reaper-pgm-control
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 61370
```
