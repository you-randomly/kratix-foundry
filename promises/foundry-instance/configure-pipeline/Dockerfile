FROM python:3.12-slim

# Install iproute2 (for ss socket inspection) and Python dependencies
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir kratix-sdk kubernetes pyyaml requests

# App structure
WORKDIR /app
COPY lib/foundry_lib /app/foundry_lib/
COPY promises/foundry-instance/configure-pipeline/scripts/ /app/scripts/

# Set working directory to /kratix for entrypoint
WORKDIR /kratix
ENV PYTHONPATH=/app

ENTRYPOINT ["python3", "/app/scripts/main.py"]
