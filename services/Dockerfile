# One image for the whole victim stack. Which service runs is decided by
# the compose command, which keeps the build cache warm and the demo fast.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common /app/common
COPY inventory-api /app/inventory-api
COPY checkout-api /app/checkout-api
COPY loadgen /app/loadgen
COPY chaos /app/chaos

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/common \
    SITREP_DB=/data/telemetry.db

CMD ["python", "-c", "print('specify a command')"]
