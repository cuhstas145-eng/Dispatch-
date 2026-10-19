FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CARGOSYNC_DATA_DIR=/data CARGOSYNC_SECURE_COOKIES=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py create_admin.py index.html ./
RUN mkdir -p /data/documents && useradd --system --uid 10001 cargosync && chown -R cargosync:cargosync /app /data
USER cargosync
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]