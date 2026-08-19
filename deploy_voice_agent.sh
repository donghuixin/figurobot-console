#!/usr/bin/env bash
# Figurobot 语音助手部署脚本（在 PC 上执行，通过 adb 推送 + 安装依赖）
set -e
cd "$(dirname "$0")"

ADB="tools/platform-tools/adb.exe"
SERIAL="654abf5e47bfded4"
REMOTE_DIR="/userdata/voice_agent"

echo "==> 推送文件到控制盒 $REMOTE_DIR ..."
"$ADB" -s "$SERIAL" shell "mkdir -p $REMOTE_DIR /userdata/data/robot_memory"
"$ADB" -s "$SERIAL" push voice_agent.py "$REMOTE_DIR/voice_agent.py"
"$ADB" -s "$SERIAL" push memory_store.py "$REMOTE_DIR/memory_store.py"
"$ADB" -s "$SERIAL" push config.json "$REMOTE_DIR/config.json"

echo "==> 升级 pip 并安装依赖（openai / edge-tts）..."
"$ADB" -s "$SERIAL" shell "python3 -m pip install --upgrade pip -q 2>&1 | tail -2"
"$ADB" -s "$SERIAL" shell "python3 -m pip install openai edge-tts -q 2>&1 | tail -5"

echo "==> 验证依赖 ..."
"$ADB" -s "$SERIAL" shell "python3 -c 'import openai, edge_tts; print(\"openai\", openai.__version__); print(\"edge_tts ok\")'"

echo "==> 部署完成。运行方式："
echo "    adb shell 进入控制盒后："
echo "    cd $REMOTE_DIR && DEEPSEEK_API_KEY=sk-xxx python3 voice_agent.py --text"
