FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src

ENV PYTHONUNBUFFERED=1 \
    SITREP_DB=/data/telemetry.db \
    SITREP_MCP_HOST=0.0.0.0 \
    SITREP_MCP_PORT=8931

EXPOSE 8931

CMD ["python", "/app/src/server.py"]
