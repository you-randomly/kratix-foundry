FROM python:3.12-slim

# Install dependencies
RUN pip install --no-cache-dir kratix-sdk kubernetes pyyaml requests

# App structure
WORKDIR /app
COPY lib/foundry_lib /app/foundry_lib/
COPY promises/foundry-license/configure-pipeline/scripts/ /app/scripts/

# Set working directory to /kratix for entrypoint
WORKDIR /kratix
ENV PYTHONPATH=/app

ENTRYPOINT ["python3", "/app/scripts/main.py"]
