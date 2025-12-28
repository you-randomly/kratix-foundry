FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY *.py ./
COPY cogs/ ./cogs/
COPY utils/ ./utils/

# Run the bot
CMD ["python", "bot.py"]
