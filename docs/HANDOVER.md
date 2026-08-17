# FiguRobot 技术交接文档

> 面向接手本项目开发/维护的开发者。记录逆向成果、通信协议、软件架构、服务管理与故障排查经验。

## 1. 硬件架构

```
PC (Windows)
  │  USB 数据线
  ▼
控制盒（瑞芯微 RK3568 开发板，lckfb 出品，Ubuntu 20.04 aarch64）
  ├─ motion_main 进程   → /dev/ttyACM2（1M 波特率，Dynamixel 协议）→ 32 个舵机
  ├─ rk_serial_stm32    → /dev/ttyACM0（Modbus 协议）→ STM32（电源/状态/LED/急停）
  └─ state_manager      → 状态机（唤醒、串口授权、IPC）
```

- **控制盒 ADB 序列号**：`654abf5e47bfded4`
- **机器 SN**：`FR03202606301006`
- **两个串口**（同一 CH342 双串口芯片，稳定名用 `/dev/serial/by-id/`）：
  - `usb-1a86_USB_Single_Serial_5C84392272-if00` → ttyACM0（STM32）
  - `usb-1a86_USB_Single_Serial_5C84392271-if00` → ttyACM2（舵机）
- **舵机**：自研毫米级微型数字舵机（6mm 直径），Dynamixel 协议兼容，ID 1–32（缺 18），共 31 个。

## 2. 电机通信协议（Dynamixel Protocol 2.0 变体）

### 帧格式

```
Header      Reserved   ID      Length       Instruction   Param...     CRC
FF 00 FD    00         ID      Len_L Len_H  INST          P1...PN      CRC_L CRC_H
```

- `Length` = Instruction + Param + CRC 的长度 = Param 长度 + 3
- CRC16（IBM/ANSI，polynomial 0x8005，initial 0xFFFF），计算范围从 Header 到 Param（不含 CRC）
- 广播 ID = `0xFE`（254）

### 指令集

| 值 | 指令 | 说明 |
|---|---|---|
| 0x01 | Ping | 探测在线 |
| 0x02 | Read | 读寄存器 |
| 0x03 | Write | 写寄存器 |
| 0x20 | Control Table Backup | 保存配置 |
| 0x55 | Status | 状态返回包 |
| 0x82 | Sync Read | 批量读 |
| 0x83 | Sync Write | 批量写 |

### 关键寄存器

| 地址 | 名称 | 长度 | 说明 |
|---|---|---|---|
| 0x0B | Operating Mode | 1 | 3=位置控制，5=力矩控制 |
| 0x40 | Torque Enable | 1 | 1=使能，0=失能 |
| 0x74 | Goal Position | 4 | 目标位置（小端 u32） |
| 0x84 | Present Position | 4 | 当前位置（小端 u32） |
| 0x90 | Present Input Voltage | 2 | 电压，0.1V 单位 |
| 0x92 | Present Temperature | 1 | 温度 |

### 角度换算

```
angle = raw × 360.0 / 4095     (raw: 0–4095 → 0–360°)
软件限位：clamp(raw, 50, 3600) ≈ [4.39°, 316.48°]
```

### 错误码（Status 包 error 字节）

官方 demo `servo_figurobot_v2.py` 的 `_ERROR_NAMES` 只定义以下**枚举值**（非 Dynamixel 位掩码）：

| 值 | 含义 |
|---|---|
| 0x01 | Result Fail |
| 0x02 | Instruction Error |
| 0x03 | CRC Error |
| 0x04 | Data Range Error |
| 0x05 | Data Length Error |
| 0x06 | Data Limit Error |
| 0x07 | Access Error |
| 0x10 | **未定义**（27 个舵机实测会集体报此码，但能正常驱动；含义待官方确认） |

> ⚠️ **关键**：舵机报 `0x10` 时，Read/SyncRead 可能不返回数据（只响应 Ping），表现为「离线」。但**不影响驱动播放动作**，不是通信/电源故障。

## 3. 软件架构

### 控制盒端（/userdata/，PyInstaller 打包的 Python 3.8）

