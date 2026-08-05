FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    AION_DATA_DIR=/var/data

WORKDIR /app

RUN addgroup --system --gid 10001 aion \
    && adduser --system --uid 10001 --ingroup aion --home /home/aion aion \
    && mkdir -p /var/data \
    && chown -R aion:aion /var/data /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=aion:aion app ./app
COPY --chown=aion:aion data ./data

USER aion
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT','8080'), timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
