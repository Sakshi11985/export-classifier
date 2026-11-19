#!/usr/bin/env bash
# Start the whole stack as plain processes (no Docker): redis, chroma server, llm-stub, api, N workers.
# Used for development and for the tests in this sandbox. Usage: scripts/local_stack.sh start|stop [workers]
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=${RUN_DIR:-/tmp/ec-stack}; mkdir -p "$RUN"
PY=${PYTHON:-python3}
export PYTHONPATH=.
export REDIS_URL=${REDIS_URL:-redis://localhost:6391/0}
export CHROMA_MODE=http CHROMA_HOST=localhost CHROMA_PORT=${CHROMA_PORT:-8010}
export EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-hashing}
export CORPUS_DIR=./corpus CHROMA_PERSIST_DIR=$RUN/chroma_meta/chroma
export LLM_PROVIDER=${LLM_PROVIDER:-offline}
case "${1:-start}" in
  start)
    N=${2:-1}
    redis-server --port 6391 --save "" --appendonly no --daemonize yes --pidfile $RUN/redis.pid >/dev/null
    nohup ${CHROMA_BIN:-chroma} run --path $RUN/chroma --port $CHROMA_PORT > $RUN/chroma.log 2>&1 & echo $! > $RUN/chroma.pid
    STUB_PORT=${STUB_PORT:-8090} nohup $PY -m stub.llm_stub > $RUN/stub.log 2>&1 & echo $! > $RUN/stub.pid
    for i in $(seq 1 40); do curl -sf localhost:$CHROMA_PORT/api/v2/heartbeat >/dev/null && break; sleep 0.5; done
    $PY -m app.corpus.ingest > $RUN/ingest.log 2>&1 || { cat $RUN/ingest.log; exit 1; }
    nohup $PY -m uvicorn app.api:get_app --factory --port ${API_PORT:-8000} --log-level warning > $RUN/api.log 2>&1 & echo $! > $RUN/api.pid
    for w in $(seq 1 $N); do
      WORKER_METRICS_PORT=$((9100+w)) nohup $PY -m app.worker > $RUN/worker$w.log 2>&1 & echo $! > $RUN/worker$w.pid
    done
    for i in $(seq 1 60); do curl -sf localhost:${API_PORT:-8000}/health/ready >/dev/null && break; sleep 0.5; done
    echo "stack up: api http://localhost:${API_PORT:-8000}  workers=$N  logs in $RUN";;
  workers)   # restart only the workers with a different count: scripts/local_stack.sh workers 3
    N=${2:-1}
    for f in $RUN/worker*.pid; do [ -f "$f" ] && kill "$(cat $f)" 2>/dev/null || true; rm -f "$f"; done
    sleep 2
    for w in $(seq 1 $N); do
      WORKER_METRICS_PORT=$((9100+w)) nohup $PY -m app.worker > $RUN/worker$w.log 2>&1 & echo $! > $RUN/worker$w.pid
    done
    sleep 2; echo "workers=$N";;
  stop)
    for f in $RUN/*.pid; do [ -f "$f" ] && kill "$(cat $f)" 2>/dev/null || true; done
    sleep 1; rm -f $RUN/*.pid; echo stopped;;
esac
