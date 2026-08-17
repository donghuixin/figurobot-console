# FiguRobot 网页控制台

一个基于 Web 的 **Figurobot（灵童机器人「念」NIA-F01 系列）人形机器人控制台**，提供毛玻璃科技风 UI，通过 ADB 连接机器人控制盒，实现动作播放、关节姿态读取、上电安全判断与 3D 骨架可视化。

> ⚠️ 本项目为个人学习/二次开发用途，非官方工具。请在安全环境下使用，上电前务必先读取姿态确认安全。

## 功能特性

- 🎬 **动作库播放** — 加载并播放机器人内置的 105 个动作（挥手、跳舞、情绪等）
- 📐 **姿态读取** — 读取全部 32 个关节的当前位置与角度
- 🛡️ **上电安全判断** — 自动评估当前姿态是否安全，识别「超限 / 离线」等状态
- 🦴 **3D 骨架可视化** — Three.js 人形骨架，关节球颜色直观显示安全状态
- 🔄 **姿态复位** — 一键回到安全姿态（Motion_Reset）
- 🌙 **待机开关** — 一键关闭/开启「自动待机动作」，防止机器人上电后自动乱动
- 🔧 **实时关节控制** — 底层 Dynamixel 协议直接读写（实验功能）

## 硬件架构

```
PC (Windows)
  │  USB (ADB)
  ▼
控制盒 (Rockchip RK3568, Ubuntu 20.04 aarch64)
  ├── motion_main      → 串口(ttyACM2) → 32× Dynamixel 兼容舵机
  └── rk_serial_stm32  → 串口(ttyACM0) → STM32（电源/状态管理）
```

## 快速开始

### 前置条件

1. **Python 3**（3.8+）
2. **Android platform-tools（adb）** — 可放入 `tools/platform-tools/`，或已加入系统 PATH
3. **机器人已通过 USB 连接**，并被识别为 ADB 设备（Rockchip 设备）

### 启动

双击 `start_console.bat`（Windows），或手动：

```bash
# 1. 启动桥接服务（HTTP API，端口 8888）
python robot_bridge.py --port 8888

# 2. 浏览器打开控制台
#    双击 figurobot-console.html，或启动本地静态服务后访问
```

打开网页后点击左上角「连接桥接」即可开始控制。

## 目录结构

```
├── figurobot-console.html   # 网页控制台（前端，含 Three.js 3D 骨架）
├── robot_bridge.py          # 桥接服务（adb → HTTP API）
├── serial_exec.py           # 设备端串口执行脚本（桥接服务自动推送）
├── start_console.bat        # Windows 一键启动脚本
├── tools/platform-tools/    # （可选）adb 工具目录
└── docs/
    └── HANDOVER.md          # 技术交接文档（协议、架构、故障排查）
```

## 桥接服务 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/status`  | 连接状态 + 设备信息 |
| GET  | `/api/motions` | 动作列表 |
| POST | `/api/play`    | 播放动作 `{"action":"你好"}` |
| POST | `/api/reset`   | 姿态复位 |
| POST | `/api/pose`    | 读取全部关节姿态 + 安全评估 |
| POST | `/api/joint/scan` | 扫描在线关节 |
| POST | `/api/joint/read` | 读取单关节位置 `{"id":1}` |

## 常见问题

### 机器人上电后一直「乱动」

这是 motion_main 的**自动待机动作机制**：`idle_interval=5`，5 秒无命令就随机播放一个 Idle 待机动作（挥手、眺望等），并非故障。

- 控制台左侧「待机开关」按钮可一键关闭/开启（关闭后基本不再自动乱动）
- 该功能通过修改 `idle_interval`（5 ↔ 86400）并重启 motion_main 实现

### 关节全部「离线」或报 `0x10` 错误

官方 demo 代码的错误码表只定义了 0x01–0x07，`0x10` 未在官方文档中出现。实测 27 个舵机会集体报 `0x10`，但**仍能正常驱动播放动作**（说明供电正常）。该错误码的真实含义需向官方确认，不影响正常使用。

### 串口节点漂移

重新插拔 USB 后，`/dev/ttyACM1` 可能漂移成 `ttyACM2`。控制盒端 motion_main 的配置需使用稳定符号链接
`/dev/serial/by-id/usb-1a86_USB_Single_Serial_5C84392271-if00`（电机通道）。

### 端口 8888 被占用

```bash
# Windows 查找并结束占用进程
netstat -ano | findstr :8888
taskkill /F /PID <pid>
```

## 免责声明

本工具会直接控制真实机器人的关节电机。错误操作可能导致电机损坏或人身伤害。请务必：
1. 上电前先读取姿态并确认安全
2. 运行动作时保持机器人周围有足够空间
3. 不要手动调高供电电压

## License

MIT License — 仅供学习研究使用，与官方 Figurobot 无关联。
