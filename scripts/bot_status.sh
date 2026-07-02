#!/bin/bash
PID_FILE=/Users/rui/code/stock_market/logs/bot.pid
if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "✅ Bot 正在运行，PID: $(cat $PID_FILE)"
    echo ""
    echo "最近日志："
    tail -8 /Users/rui/code/stock_market/logs/app.log
else
    echo "❌ Bot 未运行"
    echo "启动命令: bash scripts/start_bot.sh"
fi
