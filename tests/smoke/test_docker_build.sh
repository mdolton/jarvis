#!/bin/bash
# Smoke test: build the Docker image, start it, hit healthz, tear down.
# Run from the repo root: bash tests/smoke/test_docker_build.sh
# Requires: docker

set -euo pipefail

IMAGE_NAME="jarvis-smoke-test"
CONTAINER_NAME="jarvis-smoke-$$"

cleanup() {
    echo "cleaning up..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker rmi "$IMAGE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== building docker image ==="
docker build -t "$IMAGE_NAME" .

echo "=== creating minimal config ==="
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/config" "$TMPDIR/data"

cat > "$TMPDIR/config/jarvis.yaml" <<EOF
llm:
  base_url: http://host.docker.internal:1234/v1
  api_key: dummy
  model: test-model
EOF
echo "{}" > "$TMPDIR/config/channels.yaml"
echo "servers: []" > "$TMPDIR/config/mcp-servers.yaml"

echo "=== starting container ==="
docker run -d \
    --name "$CONTAINER_NAME" \
    -p 18080:8080 \
    -v "$TMPDIR/config:/app/config:ro" \
    -v "$TMPDIR/data:/app/data" \
    -e JARVIS_LLM_BASE_URL=http://host.docker.internal:1234/v1 \
    -e JARVIS_LLM_API_KEY=dummy \
    -e JARVIS_LLM_MODEL=test-model \
    "$IMAGE_NAME"

echo "=== waiting for healthz (up to 30s) ==="
for i in $(seq 1 30); do
    if curl -sf http://localhost:18080/healthz > /dev/null 2>&1; then
        echo "healthz OK after ${i}s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "FAIL: healthz not responding after 30s"
        docker logs "$CONTAINER_NAME"
        exit 1
    fi
    sleep 1
done

echo "=== verifying healthz response ==="
RESP=$(curl -sf http://localhost:18080/healthz)
echo "healthz: $RESP"

if echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='ok', d"; then
    echo "=== SMOKE TEST PASSED ==="
else
    echo "=== SMOKE TEST FAILED ==="
    docker logs "$CONTAINER_NAME"
    exit 1
fi
