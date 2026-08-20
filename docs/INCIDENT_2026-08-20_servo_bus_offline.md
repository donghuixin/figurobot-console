# 事故交接：舵机总线全离线 + 左臂异常（2026-08-20）

> 面向接手排查者 / 硬件支持。记录本次「控制台连不上机器人 + 左臂不正常」的完整诊断过程与结论。

## 1. 现象

- 用户报告：控制台**又连不上机器人**（姿态面板显示 0 关节在线），且**左臂看起来不正常**（无力下垂）。
- 网页控制台通过桥接读姿态，返回 **27 个舵机全部离线、PING 无任何响应**。

## 2. 排查记录（软件层逐项确认，全部正常）

| 检查项 | 命令/位置 | 结果 |
|---|---|---|
| ADB 设备 | `adb devices` | ✅ 在线 `654abf5e47bfded4` |
| 桥接服务 | `GET /api/status` | ✅ `connected: true` |
| motion_main 进程 | `ps` | ✅ 运行中，**62 个 fd**（未耗尽） |
| fd 上限 | `/proc/<pid>/limits` | ✅ 65535（此前 LimitNOFILE 修复仍生效） |
| 串口节点 | `ls /dev/serial/by-id/` | ✅ 无漂移：`5C84392271-if00→ttyACM1`(舵机)、`5C84392272-if00→ttyACM0`(STM32) |
| 磁盘 | `df -h /` | ✅ 58%（6.6G/12G） |
| STM32 供电 | `rk_serial_stm32.log` | ✅ 电池 HIGH(≈8.75V)、hardware READY、network ONLINE |
| 重启 motion_main | `systemctl --user restart motion_main` | ✅ 重启成功，**但仍 0 舵机在线** |

## 3. 根因结论

- **串口能正常打开、能正常发送，但舵机总线上没有任何设备应答**（`ping_response_hex` 为空）。
- 重启 motion_main（重新初始化串口）后依然 0 在线 → **排除软件问题**。
- STM32 侧电池 HIGH、硬件 READY → 控制盒与主供电正常。

**结论：舵机总线层面的物理/供电故障**（舵机力矩供电掉电，或总线/线缆物理断开/短路）。左臂「不正常」是舵机失力矩后的自然下垂表现。

> ⚠️ 本次是 **0 在线（PING 完全无响应）**，与历史 `0x10` 错误（在线但报错码）是两种不同状态，不要混淆。

## 4. 建议处置

1. **彻底断电重启**（第一步，之前解决舵机异常的标准做法）：
   - 拔掉机器人**主电源**（非 USB 数据线），等待 ≥30 秒，重新上电；
   - 上电后重读姿态确认。
2. 若重启后**其他关节恢复、仅左臂仍异常** → 左臂某舵机硬件故障（卡死/损坏），需检修更换。
3. 若重启后仍 **0 在线** → 检查舵机总线线缆/接插件（CH9344 串口适配器到舵机级联链），尤其左臂段。

## 5. 遗留问题（非本次主因，先记录）

1. **ticos_client WebSocket 每 ~3 秒重连一次**：此前 fd 泄漏的源头，目前靠 `LimitNOFILE=65535` 兜底未崩溃，但未根治。
2. **摄像头周期性报 `CAMERA_ERROR`**：`rkisp` sensor 未激活，内核/健康检查反复报错（此前曾致磁盘刷屏）。

## 6. 关键路径 / 命令（供接手者快速复现）

```bash
ADB="tools/platform-tools/adb.exe"; S="654abf5e47bfded4"

# 读姿态（观察在线数）
curl -s -X POST http://127.0.0.1:8888/api/pose -H "Content-Type: application/json" -d '{}'

# 串口节点
adb -s $S shell "ls -l /dev/serial/by-id/"

# motion_main 状态 / 日志
adb -s $S shell "ps aux | grep motion_main"
adb -s $S shell "tail -40 /userdata/log/motion_main.log"

# STM32 供电状态
adb -s $S shell "tail -20 /userdata/log/rk_serial_stm32.log"

# 重启 motion_main（systemd user 服务）
adb -s $S shell "su - lckfb -c 'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart motion_main'"
```

## 7. 配置速查

- motion_main 配置：`/userdata/.figurobot/motion_main/config.json`（`serial.port` 用 by-id `5C84392271`，`serial_mode: exclusive`）
- 硬件信息：`/userdata/.figurobot/device/hardware.json`（SN `FR03202606301006`，MAC `9c:b8:b4:b3:09:46`）
- 电机 ID：27 个（1–30 缺 5/10/18/31/32）
