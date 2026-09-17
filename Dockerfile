# Ops console image. Same shape as the core API's: slim base, wheels only,
# non-root runtime user, no build toolchain in the final layer.
# NOT YET PINNED — pin this before the first production deploy.
#
# A tag is a moving pointer: `3.12-slim-bookworm` today and in six months are
# different images, so an unpinned rebuild can change the runtime underneath a
# service nobody touched. Run ./scripts/pin-base-image.sh on a machine with
# Docker; it resolves the tag to a digest and rewrites this line. The session
# that wrote this had no route to a container registry, so it left the tag
# rather than commit a digest it could not verify.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd --create-home --uid 10010 opsconsole && chown -R opsconsole:opsconsole /app
USER opsconsole

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8010/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "2"]
