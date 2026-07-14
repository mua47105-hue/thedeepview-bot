# Dockerfile for TheDeepView Bot — works on Render, Hugging Face Spaces, or any Docker host
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

# Create data directory for SQLite state + seen.json
RUN mkdir -p /data
ENV DATA_DIR=/data
# Render sets PORT automatically; default to 7860 for HF Spaces compatibility
ENV PORT=7860

# Expose port (Render uses the PORT env var, HF Spaces uses 7860)
EXPOSE 7860

# Use tini as init, then run scheduler (which starts uvicorn + APScheduler + state restore).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "scheduler.py"]
