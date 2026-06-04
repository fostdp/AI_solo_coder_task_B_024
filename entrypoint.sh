#!/bin/sh
set -e

python init_db.py || echo "[WARN] DB init skipped (may already exist)"

python grpc_server.py &
GRPC_PID=$!
echo "[START] gRPC server started (PID=$GRPC_PID)"

uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}
