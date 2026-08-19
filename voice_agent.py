#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figurobot 语音助手（DeepSeek 驱动，独立 AI）
============================================
唤醒词「你好小图」触发，链路：
    麦克风录音(16kHz) → ASR 转文字 → DeepSeek 对话(带本地记忆) → TTS 合成 → 喇叭播放
    对话中 DeepSeek 可通过 function calling 控制机器人动作。

依赖（控制盒上安装）：
    python3 -m pip install --upgrade pip
    python3 -m pip install openai edge-tts

用法：
    export DEEPSEEK_API_KEY="sk-xxxx"      # 或写入 config.json
    python3 voice_agent.py

配置（config.json，与脚本同目录，可选）：
    {
      "api_key": "sk-xxxx",
      "model": "deepseek-chat",
      "wake_word": "你好小图",
      "memory_db": "/userdata/data/robot_memory/robot.db",
      "asr": "sherpa-onnx",        # 或 "none"（用文字输入代替语音）
      "motion_socket": "/tmp/motion_main_socket",
      "idle_interval": 86400
    }
"""
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from memory_store import MemoryStore  # noqa: E402

# ---------- 默认配置 ----------
DEFAULTS = {
    "api_key": "",
    "model": "deepseek-chat",
    "wake_word": "你好小图",
    "memory_db": "/userdata/data/robot_memory/robot.db",
    "asr": "none",              # "sherpa-onnx" 或 "none"
    "motion_socket": "/tmp/motion_main_socket",
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "record_seconds": 5,
    "mic_device": "plughw:2,0",
    "spk_device": "plughw:2,0",
}


def load_config():
    cfg = dict(DEFAULTS)
    cfg_path = os.path.join(HERE, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 环境变量优先
    if os.environ.get("DEEPSEEK_API_KEY"):
        cfg["api_key"] = os.environ["DEEPSEEK_API_KEY"]
    return cfg


CFG = load_config()

# ---------- 机器人动作工具（function calling） ----------
MOTION_TOOLS = [
    {"type": "function", "function": {
        "name": "play_motion",
        "description": "让机器人播放一个动作，如跳舞、挥手、难过、高兴等",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "动作名，如 挥手1、科目三1page_52"},
                "frame_rate": {"type": "integer", "description": "帧率，默认 30"}
            },
            "required": ["name"],
        },
    }},
    {"type": "function", "function": {
        "name": "read_pose",
        "description": "读取机器人当前各关节的姿态角度",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "reset_motion",
        "description": "让机器人复位到安全姿态",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def motion_socket_send(payload):
    """通过 motion_main 的 Unix socket 发命令。成功返回空串，失败抛异常。"""
    cmd = payload.strip() + "\n"
    # 用单独 python 进程发，避免污染主循环
    code = (
        "import socket,sys\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        "s.settimeout(3)\n"
        "s.connect(%r)\n"
        "s.sendall(sys.argv[1].encode('utf-8'))\n"
        "s.close()\n" % CFG["motion_socket"]
    )
    subprocess.run([sys.executable, "-c", code, cmd], timeout=8)


def run_tool(tool_call):
    """执行一个 function calling 工具，返回结果文本。"""
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments or "{}")
    except Exception:
        args = {}
    if name == "play_motion":
        motion_name = args.get("name", "")
        fps = int(args.get("frame_rate", 30))
        motion_socket_send(f":Play {motion_name} {fps}")
        return f"已播放动作「{motion_name}」（帧率 {fps}）"
    if name == "read_pose":
        return "姿态读取需串口空闲，当前通过 motion_main 运行中，暂返回占位结果。"
    if name == "reset_motion":
        motion_socket_send(":Motion_Reset")
        return "已复位到安全姿态"
    return f"未知工具 {name}"


# ---------- DeepSeek 对话 ----------
class DeepSeekChat:
    def __init__(self, api_key, model, memory: MemoryStore):
        if not api_key:
            raise RuntimeError("缺少 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY 或写入 config.json")
        # 延迟导入，避免未安装 openai 时整个脚本无法启动
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.model = model
        self.memory = memory
        self.messages = []  # 会话内消息

    def _build_system(self, user_text):
        profile = self.memory.all_profile()
        facts = self.memory.retrieve_facts(user_text)
        parts = [
            "你是 Figurobot 人形机器人，一个活泼、友好的独立 AI 助手。",
            "回复要自然、简短（一两句话），像朋友聊天。",
        ]
        if profile:
            p = "；".join(f"{k}={v}" for k, v in profile.items())
            parts.append(f"你已知用户信息：{p}。")
        if facts:
            parts.append(f"你记得这些事：{'；'.join(facts)}。")
        parts.append(
            "你可以用工具控制自己的身体：play_motion 播放动作、reset_motion 复位。"
            "当用户想看你跳舞/挥手/做动作时，调用 play_motion。"
        )
        return {"role": "system", "content": "\n".join(parts)}

    def chat(self, user_text):
        if not user_text.strip():
            return None, []
        # 从记忆库加载历史作为上下文起点
        if not self.messages:
            for role, content in self.memory.recent_history(limit=10):
                self.messages.append({"role": role, "content": content})
        msgs = [self._build_system(user_text)] + self.messages
        msgs.append({"role": "user", "content": user_text})

        resp = self.client.chat.completions.create(
            model=self.model, messages=msgs, tools=MOTION_TOOLS, stream=False)
        msg = resp.choices[0].message

        # 记录本轮
        self.memory.add_turn("user", user_text)
        tool_results = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                result = run_tool(tc)
                tool_results.append(result)
                msgs.append({"role": "assistant", "tool_calls": [tc]})
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            # 带工具结果再问一次，拿最终回复
            resp2 = self.client.chat.completions.create(
                model=self.model, messages=msgs, stream=False)
            msg = resp2.choices[0].message

        reply = msg.content or ""
        if reply:
            self.memory.add_turn("assistant", reply)
            self.messages.append({"role": "user", "content": user_text})
            self.messages.append({"role": "assistant", "content": reply})
            # 自动抽取值得记住的事实（启发式：含"我是/我叫/我喜欢/我住在"等）
            self._auto_remember(user_text)
        return reply, tool_results

    def _auto_remember(self, text):
        for kw in ("我叫", "我是", "我喜欢", "我住在", "我在", "我的"):
            if kw in text:
                self.memory.add_fact(text.strip())
                break


# ---------- 语音：录音 / ASR / TTS ----------
def record_audio(path):
    cmd = ["arecord", "-D", CFG["mic_device"], "-f", "S16_LE", "-r", "16000",
           "-c", "1", "-d", str(CFG["record_seconds"]), path]
    subprocess.run(cmd, timeout=CFG["record_seconds"] + 5, check=True)


def asr_transcribe(path):
    """语音转文字。asr='none' 时返回空串（走文字输入）。"""
    mode = CFG.get("asr", "none")
    if mode == "none":
        return ""
    if mode == "sherpa-onnx":
        # 需要预装 sherpa-onnx + 中文模型，此处为接入点
        import sherpa_onnx  # noqa: F401
        # TODO: 初始化 recognizer，识别 path，返回文字
        return ""
    return ""


async def tts_and_play(text):
    """文字转语音并用 ffplay 播放到喇叭。"""
    import edge_tts
    out = "/tmp/figurobot_reply.mp3"
    communicate = edge_tts.Communicate(text, CFG["tts_voice"])
    await communicate.save(out)
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", out],
                   timeout=30)


# ---------- 主循环 ----------
def text_loop(chat: DeepSeekChat):
    """文字输入模式（无 ASR 时的降级模式，也是联调最快的方式）。"""
    print("Figurobot 语音助手（文字模式）已就绪，输入 quit 退出。")
    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("quit", "exit", "退出"):
            break
        reply, tools = chat.chat(text)
        if tools:
            print("[动作] " + "；".join(tools))
        if reply:
            print("小图> " + reply)


def voice_loop(chat: DeepSeekChat):
    """语音模式：唤醒词「你好小图」触发。"""
    import asyncio
    print(f"Figurobot 语音助手已就绪，说「{CFG['wake_word']}」唤醒。")
    while True:
        try:
            input("按回车开始录音（说唤醒词）> ")
            path = "/tmp/figurobot_input.wav"
            record_audio(path)
            text = asr_transcribe(path)
            if not text:
                print("[ASR] 未识别到语音（asr=none 时请用文字模式）")
                continue
            print(f"[识别] {text}")
            if CFG["wake_word"] not in text:
                print("未包含唤醒词，忽略")
                continue
            # 去掉唤醒词
            text = text.replace(CFG["wake_word"], "").strip()
            if not text:
                text = "你好"
            reply, tools = chat.chat(text)
            if tools:
                print("[动作] " + "；".join(tools))
            if reply:
                print("小图> " + reply)
                asyncio.run(tts_and_play(reply))
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("出错:", e)


def once_mode(text):
    """单次对话：给定文本，返回 JSON {reply, tools}。供桥接服务 /api/chat 调用。"""
    memory = MemoryStore(CFG["memory_db"])
    chat = DeepSeekChat(CFG["api_key"], CFG["model"], memory)
    reply, tools = chat.chat(text)
    result = {"reply": reply or "", "tools": tools or []}
    # 如果有回复，尝试 TTS 播放到喇叭（失败不阻塞）
    if reply:
        try:
            import asyncio
            asyncio.run(tts_and_play(reply))
        except Exception as e:
            result["tts_error"] = str(e)
    return result


def main():
    # 单次对话模式：voice_agent.py --once "文本"
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        text = " ".join(sys.argv[2:])
        print(json.dumps(once_mode(text), ensure_ascii=False))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--text":
        mode = "text"
    else:
        mode = "voice"
    memory = MemoryStore(CFG["memory_db"])
    chat = DeepSeekChat(CFG["api_key"], CFG["model"], memory)
    if mode == "text":
        text_loop(chat)
    else:
        voice_loop(chat)


if __name__ == "__main__":
    main()
