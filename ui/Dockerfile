FROM python:3.12.10-slim

WORKDIR /app

RUN adduser --disabled-password --no-create-home appuser

COPY --chown=appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yml

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:7860/')" || exit 1

CMD ["python", "app.py"]
