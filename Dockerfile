# Dockerfile for TheDeepView Bot — Hugging Face Spaces (Docker SDK, free CPU tier)
FROM python:3.11-slim

# Install tini for proper PID 1 signal handling (clean shutdown on SIGTERM)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory (HF mounts /data automatically when persistent storage enabled)
RUN mkdir -p /data
ENV DATA_DIR=/data
ENV PORT=7860

# Expose the port HF expects (7860 for Spaces)
EXPOSE 7860

# Use tini as init, then run scheduler (which starts uvicorn + APScheduler)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scheduler.py"]
