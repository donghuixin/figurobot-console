# Figurobot 语音 AI 化改造技术方案

> 目标：① 让机器人接入 DeepSeek API；② 在控制盒本地构建记忆库；③ 通过声卡实现与机器人的语音对话；④ 接入小米音箱生态。
> 日期：2026-08-18

---

## 0. 现状盘点（已实测确认）

| 项目 | 现状 |
|---|---|
| 控制盒 | Rockchip RK3xxx，Ubuntu 20.04 aarch64，用户 `lckfb` |
| **声卡** | 3 块：`card0`=rk809 板载 codec；`card1`=HDMI；**`card2`=Yundea A31-1 USB 声卡（机器人的麦克风+喇叭，已设为默认声卡）** |
| 麦克风 | `card2` capture：**16 kHz 单声道 S16_LE**（正好是语音识别标准采样率） |
| 喇叭 | `card2` playback：48 kHz 立体声 S16_LE |
| 音频播放 | motion_main 用 `ffplay` 播 mp3/aac；`aplay`/`arecord` 可用；`pulseaudio`+`pactl` 可用 |
| 现有 AI | `app_config.json` 里 `"llm_type": "ticos"`，即官方 Ticos Agent 云服务 |
| AI 架构 | motion_main 通过 `start_ticos`/`stop_ticos` 拉起 `ticos-agent` 进程；`ticos_client` 用 WebSocket 连远程，支持 `input_audio` / `response.audio_transcript.delta` / `model.enable_memory_generation` |
| 动作控制 | Unix socket `/tmp/motion_main_socket`，`:Play <动作名> <帧率>`，105 个内置动作 |
| 现有桥接 | PC 端 `robot_bridge.py`（端口 8888，adb→HTTP），网页控制台 `figurobot-console.html` |

**关键结论**：机器人硬件上「麦克风 + 喇叭 + 语音播放 + AI 对话框架」**全都已经具备**，只是当前 AI 走的是官方 Ticos 云服务。我们要做的是把 AI 后端从 Ticos 换成 DeepSeek，并补上本地记忆库。

---

## 1. 总体架构

```
                 ┌─────────────────────────────────────────────┐
                 │            控制盒 (Ubuntu aarch64)            │
                 │                                               │
  麦克风(card2) ──► ASR ──► DeepSeek API ──► 文本回复              │
  16kHz 录音          │         ▲                │                │
                      │         │                ▼                │
                      │    记忆库(SQLite/向量)   TTS ──► 喇叭(card2) │
                      │         ▲                │   48kHz 播放     │
                      │         └────────────────┘                │
                      │          记忆检索+写入                     │
                      └───────────────┬───────────────────────────┘
                                      │ function calling（工具调用）
                                      ▼
                            motion_main socket ──► 动作播放/姿态/复位
```

分层清晰，两件事解耦：
1. **对话脑**（新写 `voice_agent.py`，跑在控制盒）：ASR → DeepSeek → 记忆库 → TTS
2. **身体**（复用 motion_main）：对话脑通过 function calling 让 DeepSeek 决定何时播放哪个动作

---

## 2. 音频控制（已具备，直接复用）

控制盒声卡已确认可用，无需改动硬件：

```bash
# 录音测试（麦克风，16kHz 单声道）
arecord -D plughw:2,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav

# 播放测试（喇叭，48kHz）
aplay -D plughw:2,0 /tmp/test.wav

# 通过 pulseaudio（默认声卡已是 Yundea）
pactl set-default-sink alsa_output.usb-Yundea_Technology_Yundea_A31-1-*.analog-stereo
pactl set-default-source alsa_input.usb-Yundea_Technology_Yundea_A31-1-*.mono-fallback
```

**注意**：机器人的「动作音乐」和「语音回复」共用同一块声卡，需要在 `voice_agent` 里做互斥——TTS 播放前先发 `:Play` 停止动作，或错开播放。

---

## 3. DeepSeek API 接入

DeepSeek 是 **OpenAI 兼容接口**，两个模型：

| 模型 | 用途 |
|---|---|
| `deepseek-chat`（V3） | 日常对话、工具调用，低延迟（对话主选） |
| `deepseek-reasoner`（R1） | 复杂推理，慢且贵（可选） |

### 3.1 基础接入（Python）

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def chat(messages, tools=None):
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        tools=tools,            # function calling，让 DeepSeek 控制机器人
        stream=False,
    )
    return resp.choices[0].message
