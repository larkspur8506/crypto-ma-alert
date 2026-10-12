FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

# 数据卷挂载点（SQLite）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000
# 必须单 worker：APScheduler 与去重都在进程内，多 worker 会重复推送
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
