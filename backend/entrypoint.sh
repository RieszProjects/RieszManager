#!/bin/sh
# Apply migrations, then serve the ASGI app (HTTP + WebSocket) with uvicorn.
set -e
python manage.py migrate --noinput
exec uvicorn riesz.asgi:application \
  --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips '*' \
  --lifespan off --no-server-header --ws websockets