```

### 3.2 关键点：DeepSeek 没有语音接口

DeepSeek **只提供文本**（chat completions + function calling），**没有** ASR/TTS/Realtime 语音接口。所以语音链路必须自己拼：

```
麦克风 → [ASR] → 文本 → DeepSeek → 文本 → [TTS] → 喇叭
```

ASR 和 TTS 的选型见第 5 节。

### 3.3 function calling 让 DeepSeek 控制机器人

给 DeepSeek 定义工具，让它能「跳舞」「挥手」「复位」「读姿态」：

```python
TOOLS = [
    {"type": "function", "function": {
        "name": "play_motion",
        "description": "让机器人播放一个动作，如跳舞、挥手",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "动作名，如 挥手1、科目三"},
                "frame_rate": {"type": "integer", "description": "帧率，默认 30"}
            },
            "required": ["name"]
        }
    }},
    {"type": "function", "function": {
        "name": "read_pose",
        "description": "读取机器人当前关节姿态",
        "parameters": {"type": "object", "properties": {}}
    }},
]
```

收到 `tool_calls` 后，调用 `robot_bridge.py` 的 HTTP API 或直接发 socket 命令执行，再把结果回填给 DeepSeek。

---

## 4. 本地记忆库

### 4.1 设计目标

机器人要「记得」三样东西，跨会话持久化：

1. **对话历史** —— 最近 N 轮，用于上下文连续
2. **用户画像** —— 用户姓名、偏好、常用指令（长期）
3. **语义记忆** —— 关键事实（如「用户说下次见面要挥手」），供跨会话检索

### 4.2 技术选型（推荐组合）

| 类型 | 方案 | 理由 |
|---|---|---|
| 结构化存储 | **SQLite**（控制盒自带，零依赖） | 存画像、对话摘要、事实三元组 |
| 语义检索 | **SQLite + FTS5 全文索引** 或 **chromadb 向量库** | 第一步先用 FTS5 零成本起步；需要语义理解时再上向量库 + 本地 embedding 模型 |
| 存储位置 | `/userdata/data/robot_memory/` | 与控制盒其他数据同目录，随系统持久 |

### 4.3 数据库 Schema（初版）

```sql
CREATE TABLE conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts  TEXT DEFAULT (datetime('now','localtime')),
    role TEXT,          -- user / assistant / system
    content TEXT
);

CREATE TABLE profile (
    key   TEXT PRIMARY KEY,   -- 如 user_name, user_city
    value TEXT
);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts  TEXT,
    fact TEXT,          -- 三元组或自然语言事实
    UNIQUE(fact)
);

-- FTS5 全文索引，支持语义关键词检索
CREATE VIRTUAL TABLE facts_fts USING fts5(fact);
```

### 4.4 记忆注入策略

每轮对话前，从记忆库检索相关内容拼进 system prompt：

```python
def build_system_prompt():
    profile = load_profile()          # 用户画像
    facts   = retrieve_facts(query)   # 语义记忆（FTS5 或向量检索）
    recent  = load_recent_history()   # 最近对话
    return f"""你是 Figurobot 机器人。
用户信息：{profile}
相关记忆：{facts}
请自然、简短地回应用户。"""
```

每轮对话后，用 DeepSeek 自动抽取值得记住的事实（`memory_generation`），写入 `facts` 表。

---

## 5. 语音对话链路（ASR + TTS 选型）

### 5.1 ASR（语音转文字）

| 方案 | 优缺点 | 建议 |
|---|---|---|
| **本地 sherpa-onnx / whisper.cpp** | 离线、免费、隐私好；aarch64 可跑，但吃 CPU | **首选**（机器人应离线可用） |
| 云端（讯飞/百度/腾讯 ASR） | 识别率高；需联网+API key+付费 | 备选 |
| 小米小爱 ASR | 有开放平台 | 仅当走小爱路线时用 |

**推荐**：`sherpa-onnx` 的流式中文 ASR，模型约 100MB，aarch64 官方有预编译包。麦克风 16kHz 单声道正好匹配其默认输入。

### 5.2 TTS（文字转语音）

| 方案 | 优缺点 | 建议 |
|---|---|---|
| **edge-tts**（微软免费） | 免费、音质好、中文自然；需联网 | **首选**（控制盒联网即可） |
| 本地 piper / sherpa-onnx TTS | 离线；中文音质一般 | 备选 |
| 讯飞/阿里云 TTS | 音质最好；付费 | 需要高音质时用 |

**推荐**：`edge-tts`，`pip install edge-tts`，生成 mp3 后用 `ffplay` 播放（与 motion_main 一致的播放方式）。

### 5.3 完整对话循环

```python
import subprocess, edge_tts

