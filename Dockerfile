FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 数据目录（Fly.io 挂载持久化卷到 /data）
RUN mkdir -p /data logs

# DB_PATH 指向持久化卷
ENV DB_PATH=/data/signals.db
ENV DATA_SOURCE=yfinance

CMD ["python", "main.py"]
