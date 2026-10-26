FROM python:3.12-slim

# Install lightweight dependencies for player monitoring & K8s Lease coordination
RUN pip install --no-cache-dir kubernetes requests

WORKDIR /app
COPY lib/foundry_lib /app/foundry_lib/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "foundry_lib.sidecar_monitor"]
