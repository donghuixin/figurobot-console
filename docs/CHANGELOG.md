# 变更日志

## 2026-08-18

### 新增功能

#### 1. AI 语音助手（DeepSeek 驱动）

机器人接入 DeepSeek API，作为独立语音助手（唤醒词「你好小图」），与小爱同学等封闭生态并存。

- `voice_agent.py` — 语音助手主程序：录音 → ASR → DeepSeek 对话 → 本地记忆 → TTS → 喇叭播放，支持 `--once`/`--text` 模式
- `memory_store.py` — SQLite 本地记忆库（对话历史 / 用户画像 / 语义事实三表，中文检索用 LIKE 子串匹配）
- `config.json` — 配置模板（含 DeepSeek API key，**勿提交**）
- `deploy_voice_agent.sh` — 一键部署到控制盒

**关键设计**：
- DeepSeek 通过 **function calling** 控制身体（`play_motion`/`read_pose`/`reset_motion` 走 motion_main socket），说「跳个科目三」机器人真的跳舞
- 记忆库跨会话生效（换新实例仍能记住「我叫小明」）
- 麦克风/喇叭 = 控制盒 card2 声卡（Yundea A31-1 USB，16kHz 单声道录音）

#### 2. 网页控制台 AI 对话面板

`figurobot-console.html` 新增「AI 对话」卡片：对话气泡 + 文本输入 + 麦克风按钮（浏览器 Web Speech API）。

链路：网页 → 桥接 `/api/chat` → adb → 控制盒 `voice_agent --once` → DeepSeek + 记忆库 → 回复 + 动作 + TTS。

#### 3. 实时姿态三档自适应

- 后端 `robot_bridge.py`：`read_pose(fast)` 改为「PING 拿在线/错误码 + **SYNC_READ 一次读全部位置**（实测 1 帧读回 27 舵机）+ 漏读逐个 READ 回退」，新增 `fast` 模式（跳过 PING）
- 前端：姿态面板新增「实时」开关，三档自适应——关闭 / 1 秒（空闲）/ 2.5 秒（动作播放中，避免与 motion_main 抢串口）

### 问题排查与修复

#### 1. 舵机异常状态（核心问题，彻底断电解决）

**现象**：27 个舵机全部报未定义错误码 `0x10`，动作无响应，之后通信乱码（PING 有响应但数据错乱）。

**排查过程**：
- 确认 `0x10` 官方从未定义（官方 demo 与 motion_main 编译产物的 `_ERROR_NAMES` 均只定义 0x01–0x07）
- 排除电池电量、指令错误、芯片损坏等假设

**根因**：舵机长时间处于异常状态（`0x10` 累积），通信逐渐异常。

**解决**：**彻底断电整个机器人**（连舵机一起掉电），舵机内部状态复位，`0x10` 清为 `00`，27 舵机全部恢复正常。仅重启控制盒或插拔 USB 无效。

#### 2. 磁盘满（日志刷屏）

根分区 12G 用满 100%，元凶是日志无限刷屏：
- `kern.log` 刷 `rkisp...Not active sensor`（摄像头 sensor 未激活，内核无限刷错，669 万行）
- `syslog` 刷 `Too many open files`（motion_main 文件描述符耗尽，2500 万行）

已清空刷屏日志释放约 7.7G。**注意**：摄像头刷错根因未根治，日志会再次膨胀。

#### 3. motion_main 崩溃

磁盘满导致 `~/.config/ticos/session_config` 写入成 0 字节空文件 → ticos_client `json.load` 崩溃。已修复。

#### 4. pid.yaml 路径错误

motion_main config.json 的 `pid_yaml_path` 指向不存在路径，已软链接修复。

### 遗留问题

1. **摄像头刷错**：`rkisp-vir0: rkisp_enum_frameintervals Not active sensor`（摄像头 sensor 未激活，某进程反复打开 `/dev/video4` 触发）。需禁用摄像头轮询或修复摄像头硬件，否则磁盘会反复被填满。
2. **motion_main fd 耗尽**：`Too many open files`（socket.accept 失败），软限制 1024，与反复 socket 连接有关。
3. **机器人麦克风 ASR**：已确认麦克风硬件可用（pulseaudio 录音正常），但 ASR（sherpa-onnx）尚未接入，语音输入暂用浏览器 Web Speech API 替代。

### 技术要点

- **SYNC_READ 提速**：广播 1 帧读回全部 27 舵机位置，替代逐个 READ（省 ~26 帧发送）
- **串口节点漂移**：重新插拔后 `/dev/ttyACM` 节点号会变，必须用 `/dev/serial/by-id/` 稳定名
- **motion_main socket 单向**：`:Play` 等命令 fire-and-forget，不回执，桥接读到空响应属正常
- **机器人有独立麦克风**（card2 = Yundea A31-1 USB 声卡），录音需走 pulseaudio（`arecord -D pulse`），直接 `plughw:2,0` 会被占用
