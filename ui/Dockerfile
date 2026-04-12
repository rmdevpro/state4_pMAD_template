FROM python:3.12.10-slim

WORKDIR /app

RUN adduser --disabled-password --no-create-home appuser

COPY --chown=appuser requirements.txt .

ARG PIP_INDEX_URL="http://irina:3141/root/pypi/+simple/"
ARG PIP_TRUSTED_HOST="irina"
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" -r requirements.txt

COPY --chown=appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yml

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:7860/')" || exit 1

CMD ["python", "app.py"]
