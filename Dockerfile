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

# Use tini as init, then run scheduler (which starts uvicorn + APScheduler + state restore).
# NOTE: do NOT use 'python -m scheduler.py' — the -m flag expects a module name (no .py).
#       Also avoid 'python -m scheduler' — that risks importing Python's stdlib 'scheduler' module.
#       Running the file directly is the safest pattern.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "scheduler.py"]
