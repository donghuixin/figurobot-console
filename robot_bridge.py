#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FiguRobot 机器人控制桥接服务
==============================
通过 ADB 连接 Figurobot 控制盒（Rockchip + Ubuntu），向网页控制台暴露 HTTP API。

用法:
    python robot_bridge.py [--port 8888] [--adb <adb路径>] [--serial <设备序列号>]

API:
    GET  /api/status        连接状态 + 设备信息
    GET  /api/motions       动作列表（98 个内置动作）
    POST /api/play          {"action": "你好"}  播放动作
    POST /api/idle          {"action": "..."}  播放待机动作
    POST /api/reset         关节复位 (Motion_Reset)
    POST /api/refresh       重新生成动作列表
    POST /api/joint/write   {"id":1,"position":2048} 实时关节控制（Dynamixel v2）
    POST /api/joint/read    {"id":1}                 读取关节当前位置
    POST /api/joint/enable  {"id":1,"enable":true}   扭矩使能
    POST /api/joint/scan    扫描在线关节
"""

import argparse
import base64
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 路径与设备 ----------
import os
HERE = os.path.dirname(os.path.abspath(__file__))


def _find_adb():
    """查找 adb：优先仓库内 tools/platform-tools/adb.exe，其次系统 PATH 里的 adb。"""
    local = os.path.join(HERE, "tools", "platform-tools", "adb.exe")
    if os.path.exists(local):
        return local
    import shutil
    found = shutil.which("adb")
    return found or "adb"


DEFAULT_ADB = _find_adb()

# ---------- 协议常量（Figurobot / Dynamixel Protocol 2.0 变体） ----------
FRAME_HEADER = [0xFF, 0x00, 0xFD, 0x00]
CMD = {
    "PING": 0x01, "READ": 0x02, "WRITE": 0x03,
    "SYNC_READ": 0x82, "SYNC_WRITE": 0x83,
}
BROADCAST_ID = 0xFE
ADDR = {
    "torque_enable": 0x40,
    "goal_position": 0x74,
    "present_position": 0x84,
    "present_input_voltage": 0x90,  # 0.1V 单位，2 字节
    "present_temperature": 0x92,     # 1 字节
    "moving": 0x7A,
}
POS_MAX = 4095          # 位置范围 0-4095
DEG_MAX = 360.0         # 对应 360 度
POSITION_LENGTH = 4     # 位置寄存器 4 字节（Dynamixel X 系列标准）
MIN_GOAL_TICK = 50      # 软件限位下界（约 4.39°）
MAX_GOAL_TICK = 3600    # 软件限位上界（约 316.48°）
SERIAL_BAUD = 1000000
# 官方 demo servo_figurobot_v2.py 的 _ERROR_NAMES 只定义 0x01–0x07（自定义枚举）
DXL_ERROR_NAMES = {
    0x00: "正常",
    0x01: "结果失败",
    0x02: "指令错误",
    0x03: "CRC 错误",
    0x04: "数据范围错误",
    0x05: "数据长度错误",
    0x06: "数据限制错误",
    0x07: "访问错误",
}
# 注意：0x10 官方未定义（实测 27 舵机会集体报，但能正常驱动，含义待官方确认），
# 不要臆断为「电压错误」。未收录的错误码统一显示「未定义(0xXX)」。
# 全部关节 ID（缺 18，来自 motor.json id_action_list）
SERVO_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
             19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]


def crc16(data):
    """CRC-16 IBM/ANSI (poly 0x8005, 初始值 0)。官方 demo 用 init=0，非 0xFFFF！"""
    crc = 0
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return [crc & 0xFF, (crc >> 8) & 0xFF]


# 字节填充（byte stuffing）：0xFF 0x00 0xFD → 0xFF 0x00 0xFD 0xFD
_MARKER = bytes([0xFF, 0x00, 0xFD])
_STUFFED = bytes([0xFF, 0x00, 0xFD, 0xFD])


def _apply_stuffing(data):
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i:i + 3] == _MARKER:
            result += _STUFFED
            i += 3
        else:
            result.append(data[i])
            i += 1
    return bytes(result)


def _remove_stuffing(data):
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i:i + 4] == _STUFFED:
            result += _MARKER
            i += 4
        else:
            result.append(data[i])
            i += 1
    return bytes(result)


def build_frame(dev_id, instruction, params):
    """按官方 _build_packet 实现：对 instruction+params 做 byte stuffing，CRC init=0。"""
    params = bytes(params)
    payload = bytes([instruction]) + params
    if _MARKER in params:
        stuffed = _apply_stuffing(payload)
    else:
        stuffed = payload
    length = len(stuffed) + 2  # +2 for CRC
    partial = bytearray(FRAME_HEADER)  # FF 00 FD 00
    partial.append(dev_id & 0xFF)
    partial.append(length & 0xFF)
    partial.append((length >> 8) & 0xFF)
    partial += stuffed
    partial += bytes(crc16(bytes(partial)))
    return list(partial)


def build_read(dev_id, addr, length):
    return build_frame(dev_id, CMD["READ"], [addr & 0xFF, (addr >> 8) & 0xFF,
                                             length & 0xFF, (length >> 8) & 0xFF])


def build_write(dev_id, addr, value_bytes):
    return build_frame(dev_id, CMD["WRITE"], [addr & 0xFF, (addr >> 8) & 0xFF] + list(value_bytes))


def build_sync_read(ids, addr, length):
    """SYNC_READ：广播读取多个设备的同一寄存器"""
    params = [addr & 0xFF, (addr >> 8) & 0xFF, length & 0xFF, (length >> 8) & 0xFF] + list(ids)
    return build_frame(BROADCAST_ID, CMD["SYNC_READ"], params)


def u32(v):
    """位置值编码为 4 字节小端（Dynamixel X 系列）"""
    v = int(v) & 0xFFFFFFFF
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


# ---------- ADB 封装 ----------
class Robot:
    def __init__(self, adb_path, serial=None):
        self.adb = adb_path
        self.serial = serial
        self._lock = threading.Lock()
        self._err_cache = {}      # {id: error_code}，PING 时更新，fast 读时复用
        self._err_cache_ts = 0.0  # 缓存时间戳（用于判断是否过期）

    def _cache_errs(self, err_by_id):
        """更新 error_code 缓存（full 读时调用）。"""
        self._err_cache = dict(err_by_id)
        self._err_cache_ts = time.time()

    def _cmd(self, *args, timeout=10):
        base = [self.adb]
        if self.serial:
            base += ["-s", self.serial]
        r = subprocess.run(base + list(args), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r

    def detect_device(self):
        """自动检测机器人设备（排除 emulator）"""
        if self.serial:
            return self.serial
        r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device" and not parts[0].startswith("emulator"):
                self.serial = parts[0]
                return parts[0]
        return None

    def shell(self, cmd, timeout=10):
        return self._cmd("shell", cmd, timeout=timeout)

    def is_online(self):
        if self.serial:
            r = self._cmd("shell", "echo ok", timeout=5)
            if "ok" in (r.stdout or ""):
                return True
            # 设备掉线，尝试重新检测
            self.serial = None
        return self.detect_device() is not None

    # ---- 高层动作控制（通过 motion_main 的 Unix socket） ----
    def _socket_send(self, payload):
        """通过 /tmp/motion_main_socket 发送命令，并读取 motion_main 的响应。

        base64 编码传代码（多行，逐语句换行），绕开 shell 引号问题。
        返回 subprocess 结果，附加属性 `.resp`（motion_main 返回的文本，无响应则为空）。
        """
        py = (
            "import socket,time\n"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            "s.settimeout(2)\n"
            "s.connect('/tmp/motion_main_socket')\n"
            f"s.sendall({payload!r}.encode('utf-8')+b'\\n')\n"
            "time.sleep(0.35)\n"
            "s.settimeout(0.8)\n"
            "try:\n"
            "    d=s.recv(65536)\n"
            "    print('RESP:'+d.decode('utf-8','replace'))\n"
            "except Exception:\n"
            "    print('RESP:')\n"
            "s.close()\n"
        )
        b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
        r = self.shell(f"echo {b64} | base64 -d | python3", timeout=10)
        resp = ""
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESP:"):
                resp = line[5:]
        r.resp = resp
        return r

    def play(self, action, frame_rate=30):
        """播放动作。motion_main 要求 `:Play <动作名> <帧率>`（3 段，缺帧率会被静默忽略）。"""
        cmd = f":Play {action} {int(frame_rate)}"
        r = self._socket_send(cmd)
        r.cmd = cmd
        return r

    def play_idle(self, action, frame_rate=30):
        """播放待机动作。格式同 :Play，需 3 段。"""
        cmd = f":PlayIdle {action} {int(frame_rate)}"
        r = self._socket_send(cmd)
        r.cmd = cmd
        return r

    def reset(self):
        cmd = ":Motion_Reset"
        r = self._socket_send(cmd)
        r.cmd = cmd
        return r

    # ---- 语音助手（DeepSeek 对话，跑在控制盒 voice_agent） ----
    def chat(self, text):
        """调用控制盒 voice_agent 的单次对话，返回 {reply, tools}。

        对话大脑（DeepSeek + 本地记忆库）跑在控制盒 /userdata/voice_agent/voice_agent.py。
        文本用 base64 编码后作为参数传递，绕开 shell 引号/中文问题。
        """
        text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        py = (
            "import base64,subprocess,sys;"
            f"text=base64.b64decode('{text_b64}').decode('utf-8');"
            "r=subprocess.run(['python3','/userdata/voice_agent/voice_agent.py',"
            "'--once',text],capture_output=True,text=True,encoding='utf-8',"
            "errors='replace',timeout=90);"
            "sys.stdout.write(r.stdout);sys.stderr.write(r.stderr)"
        )
        b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
        cmd = f"echo {b64} | base64 -d | python3"
        r = self.shell(cmd, timeout=100)
        out = (r.stdout or "").strip()
        try:
            data = json.loads(out)
            if isinstance(data, dict) and "reply" in data:
                return data
        except Exception:
            pass
        return {"reply": "", "tools": [], "raw": out[:500]}

    def list_motions(self):
        r = self.shell("cat /sdcard/.config/Figurobot/data/motionList.json", timeout=8)
        try:
            return json.loads(r.stdout)
        except Exception:
            return None

    def refresh_motions(self):
        self._socket_send(":ListMotions")

    # ---- 待机（自动 idle 动作）开关 ----
    MOTION_MAIN_CONFIG = "/userdata/.figurobot/motion_main/config.json"
    IDLE_NORMAL = 5        # 正常待机：5 秒无命令自动播随机 Idle 动作
    IDLE_QUIET = 86400     # 安静待机：24 小时（基本不再自动乱动）

    def _exec_python(self, py):
        """在设备上用 python3 执行一段代码（base64 传参，绕开 shell 引号问题）。"""
        b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
        return self.shell(f"echo {b64} | base64 -d | python3", timeout=15)

    def get_idle_interval(self):
        """读取 motion_main 当前的 idle_interval。返回 int，失败返回 None。"""
        py = (
            "import json;"
            f"c=json.load(open('{self.MOTION_MAIN_CONFIG}'));"
            "print(c.get('idle_interval'))"
        )
        r = self._exec_python(py)
        try:
            return int((r.stdout or "").strip())
        except Exception:
            return None

    def set_idle_interval(self, seconds):
        """修改 motion_main 的 idle_interval 并重启服务。返回 (ok, output)。"""
        py = (
            "import json;"
            f"p='{self.MOTION_MAIN_CONFIG}';"
            "c=json.load(open(p));"
            f"c['idle_interval']={int(seconds)};"
            "json.dump(c,open(p,'w'),indent=4,ensure_ascii=False);"
            "print('set idle_interval=%d' % c['idle_interval'])"
        )
        r = self._exec_python(py)
        out = (r.stdout or "").strip()
        # 重启 motion_main 使配置生效（systemd user 服务，需 lckfb 身份）
        restart = self.shell(
            "su - lckfb -c 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart motion_main'",
            timeout=20)
        return ("set idle_interval" in out), out + " | restart rc=%s" % restart.returncode

    # ---- 底层关节控制（Dynamixel v2，通过串口，纯 Python 实现，不依赖 pyserial） ----
    _SERIAL_EXEC_REMOTE = "/tmp/figurobot_serial_exec.py"

    def _find_serial_exec_local(self):
        """优先同目录 serial_exec.py，其次 .workbuddy/serial_exec.py。"""
        here = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(here, "serial_exec.py"),
                  os.path.join(here, ".workbuddy", "serial_exec.py")):
            if os.path.exists(p):
                return p
        return None

    def _ensure_serial_exec_uploaded(self):
        """把 serial_exec.py 推到设备 /tmp，避免 python3 -c 的 shell 引号转义问题。"""
        if getattr(self, "_serial_exec_uploaded", False):
            return
        local = self._find_serial_exec_local()
        if not local:
            raise RuntimeError("missing serial_exec.py (look in repo root or .workbuddy/)")
        subprocess.run(
            [self.adb, "push", local, self._SERIAL_EXEC_REMOTE],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self._serial_exec_uploaded = True

    def _serial_exec(self, hex_frames, read_timeout=0.4):
        """在设备上跑串口执行脚本，传 frames（hex 列表）+ read_timeout。

        用 base64 编码 + stdin pipe 传 Python 代码，绕开 shell 单引号转义问题。
        """
        self._ensure_serial_exec_uploaded()
        env_json = json.dumps(list(hex_frames))
        py = (
            "import os;"
            f"os.environ['SERIAL_HEX_FRAMES']={env_json!r};"
            f"os.environ['SERIAL_READ_TIMEOUT']={str(read_timeout)!r};"
            f"exec(open('{self._SERIAL_EXEC_REMOTE}').read())"
        )
        b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
        cmd = f"echo {b64} | base64 -d | python3"
        r = self.shell(cmd, timeout=20)
        return r.stdout or ""

    def joint_write(self, dev_id, position):
        frame = build_write(dev_id, ADDR["goal_position"], u32(position))
        hx = bytes(frame).hex()
        out = self._serial_exec([hx])
        return self._parse_hex(out)

    def joint_read(self, dev_id):
        frame = build_read(dev_id, ADDR["present_position"], POSITION_LENGTH)
        hx = bytes(frame).hex()
        out = self._serial_exec([hx])
        return self._parse_hex(out)

    def joint_enable(self, dev_id, enable):
        frame = build_write(dev_id, ADDR["torque_enable"], [1 if enable else 0])
        hx = bytes(frame).hex()
        out = self._serial_exec([hx])
        return self._parse_hex(out)

    def joint_scan(self, id_start=1, id_end=32):
        frames = []
        for i in range(id_start, id_end + 1):
            frames.append(bytes(build_frame(i, CMD["PING"], [])).hex())
        out = self._serial_exec(frames, read_timeout=0.6)
        hexstr = self._parse_hex(out)
        if isinstance(hexstr, dict):
            return {"error": hexstr.get("error", "serial error"), "raw": "",
                    "sent_frames": frames}
        return {"raw": hexstr, "sent_frames": frames}

    # ---- 姿态读取 + 上电安全判断 ----
    @staticmethod
    def _parse_frames(hexstr):
        """解析串口读回的字节流，返回 [(id, error, data_bytes)]（data_bytes 已去填充）。"""
        try:
            buf = bytes.fromhex(hexstr)
        except Exception:
            return []
        frames = []
        i, n = 0, len(buf)
        while i <= n - 9:
            if buf[i:i + 4] == b'\xff\x00\xfd\x00':
                did = buf[i + 4]
                ln = buf[i + 5] | (buf[i + 6] << 8)
                total = 7 + ln
                if i + total > n:
                    break
                inst = buf[i + 7]
                if inst == 0x55:  # STATUS
                    err = buf[i + 8]
                    params_raw = buf[i + 9:i + total - 2]
                    params = _remove_stuffing(params_raw)
                    frames.append((did, err, params))
                i += total
            else:
                i += 1
        return frames

    def read_pose(self, fast=False):
        """读取全部关节当前位置。

        策略（fast=False，完整模式，约 2-3s）：
          1. PING 所有 ID → 拿 online + error_code（更新缓存）。
          2. SYNC_READ 一次广播读所有在线 ID 的 present_position（快路径）。
          3. 对 SYNC_READ 没返回的在线 ID 逐个 READ 回退（兜底）。

        策略（fast=True，实时轮询模式，约 0.7-0.9s）：
          跳过 PING，直接 SYNC_READ 全部 ID 的位置；error_code 复用上次 PING 缓存。
        返回 {joints: {id: {raw, angle, online, error_code, error_text}}}
        """
        pos_by_id = {}
        read_count = 0
        read_sample = ""
        read_response_hex = ""
        ping_count = 0
        ping_sample = ""
        ping_response_hex = ""
        err_by_id = {}

        # fast 模式但缓存为空时，降级为完整读（保证首次能拿到 error_code 和在线列表）
        if fast and not self._err_cache:
            fast = False

        # 第一步（完整模式）：逐个 PING 拿在线状态和错误码
        online_ids = list(SERVO_IDS)
        if not fast:
            ping_frames = [bytes(build_frame(i, CMD["PING"], [])).hex() for i in SERVO_IDS]
            ping_count = len(ping_frames)
            ping_sample = ping_frames[0] if ping_frames else ""
            out = self._serial_exec(ping_frames, read_timeout=0.4)
            hexstr = self._parse_hex(out)
            if isinstance(hexstr, dict):
                return {"error": hexstr.get("error", "serial error"), "joints": {},
                        "_debug": {"ping_count": ping_count, "ping_sample": ping_sample,
                                   "ping_response_hex": "", "read_count": 0,
                                   "read_sample": "", "read_addr": "0x84 (Present Position, 4 bytes)",
                                   "read_response_hex": ""}}
            ping_response_hex = hexstr if isinstance(hexstr, str) else ""
            ping_frames_parsed = self._parse_frames(hexstr)
            online_ids = []
            for did, err, _ in ping_frames_parsed:
                online_ids.append(did)
                err_by_id[did] = err
            self._cache_errs(err_by_id)
        else:
            # fast 模式：复用缓存，在线集合 = 上次 PING 在线的 ID
            err_by_id = dict(self._err_cache)
            online_ids = list(self._err_cache.keys())

        # 第二步：SYNC_READ 广播读所有在线 ID 的位置（1 帧完成）
        sync_ids = sorted(set(online_ids) & set(SERVO_IDS))
        if sync_ids:
            sync_frame = bytes(build_sync_read(sync_ids, ADDR["present_position"], POSITION_LENGTH)).hex()
            read_sample = sync_frame
            read_count += 1
            out2 = self._serial_exec([sync_frame], read_timeout=0.25)
            hexstr2 = self._parse_hex(out2)
            if not isinstance(hexstr2, dict):
                read_response_hex = hexstr2 if isinstance(hexstr2, str) else ""
                for did, err, data in self._parse_frames(hexstr2):
                    if len(data) >= POSITION_LENGTH:
                        raw = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)
                        pos_by_id[did] = raw

        # 第三步：对 SYNC_READ 没返回的在线 ID 逐个 READ 回退（兜底）
        missed = [sid for sid in online_ids if sid not in pos_by_id]
        if missed:
            fallback_frames = [bytes(build_read(sid, ADDR["present_position"], POSITION_LENGTH)).hex()
                               for sid in missed]
            read_count += len(fallback_frames)
            out3 = self._serial_exec(fallback_frames, read_timeout=0.25)
            hexstr3 = self._parse_hex(out3)
            if not isinstance(hexstr3, dict):
                read_response_hex += hexstr3 if isinstance(hexstr3, str) else ""
                for did, err, data in self._parse_frames(hexstr3):
                    if len(data) >= POSITION_LENGTH:
                        raw = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)
                        pos_by_id[did] = raw

        # 组装结果
        result = {"joints": {}}
        # 附加「发出的指令」和「机器反馈」用于前端日志打印
        result["_debug"] = {
            "ping_count": ping_count,
            "ping_sample": ping_sample,
            "ping_response_hex": ping_response_hex,
            "read_count": read_count,
            "read_sample": read_sample,
            "read_addr": "0x84 (Present Position, 4 bytes)",
            "read_response_hex": read_response_hex,
            "sync_read_ok": len(pos_by_id),
            "fast": fast,
        }
        for sid in SERVO_IDS:
            if sid in online_ids:
                err = err_by_id.get(sid, 0)
                if sid in pos_by_id:
                    raw = pos_by_id[sid]
                    result["joints"][str(sid)] = {
                        "raw": raw,
                        "angle": round(raw * DEG_MAX / POS_MAX, 2),
                        "online": True,
                        "error_code": err,
                        "error_text": DXL_ERROR_NAMES.get(err, f"未知(0x{err:02X})"),
                    }
                else:
                    # 在线但读不到 position（error 状态拒读）
                    result["joints"][str(sid)] = {
                        "raw": None, "angle": None, "online": True,
                        "error_code": err,
                        "error_text": DXL_ERROR_NAMES.get(err, f"未知(0x{err:02X})"),
                    }
            else:
                result["joints"][str(sid)] = {
                    "raw": None, "angle": None, "online": False,
                    "error_code": None, "error_text": "无响应",
                }
        return result

    def read_zero_values(self):
        """读取 zero.ini 的零点偏移，返回 {servoId: zeroValue}。

        zero.ini 格式（INI）：
            [Steerings]
            1\\id=1
            1\\zeroValue=1016
            ...
        """
        r = self.shell("cat /userdata/data/zero.ini", timeout=8)
        idx_id = {}
        idx_zero = {}
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip().strip("\\")
            try:
                if key.endswith("id"):
                    idx_id[key[:-2].strip()] = int(val.strip())
                elif key.endswith("zeroValue"):
                    idx_zero[key[:-9].strip()] = int(val.strip())
            except ValueError:
                continue
        result = {}
        for idx, mid in idx_id.items():
            if idx in idx_zero:
                result[str(mid)] = idx_zero[idx]
        return result

    @staticmethod
    def assess_safety(pose):
        """判断上电是否会损伤机器人。

        返回 {level, level_text, issues, online_count, total, err_joints, ...}。
        level: safe / warn / danger / unknown
        判断标准（对齐软件限位 [MIN_GOAL_TICK, MAX_GOAL_TICK]）：
          - 所有关节在线、err==0 且角度在 [4.39°, 316.48°] 内 → safe
          - 有离线关节（读不到）→ warn
          - 有角度超出限位 → danger（该关节处于极限位，上电可能损伤）
          - 在线但报错误码（含 0x10）→ warn（错误码官方未定义，含义待确认）
        """
        joints = pose.get("joints", {})
        total = len(joints)
        online = [j for j in joints.values() if j.get("online")]
        offline = [k for k, j in joints.items() if not j.get("online")]
        # 在线但报错误码的关节（error_code 非 0）
        err_joints = [k for k, j in joints.items()
                      if j.get("online") and j.get("error_code") not in (None, 0)]
        min_deg = MIN_GOAL_TICK * DEG_MAX / POS_MAX   # ≈ 4.39°
        max_deg = MAX_GOAL_TICK * DEG_MAX / POS_MAX   # ≈ 316.48°
        out_of_range = []
        for k, j in joints.items():
            if j.get("online") and j.get("error_code") in (None, 0) and j.get("angle") is not None:
                a = j["angle"]
                if a < min_deg or a > max_deg:
                    out_of_range.append((k, a))
        issues = []
        if err_joints:
            # 统计错误码分布
            err_codes = {}
            for k in err_joints:
                c = joints[k].get("error_code")
                err_codes[c] = err_codes.get(c, 0) + 1
            desc = "、".join(f"{n} 个报 0x{c:02X}" for c, n in sorted(err_codes.items()))
            issues.append(f"有 {len(err_joints)} 个关节报告错误码（{desc}）。官方文档未定义这些错误码，含义待官方确认，不影响驱动")
        if out_of_range:
            issues.append("有 %d 个关节角度超出安全范围（限位附近）" % len(out_of_range))
        if offline:
            issues.append("有 %d 个关节离线/未响应（该 ID 未接电机或总线异常）" % len(offline))
        if total == 0:
            return {"level": "unknown", "level_text": "无法读取", "issues": ["未能读取任何关节"],
                    "online_count": 0, "total": 0, "out_of_range": out_of_range,
                    "offline": offline, "err_joints": err_joints}
        if out_of_range:
            return {"level": "danger", "level_text": "危险：建议先复位再上电", "issues": issues,
                    "online_count": len(online), "total": total,
                    "out_of_range": out_of_range, "offline": offline, "err_joints": err_joints}
        if offline or err_joints:
            return {"level": "warn", "level_text": "警告：部分关节未响应或报错误码", "issues": issues,
                    "online_count": len(online), "total": total,
                    "out_of_range": [], "offline": offline, "err_joints": err_joints}
        return {"level": "safe", "level_text": "安全：可以上电", "issues": [],
                "online_count": len(online), "total": total,
                "out_of_range": [], "offline": [], "err_joints": []}

    @staticmethod
    def _parse_hex(stdout):
        for line in (stdout or "").splitlines():
            if line.startswith("HEX:"):
                return line[4:].strip()
            if line.startswith("SERIAL_ERROR:"):
                return {"error": line[13:].strip()}
        return ""


# ---------- HTTP 服务 ----------
class Handler(BaseHTTPRequestHandler):
    robot = None  # 注入

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self._json({})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            online = self.robot.is_online()
            self._json({"connected": online, "serial": self.robot.serial,
                        "protocol": "Figurobot v2 (Dynamixel)", "baud": SERIAL_BAUD})
        elif path == "/api/motions":
            motions = self.robot.list_motions()
            if motions is None:
                self._json({"error": "无法读取动作列表"}, 500)
            else:
                self._json({"count": len(motions), "motions": motions})
        elif path == "/api/standby":
            interval = self.robot.get_idle_interval()
            quiet = interval is not None and interval >= 60
            self._json({"ok": True, "idle_interval": interval, "quiet": quiet,
                        "normal_interval": self.robot.IDLE_NORMAL,
                        "quiet_interval": self.robot.IDLE_QUIET})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        if path == "/api/play":
            action = (body.get("action") or "").strip()
            if not action:
                return self._json({"error": "缺少 action"}, 400)
            frame_rate = body.get("frame_rate", 30)
            r = self.robot.play(action, frame_rate=frame_rate)
            self._json({"ok": True, "action": action, "frame_rate": frame_rate,
                        "command": getattr(r, "cmd", ""),
                        "response": getattr(r, "resp", ""),
                        "output": (r.stdout or "")[:300]})
        elif path == "/api/idle":
            action = (body.get("action") or "").strip()
            frame_rate = body.get("frame_rate", 30)
            r = self.robot.play_idle(action, frame_rate=frame_rate)
            self._json({"ok": True, "action": action, "frame_rate": frame_rate,
                        "command": getattr(r, "cmd", ""),
                        "response": getattr(r, "resp", "")})
        elif path == "/api/reset":
            r = self.robot.reset()
            self._json({"ok": True, "command": getattr(r, "cmd", ""),
                        "response": getattr(r, "resp", ""),
                        "output": (r.stdout or "")[:200]})
        elif path == "/api/refresh":
            self.robot.refresh_motions()
            self._json({"ok": True, "note": "动作列表已刷新"})
        elif path == "/api/joint/write":
            dev_id = int(body.get("id", 0))
            pos = int(body.get("position", 0))
            res = self.robot.joint_write(dev_id, pos)
            self._json({"ok": True, "id": dev_id, "position": pos, "raw": res})
        elif path == "/api/joint/read":
            dev_id = int(body.get("id", 0))
            res = self.robot.joint_read(dev_id)
            self._json({"ok": True, "id": dev_id, "raw": res})
        elif path == "/api/joint/enable":
            dev_id = int(body.get("id", 0))
            enable = bool(body.get("enable", True))
            res = self.robot.joint_enable(dev_id, enable)
            self._json({"ok": True, "id": dev_id, "enable": enable, "raw": res})
        elif path == "/api/joint/scan":
            res = self.robot.joint_scan(int(body.get("start", 1)), int(body.get("end", 32)))
            if isinstance(res, dict) and "error" in res:
                self._json({"ok": False, "error": res["error"], "raw": res.get("raw", ""),
                            "sent_frames": res.get("sent_frames", [])})
            else:
                self._json({"ok": True, "raw": res["raw"], "sent_frames": res.get("sent_frames", [])})
        elif path == "/api/pose":
            fast = bool(body.get("fast", False))
            pose = self.robot.read_pose(fast=fast)
            dbg = (pose.pop("_debug", {}) if isinstance(pose, dict) else {})
            if "error" in pose and not pose.get("joints"):
                self._json({"error": pose["error"], "joints": {}, "safety": None,
                            "debug": dbg}, 503)
            else:
                safety = self.robot.assess_safety(pose)
                zero_values = self.robot.read_zero_values()
                self._json({"ok": True, "joints": pose["joints"], "safety": safety,
                            "zero_values": zero_values,
                            "debug": dbg,
                            "limits": {"min_angle": round(MIN_GOAL_TICK * DEG_MAX / POS_MAX, 2),
                                       "max_angle": round(MAX_GOAL_TICK * DEG_MAX / POS_MAX, 2)}})
        elif path == "/api/standby":
            quiet = bool(body.get("quiet", True))
            target = self.robot.IDLE_QUIET if quiet else self.robot.IDLE_NORMAL
            ok, out = self.robot.set_idle_interval(target)
            cmd_desc = ("修改 config.json idle_interval=%d → systemctl --user restart motion_main"
                        % target)
            self._json({"ok": ok, "quiet": quiet, "idle_interval": target, "output": out,
                        "command": cmd_desc})
        elif path == "/api/chat":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "缺少 text"}, 400)
            result = self.robot.chat(text)
            self._json({"ok": True, "text": text, "reply": result.get("reply", ""),
                        "tools": result.get("tools", []),
                        "tts_error": result.get("tts_error", "")})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=None, help="设备序列号，留空自动检测")
    args = ap.parse_args()

    if not os.path.exists(args.adb):
        print(f"[错误] 未找到 adb: {args.adb}")
        print("请先安装 Android platform-tools，或用 --adb 指定路径")
        sys.exit(1)

    robot = Robot(args.adb, args.serial)
    serial = robot.detect_device()
    if not serial:
        print("[警告] 未检测到机器人设备（请确认 USB 已连接且识别为 ADB 设备）")
    else:
        print(f"[信息] 检测到机器人设备: {serial}")

    Handler.robot = robot
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[信息] 桥接服务已启动: http://127.0.0.1:{args.port}")
    print(f"[信息] 网页控制台请打开 figurobot-console.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[信息] 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
