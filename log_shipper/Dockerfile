FROM python:3.12.10-slim

WORKDIR /app

# Create shipper user. Docker socket access is handled by running
# the container with --group-add matching the host's docker socket GID,
# configured in docker-compose.yml (not hardcoded here).
RUN adduser --disabled-password --no-create-home --system --group shipper

# Install dependencies
# Using slim instead of alpine ensures we can use wheels for asyncpg,
# keeping the image small without needing build-essentials.
COPY --chown=shipper requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the shipper script
COPY --chown=shipper shipper.py .

USER shipper

# Ensure logs are flushed immediately to stdout for debugging
ENV PYTHONUNBUFFERED=1

# Log shipper health: verify the Python process is alive.
# No HTTP endpoint — checks that PID 1 (the shipper) is running.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD kill -0 1 || exit 1

# Run the shipper
CMD ["python", "shipper.py"]