def voice_loop():
    while True:
        # 1. 录音（麦克风 16kHz）
        subprocess.run(["arecord", "-D", "plughw:2,0", "-f", "S16_LE",
                        "-r", "16000", "-c", "1", "-d", "5", "/tmp/input.wav"])
        # 2. ASR 转文字
        text = asr_transcribe("/tmp/input.wav")       # sherpa-onnx
        # 3. DeepSeek 对话（带记忆 + 工具）
        reply, tool_calls = chat_with_memory(text)
        # 4. 若有工具调用，执行动作
        for tc in tool_calls:
            run_tool(tc)                              # 发 socket 播放动作
        # 5. TTS 合成 + 播放
        await edge_tts.Communicate(reply, "zh-CN-XiaoxiaoNeural").save("/tmp/reply.mp3")
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "/tmp/reply.mp3"])
```

---

## 6. 落地步骤（分阶段）

### 阶段 A：验证音频链路（1 步，已具备）
- [x] 确认声卡 `card2` 麦克风+喇叭可用
- [ ] 实测 `arecord` 录音 + `aplay` 播放往返

### 阶段 B：DeepSeek 文本对话 + 记忆库（跑在控制盒）
1. 控制盒装 Python 依赖：`pip install openai edge-tts`
2. 写 `voice_agent.py`：DeepSeek 对话 + SQLite 记忆库（第 3、4 节）
3. 先把对话做成「文字进文字出」，在控制盒上 curl 自测

### 阶段 C：接入语音（ASR + TTS）
1. 装 `sherpa-onnx`（ASR）+ `edge-tts`（TTS）
2. 打通「录音→ASR→DeepSeek→TTS→播放」全链路
3. 加唤醒词（如「你好小图」）触发录音，避免一直录

### 阶段 D：function calling 控制身体
1. 给 DeepSeek 定义动作工具（第 3.3 节）
2. 工具执行走 `/tmp/motion_main_socket` 发 `:Play`
3. 实现「你说『跳个科目三』→ 机器人真的跳舞」

### 阶段 E：小米音箱接入（见第 7 节，二选一）

---

## 7. 小米音箱 / 小爱同学接入

### 7.1 边界说明（必须先讲清楚）

> **第三方硬件无法直接「变成小爱同学」**——「小爱同学」是小米封闭的语音助手品牌，不能装到别的机器人上。可行的接入方式只有两种：

### 方案一：让「小爱音箱控制机器人」（推荐，官方支持）

把 Figurobot 注册成**小米 IoT 平台**设备，之后用户对小爱音箱说「小爱同学，让机器人跳舞」，小爱解析语义后下发指令给机器人。

**路径**：
1. 注册小米企业开发者账号：https://iot.mi.com
2. 创建产品（选「智能机器人」或相近品类）
3. 选接入方式：
   - **云对云接入**（适合已有云能力的 Figurobot）：实现小米 IoT 的 OAuth + 设备控制 API
   - **本地直连接入**（小米模组/SDK）：需在控制盒集成小米 SDK
4. 定义设备功能（如「播放动作」能力，参数=动作名）
5. 认证 + 发布，用户在米家 App 绑定后即可用小爱语音控制

**关键点**：这一步主要是**厂商侧**动作（注册、认证），且「云对云」需要 Figurobot 有可公网访问的云端，否则走「本地直连」要在控制盒装小米 SDK（aarch64 兼容性需验证）。

### 方案二：机器人作为独立语音助手（DeepSeek 驱动，与小米无关）

这就是第 3-6 节做的整套东西。机器人有自己的唤醒词（如「你好小图」）、自己的大模型（DeepSeek）、自己的记忆。它和小爱音箱是**两个独立的设备**，可以共存但互不替代。

### 方案三（进阶，需要 Home Assistant 中转）

如果想「既用 DeepSeek 又让小米音箱参与」，可用 **Home Assistant + Xiaomi Miot Auto 插件**做中间层：小米音箱的指令 → HA → 机器人；机器人的状态 → HA → 米家。这属于进阶玩法，适合后续再上。

---

## 8. 风险与注意事项

| 风险 | 说明 | 应对 |
|---|---|---|
| **声卡争用** | 动作音乐 vs 语音回复共用 card2 | voice_agent 播放前停动作，或加互斥锁 |
| **DeepSeek 无语音** | 需自拼 ASR/TTS，链路长、延迟高 | 用流式 ASR + 短回复降低延迟 |
| **控制盒性能** | 本地 ASR（whisper/sherpa）吃 CPU，aarch64 可能吃力 | 首选轻量 sherpa-onnx；不行就上云端 ASR |
| **联网依赖** | DeepSeek + edge-tts 都需联网 | 接受联网（机器人本身有 WiFi 模块） |
| **记忆库隐私** | 对话内容存本地 SQLite | 全本地，不外传，反而更安全 |
| **小米接入门槛** | IoT 平台需企业开发者 + 产品认证 | 个人玩建议走方案二（独立助手）；要官方「小爱控制」再走方案一 |
| **fd 泄漏隐患** | motion_main 曾报 15 万次 `Too many open files` | 对话 agent 注意 socket 用完即关；监控该错误 |

---

## 9. 交付物清单

| 文件 | 说明 |
|---|---|
| `voice_agent.py` | 新增：控制盒端对话脑（ASR→DeepSeek→记忆→TTS→播放） |
| `memory_store.py` | 新增：SQLite 记忆库封装 |
| `robot_bridge.py` | 扩展：暴露 `/api/chat`、`/api/motion` 供 voice_agent 复用 |
| `figurobot-console.html` | 可选：加「语音对话」面板（显示对话流 + 记忆） |
| 本文件 | 技术方案，随代码同步 |

---

## 10. 下一步建议

按优先级：
1. **先做阶段 B**（DeepSeek 文本对话 + 记忆库）——价值最高、风险最低，可先在 PC 端跑通再搬控制盒
2. **再做阶段 C**（语音链路）——让机器人真正「开口说话」
3. **阶段 D**（function calling）——让对话能控制动作，形成「语音→动作」闭环
4. **阶段 E**（小米接入）——最后再考虑，且先明确你倾向「小爱控制机器人」还是「独立语音助手」
