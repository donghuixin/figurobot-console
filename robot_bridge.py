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
# Dynamixel 错误码 → 可读描述（来自官方协议手册 v2）
DXL_ERROR_NAMES = {
    0x00: "正常",
    0x01: "结果失败",
    0x02: "指令错误",
    0x03: "CRC 错误",
    0x04: "数据范围错误",
    0x05: "数据长度错误",
    0x06: "数据限制错误",
    0x07: "访问错误",
    0x08: "告警动作",
    0x10: "输入电压错误",   # ← 本次诊断的关键
    0x20: "过温",
    0x40: "过载",
    0x80: "超时",
}
# 全部关节 ID（缺 18，来自 motor.json id_action_list）
SERVO_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
             19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return [crc & 0xFF, (crc >> 8) & 0xFF]


def build_frame(dev_id, instruction, params):
    length = len(params) + 3
    body = list(FRAME_HEADER) + [dev_id & 0xFF, length & 0xFF, (length >> 8) & 0xFF,
                                 instruction & 0xFF] + list(params)
    return body + crc16(body)


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
        """通过 /tmp/motion_main_socket 发送命令。base64 编码传代码，绕开 shell 引号问题。"""
        py = (
            "import socket;"
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);"
            "s.settimeout(2);"
            "s.connect('/tmp/motion_main_socket');"
            f"s.sendall({payload!r}.encode('utf-8')+b'\\n');"
            "s.close()"
        )
        b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
        return self.shell(f"echo {b64} | base64 -d | python3", timeout=8)

    def play(self, action):
        return self._socket_send(f":Play {action}")

    def play_idle(self, action):
        return self._socket_send(f":PlayIdle {action}")

    def reset(self):
        return self._socket_send("Motion_Reset")

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
        return self._parse_hex(out)

    # ---- 姿态读取 + 上电安全判断 ----
    @staticmethod
    def _parse_frames(hexstr):
        """解析串口读回的字节流，返回 [(id, error, data_bytes)]"""
        try:
            buf = bytes.fromhex(hexstr)
        except Exception:
            return []
        frames = []
        i, n = 0, len(buf)
        while i <= n - 9:
            if buf[i] == 0xFF and buf[i + 1] == 0x00 and buf[i + 2] == 0xFD and buf[i + 3] == 0x00:
                did = buf[i + 4]
                ln = buf[i + 5] | (buf[i + 6] << 8)
                total = 7 + ln
                if i + total > n:
                    break
                inst = buf[i + 7]
                params = buf[i + 8:i + total - 2]
                if inst == 0x55:  # STATUS
                    err = params[0] if params else 0
                    frames.append((did, err, params[1:]))
                i += total
            else:
                i += 1
        return frames

    def read_pose(self):
        """读取全部关节当前位置。

        策略：先 PING 所有 ID 拿 online + error_code，对在线 ID 逐个 READ Present Position。
        （SYNC_READ 在 voltage error 等异常状态下不响应，必须用 PING+READ 组合。）
        返回 {joints: {id: {raw, angle, online, error_code, error_text}}}
        """
        # 第一步：逐个 PING 拿在线状态和错误码
        ping_frames = [bytes(build_frame(i, CMD["PING"], [])).hex() for i in SERVO_IDS]
        out = self._serial_exec(ping_frames, read_timeout=0.4)
        hexstr = self._parse_hex(out)
        if isinstance(hexstr, dict):
            return {"error": hexstr.get("error", "serial error"), "joints": {}}
        ping_frames_parsed = self._parse_frames(hexstr)
        online_ids = []
        err_by_id = {}
        for did, err, _ in ping_frames_parsed:
            online_ids.append(did)
            err_by_id[did] = err

        # 第二步：逐个 READ Present Position（仅对在线的）
        read_frames = []
        id_order = []
        for sid in SERVO_IDS:
            if sid in online_ids:
                read_frames.append(bytes(build_read(sid, ADDR["present_position"], POSITION_LENGTH)).hex())
                id_order.append(sid)
        # 一些舵机在 error 状态时不会响应 READ，单独处理：尝试读，失败就保留 error 信息
        pos_by_id = {}
        if read_frames:
            out = self._serial_exec(read_frames, read_timeout=0.4)
            hexstr2 = self._parse_hex(out)
            if not isinstance(hexstr2, dict):
                for did, err, data in self._parse_frames(hexstr2):
                    if len(data) >= POSITION_LENGTH:
                        raw = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)
                        pos_by_id[did] = raw

        # 组装结果
        result = {"joints": {}}
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

        返回 {level, level_text, issues, online_count, total, voltage_err_count, ...}。
        level: safe / warn / danger / unknown / power
        判断标准（对齐软件限位 [MIN_GOAL_TICK, MAX_GOAL_TICK]）：
          - 所有关节在线、err==0 且角度在 [4.39°, 316.48°] 内 → safe
          - 有离线关节（读不到）→ warn（总线可能未连接）
          - 有角度超出限位 → danger（该关节处于极限位，上电可能损伤）
          - 全部/大部分在线但错误码=0x10(输入电压错误) → power（电源异常）
        """
        joints = pose.get("joints", {})
        total = len(joints)
        online = [j for j in joints.values() if j.get("online")]
        offline = [k for k, j in joints.items() if not j.get("online")]
        voltage_err = [k for k, j in joints.items()
                       if j.get("online") and j.get("error_code") == 0x10]
        other_err = [k for k, j in joints.items()
                     if j.get("online") and j.get("error_code") not in (None, 0, 0x10)]
        min_deg = MIN_GOAL_TICK * DEG_MAX / POS_MAX   # ≈ 4.39°
        max_deg = MAX_GOAL_TICK * DEG_MAX / POS_MAX   # ≈ 316.48°
        out_of_range = []
        for k, j in joints.items():
            if j.get("online") and j.get("angle") is not None and j.get("error_code") in (None, 0):
                a = j["angle"]
                if a < min_deg or a > max_deg:
                    out_of_range.append((k, a))
        issues = []
        if voltage_err:
            issues.append(f"有 {len(voltage_err)} 个关节报告「输入电压错误」—— 24V 母线电压异常，请检查电源/电池/连接")
        if other_err:
            issues.append(f"有 {len(other_err)} 个关节报告其他错误（非电压错误）")
        if out_of_range:
            issues.append("有 %d 个关节角度超出安全范围（限位附近）" % len(out_of_range))
        if offline:
            issues.append("有 %d 个关节离线/未响应（总线可能未连接或该 ID 未接电机）" % len(offline))
        if total == 0:
            return {"level": "unknown", "level_text": "无法读取", "issues": ["未能读取任何关节"],
                    "online_count": 0, "total": 0, "out_of_range": out_of_range,
                    "offline": offline, "voltage_err": voltage_err, "other_err": other_err}
        # 优先级：电源 > 限位 > 离线 > 正常
        if voltage_err and len(voltage_err) >= max(1, len(online) // 2):
            return {"level": "power", "level_text": "电源异常：所有舵机报电压错误",
                    "issues": issues, "online_count": len(online), "total": total,
                    "out_of_range": out_of_range, "offline": offline,
                    "voltage_err": voltage_err, "other_err": other_err}
        if out_of_range:
            return {"level": "danger", "level_text": "危险：建议先复位再上电", "issues": issues,
                    "online_count": len(online), "total": total,
                    "out_of_range": out_of_range, "offline": offline,
                    "voltage_err": voltage_err, "other_err": other_err}
        if offline or other_err:
            return {"level": "warn", "level_text": "警告：部分关节未响应或报错", "issues": issues,
                    "online_count": len(online), "total": total,
                    "out_of_range": [], "offline": offline,
                    "voltage_err": voltage_err, "other_err": other_err}
        return {"level": "safe", "level_text": "安全：可以上电", "issues": [],
                "online_count": len(online), "total": total,
                "out_of_range": [], "offline": [],
                "voltage_err": [], "other_err": []}

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
            r = self.robot.play(action)
            self._json({"ok": True, "action": action, "output": (r.stdout or "")[:200]})
        elif path == "/api/idle":
            action = (body.get("action") or "").strip()
            r = self.robot.play_idle(action)
            self._json({"ok": True, "action": action})
        elif path == "/api/reset":
            r = self.robot.reset()
            self._json({"ok": True, "output": (r.stdout or "")[:200]})
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
            self._json({"ok": True, "raw": res})
        elif path == "/api/pose":
            pose = self.robot.read_pose()
            if "error" in pose and not pose.get("joints"):
                self._json({"error": pose["error"], "joints": {}, "safety": None}, 503)
            else:
                safety = self.robot.assess_safety(pose)
                zero_values = self.robot.read_zero_values()
                self._json({"ok": True, "joints": pose["joints"], "safety": safety,
                            "zero_values": zero_values,
                            "limits": {"min_angle": round(MIN_GOAL_TICK * DEG_MAX / POS_MAX, 2),
                                       "max_angle": round(MAX_GOAL_TICK * DEG_MAX / POS_MAX, 2)}})
        elif path == "/api/standby":
            quiet = bool(body.get("quiet", True))
            target = self.robot.IDLE_QUIET if quiet else self.robot.IDLE_NORMAL
            ok, out = self.robot.set_idle_interval(target)
            self._json({"ok": ok, "quiet": quiet, "idle_interval": target, "output": out})
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
