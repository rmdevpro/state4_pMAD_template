FROM python:3.12.10-slim

WORKDIR /app

RUN adduser --disabled-password --no-create-home --uid 1000 appuser

COPY --chown=appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "alerter:app", "--host", "0.0.0.0", "--port", "8000"]
