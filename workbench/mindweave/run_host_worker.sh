#!/bin/bash
set -a
source .env
set +a
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306
export REDIS_HOST=127.0.0.1 REDIS_PORT=6379 REDIS_DB=3
export NEO4J_URI=bolt://127.0.0.1:17687
export HF_ENDPOINT=https://hf-mirror.com
cd backend
exec "$HOME/Documents/Codex/MindWeave/.venv312/bin/python" -m app.graph.worker
