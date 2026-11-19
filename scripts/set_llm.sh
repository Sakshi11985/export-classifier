#!/usr/bin/env bash
# Switch the reasoning backend in .env and restart the containers that read it.
#
#   scripts/set_llm.sh offline   deterministic offline policy (no key, no network). Correct on the eval set.
#                                Safe default for "docker compose up" from a clean clone. NOT a language model.
#   scripts/set_llm.sh stub      bundled llm-stub: FIXED vacuum-flask answers after STUB_DELAY_S. Use ONLY for
#                                the load test (D6) and the circuit-breaker demonstration (A5). Any other
#                                product is (correctly) rejected by the verifier and escalated.
#   scripts/set_llm.sh real      your provider: copies REAL_LLM_BASE_URL / REAL_LLM_API_KEY / REAL_LLM_MODEL
#                                (and optional REAL_LLM_FALLBACK_*) from .env into the active LLM_* settings.
#
# Add --no-restart to only edit .env.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || cp .env.example .env

get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
setkv() {  # setkv KEY VALUE   (value may contain / : etc.)
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^$k=" .env; then
    K="$k" V="$v" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]} index($0, k"=")==1{print k"="v; next} {print}' .env > "$tmp"
  else
    cat .env > "$tmp"; echo "$k=$v" >> "$tmp"
  fi
  cat "$tmp" > .env; rm -f "$tmp"
}

mode="${1:-}"; restart=1
[ "${2:-}" = "--no-restart" ] && restart=0
case "$mode" in
  offline)
    setkv LLM_PROVIDER offline ;;
  stub)
    setkv LLM_PROVIDER openai_compat
    setkv LLM_BASE_URL "http://llm-stub:8080/v1"
    setkv LLM_API_KEY ""
    setkv LLM_MODEL_CLASSIFIER stub-model
    setkv LLM_MODEL_VERIFIER stub-model
    setkv LLM_FALLBACK_BASE_URL ""; setkv LLM_FALLBACK_API_KEY ""; setkv LLM_FALLBACK_MODEL "" ;;
  real)
    url="$(get REAL_LLM_BASE_URL)"; key="$(get REAL_LLM_API_KEY)"; model="$(get REAL_LLM_MODEL)"
    if [ -z "$url" ] || [ -z "$key" ] || [ -z "$model" ]; then
      echo "ERROR: fill REAL_LLM_BASE_URL, REAL_LLM_API_KEY and REAL_LLM_MODEL in .env first (see README section 7)." >&2
      exit 1
    fi
    setkv LLM_PROVIDER openai_compat
    setkv LLM_BASE_URL "$url"; setkv LLM_API_KEY "$key"
    setkv LLM_MODEL_CLASSIFIER "$model"; setkv LLM_MODEL_VERIFIER "$(get REAL_LLM_VERIFIER_MODEL || true)"
    [ -z "$(get LLM_MODEL_VERIFIER)" ] && setkv LLM_MODEL_VERIFIER "$model"
    setkv LLM_FALLBACK_BASE_URL "$(get REAL_LLM_FALLBACK_BASE_URL)"
    setkv LLM_FALLBACK_API_KEY "$(get REAL_LLM_FALLBACK_API_KEY)"
    setkv LLM_FALLBACK_MODEL "$(get REAL_LLM_FALLBACK_MODEL)" ;;
  *)
    echo "usage: scripts/set_llm.sh offline|stub|real [--no-restart]"; exit 2 ;;
esac

echo "active backend: LLM_PROVIDER=$(get LLM_PROVIDER)  base_url=$(get LLM_BASE_URL)  model=$(get LLM_MODEL_CLASSIFIER)"
if [ "$restart" = 1 ]; then
  # api reports the provider in /health/ready; worker runs the agents. The whole-run cache key includes the
  # provider, so answers cached under another backend are not reused.
  n="$(docker compose ps -q worker 2>/dev/null | wc -l | tr -d ' ')"; [ "${n:-0}" -ge 1 ] || n=1
  docker compose up -d --no-deps --force-recreate --scale worker="$n" api worker   # keep the current replica count
  echo "restarted api + worker. Check: curl -s localhost:8000/health/ready"
fi
