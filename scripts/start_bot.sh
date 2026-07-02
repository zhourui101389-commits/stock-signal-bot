#!/bin/bash
cd /Users/rui/code/stock_market

PID_FILE=logs/bot.pid
if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "⚠️  Bot 已在运行，PID: $(cat $PID_FILE)"
    exit 0
fi

nohup .venv/bin/python main.py >> logs/app.log 2>&1 &
echo $! > logs/bot.pid
echo "✅ Bot 已在后台启动，PID: $!"
echo "   日志: logs/app.log"
echo "   停止: bash scripts/stop_bot.sh"
