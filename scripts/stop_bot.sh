#!/bin/bash
PID_FILE=/Users/rui/code/stock_market/logs/bot.pid
# 杀掉所有 main.py 进程（防止多实例残留导致 409 Conflict）
pkill -f "python main.py" 2>/dev/null
pkill -f "python3 main.py" 2>/dev/null
sleep 1
rm -f "$PID_FILE"
if pgrep -f "python.*main.py" > /dev/null; then
    echo "⚠️  仍有残留进程，强制终止..."
    pkill -9 -f "main.py"
fi
echo "✅ Bot 已停止"
