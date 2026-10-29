FROM python:3.12-slim

# Install iproute2 (for ss socket inspection) and Python dependencies
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir kubernetes requests

WORKDIR /app
COPY lib/foundry_lib /app/foundry_lib/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "foundry_lib.sidecar_monitor"]
