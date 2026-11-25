#!/bin/sh
set -e
# Build the vector index once, before workers start (avoids a race between workers).
python -m scripts.build_index --if-missing
exec gunicorn "app.main:get_app()" -k uvicorn.workers.UvicornWorker \
  --workers "${WEB_WORKERS:-2}" --bind 0.0.0.0:8000 \
  --timeout 180 --graceful-timeout 30 --keep-alive 15 --access-logfile -
