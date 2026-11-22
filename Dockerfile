FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DB_PATH=/data/review.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 两 worker 各自打开同一 SQLite 文件；配合 WAL 与短事务可满足演示规模
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=60", "wsgi:app"]
