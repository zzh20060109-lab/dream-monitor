FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DM_HOST=0.0.0.0 \
    DM_AUTO=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 标准库 + PyYAML + requests（dmclient/notify/share 用到）；tzdata 提供上海时区
RUN apt-get update -qq && apt-get install -y -qq tzdata && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pyyaml==6.0.2 requests==2.32.3

COPY *.py ./
COPY config.yaml ./
COPY ui ./ui

# data 目录用于 SQLite，挂卷持久化
RUN mkdir -p /app/data

# 平台会通过 PORT 环境变量注入端口；代码已读取 PORT/DM_PORT
EXPOSE 8770
CMD ["python", "gui_server.py", "--no-open"]
