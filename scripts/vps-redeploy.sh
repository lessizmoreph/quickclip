#!/usr/bin/env bash
# Rebuild + run QuickClip on the VPS.
# Routed container-direct on root_default via Traefik labels (no host port).
set -euo pipefail

APP=quickclip
IMG=quickclip:latest
NAME=quickclip
ROUTER=quickclip
HOST=quickclip.pdowndigital.com
PORT=8501

cd "/root/${APP}"

echo "==> Building ${IMG}"
docker build -t "${IMG}" .

echo "==> Replacing container ${NAME}"
docker rm -f "${NAME}" 2>/dev/null || true

docker run -d --name "${NAME}" --restart unless-stopped \
  --network root_default \
  -v "/root/${APP}/data:/app/data" \
  --label traefik.enable=true \
  --label "traefik.http.routers.${ROUTER}.entrypoints=web,websecure" \
  --label "traefik.http.routers.${ROUTER}.rule=Host(\`${HOST}\`)" \
  --label "traefik.http.routers.${ROUTER}.tls=true" \
  --label "traefik.http.routers.${ROUTER}.tls.certresolver=mytlschallenge" \
  --label "traefik.http.services.${ROUTER}.loadbalancer.server.port=${PORT}" \
  "${IMG}"

echo "==> Done. Live at https://${HOST}"
docker ps --filter "name=${NAME}" --format '  {{.Names}}  {{.Status}}'