| 程序 | 路径 | 职责 |
|---|---|---|
| motion_main | `/userdata/motion_main/dist/Main` | 运动主控，监听 Unix socket 播放动作 |
| rk_serial_stm32 | `/userdata/rk_serial_stm32/dist/main` | STM32 桥（Modbus） |
| state_manager | `/userdata/state_manager/` | 状态机、KWS 唤醒 |
| robotConnectServer | — | 配网服务 |

- **动作播放核心**：Unix socket `/tmp/motion_main_socket`，命令：
  - `:Play <动作名>` / `:PlayIdle` / `:ListMotions` / `Motion_Reset` / `:Exit`
- 动作 CSV 位于 `/sdcard/.config/Figurobot/csv_folder/`，动作列表 `motionList.json`
- 零点偏移：`/userdata/data/zero.ini`（INI 格式，`N\id=X` + `N\zeroValue=Y`）

### PC 端（本项目）

| 文件 | 职责 |
|---|---|
| `robot_bridge.py` | adb → HTTP API（端口 8888） |
| `serial_exec.py` | 设备端串口执行脚本（桥接自动推送到 `/tmp/`） |
| `figurobot-console.html` | 前端控制台（含 Three.js 3D 骨架） |

## 4. 服务管理（重要）

`motion_main` 和 `rk_serial_stm32` 是 **systemd user 服务**（非系统服务），重启必须用 lckfb 身份：

```bash
# 通过 adb shell 进入控制盒后
su - lckfb -c 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart motion_main'
su - lckfb -c 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart rk_serial_stm32'
su - lckfb -c 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status motion_main'
```

配置文件：
- motion_main：`/userdata/.figurobot/motion_main/config.json`
- rk_serial_stm32：`/userdata/.figurobot/rk_serial_stm32/config.json`

## 5. 已知问题与陷阱

1. **串口节点漂移**：重新插线后 `/dev/ttyACM1` → `ttyACM2`，motion_main 配置写死会失效。必须用 `/dev/serial/by-id/` 稳定名。
2. **shell 引号转义 bug**：往设备传 Python 脚本不能用 `python3 -c {code!r}`（bash 单引号内不接受 `\'`）。本项目用 **base64 编码 + `echo | base64 -d | python3`** 传代码。
3. **`os.environ` 赋值**：value 必须是 str，float 要显式 `str()`。
4. **SYNC_READ 在异常态不响应**：读姿态要用「逐个 Ping（拿 online+error）+ 单 ID Read（拿 position）」两段式。
5. **motion_main 独占串口**：`serial_mode: exclusive`，播放动作时会抢串口，实时关节控制会与其冲突。读姿态建议临时 stop motion_main。
6. **`dev_mode` 配置项**：设 `dev_mode=true` 会让 motion_main 跳过 socket 监听（非 edit mode），不要用。

## 6. 故障排查流程

| 症状 | 排查 |
|---|---|
| 网页连不上桥接 | 检查 8888 端口、adb devices |
| 动作不播放 | 查 motion_main 日志，确认串口 `connect_serial` 成功 |
| 上电一直「乱动」 | 自动待机动作（idle_interval=5），控制台「待机开关」关闭即可 |
| 关节全离线/报 0x10 | 停 motion_main 后 Ping；0x10 官方未定义、不影响驱动，含义待官方确认 |
| 自检报错 | 查 rk_serial_stm32 日志（硬件自检硬编码匹配设备名） |

## 7. 待机开关实现

「待机开关」通过修改 motion_main `config.json` 的 `idle_interval` 实现：
- 自动待机（默认）：`idle_interval=5`，5 秒无命令随机播 Idle 动作
- 安静待机：`idle_interval=86400`，基本不再自动乱动

桥接服务 API：`GET /api/standby`（查状态）、`POST /api/standby {quiet: bool}`（切换，改配置并重启 motion_main）。不清空 Idle 动作库。

## 8. 逆向产物与参考

- 反编译产物：`.workbuddy/robot_bin/`（Main / rk_serial_main / robot_server / state_manager）
- 官方 demo 源码：`发货资料分类_20260708/控制盒/demo/motor/python_motion_demo_*/python_motion_demo/servo_figurobot_v2.py`
- 官方文档：`发货资料分类_20260708/共用资料/文档/`（protocol.pdf、servo_useage_position_*.pdf、servo_useage_torque.md）
