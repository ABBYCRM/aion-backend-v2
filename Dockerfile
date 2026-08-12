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

# v2.8.14 — operator PyPI gift batch (2026-08-12). 14 packages, all pinned.
# Operator's exact words: "digest ingest and install all". Strategy:
# - Layer 1 (REQUIRED): the 7 lightweight packages. These will fit in basic-xxs.
# - Layer 2 (BEST-EFFORT): the 7 heavy packages, installed one at a time so
#   if one fails the others still come through. Each successful install is
#   logged to /var/data/gifts_install.log so the operator can see which made it.
COPY requirements-gifts-light.txt requirements-gifts-heavy.txt install-gifts-heavy.sh ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements-gifts-light.txt && \
    bash install-gifts-heavy.sh

COPY --chown=aion:aion app ./app
# /app/data = READ-ONLY shipped corpus (CSVs, JSONL catalogs). Mounted as image
# layer, replaced on every redeploy. Anything that should PERSIST across
# deploys (RAG indexes, sqlite notes, audit log) must go under
# $AION_DATA_DIR (/var/data) which is a mounted volume on DO.
COPY --chown=aion:aion data ./data

USER aion
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT','8080'), timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
