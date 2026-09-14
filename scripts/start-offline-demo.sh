#!/usr/bin/env bash
# Start the fully-offline downtime demo and warm both local models.
#
# One command for Scenario A of docs/runbook-demo.md: it checks the box can
# actually run offline, starts the mock HL7 integration engine and the downtime
# capture app, then loads the Whisper weights and the Ollama model into memory
# before anyone touches the UI.
#
# The warm-up is the point. Both models load lazily, so without it the first
# clinician action of the demo pays for a cold model load - tens of seconds for
# Whisper, a minute or more for a 26B model on CPU - with an audience waiting.
# This moves that cost before the room, and pins keep_alive at 2h so the weights
# stay resident for the whole session.
#
#   ./scripts/start-offline-demo.sh
#   ./scripts/start-offline-demo.sh --model qwen2.5:7b --whisper-model small --no-strict
set -euo pipefail

# Git Bash on Windows hands Python a cp1252 stdout, which cannot encode the
# checklist's tick and cross - and a demo script must not die formatting a tick.
export PYTHONIOENCODING=utf-8

MODEL="gemma4:26b"
WHISPER_MODEL="medium"
PORT=8200
ENGINE_PORT=2575
STRICT=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--model)         MODEL="$2"; shift 2 ;;
    -w|--whisper-model) WHISPER_MODEL="$2"; shift 2 ;;
    -p|--port)          PORT="$2"; shift 2 ;;
    -e|--engine-port)   ENGINE_PORT="$2"; shift 2 ;;
    --no-strict)        STRICT=false; shift ;;
    -h|--help)          sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/.downtime-logs"
APP_LOG="$LOG_DIR/app.log"
ENG_LOG="$LOG_DIR/engine.log"
PID_FILE="$LOG_DIR/pids.json"
OLLAMA_BASE="${EPICVIBE_DOWNTIME_OLLAMA_BASE_URL:-http://localhost:11434}"

# Git Bash on Windows has the venv under Scripts/, POSIX under bin/.
if   [[ -x "$ROOT/.venv/bin/python" ]];        then PY="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then PY="$ROOT/.venv/Scripts/python.exe"
else PY=""
fi

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# -- 1. preflight ------------------------------------------------------------

step "Checking the virtualenv"
[[ -n "$PY" ]] || fail ".venv not found under $ROOT. Create it first: python -m venv .venv && .venv/bin/pip install -e '.[audio]'"
echo "    $PY"

step "Checking Ollama at $OLLAMA_BASE"
TAGS="$(curl -fsS --max-time 10 "$OLLAMA_BASE/api/tags" 2>/dev/null)" \
  || fail "cannot reach the Ollama daemon at $OLLAMA_BASE. Start it with: ollama serve"
if ! printf '%s' "$TAGS" | "$PY" -c "
import json,sys
names=[m.get('name','') for m in json.load(sys.stdin).get('models',[])]
sys.stderr.write('    models present: '+', '.join(names)+'\n')
sys.exit(0 if '$MODEL' in names else 1)
"; then
  fail "model '$MODEL' is not on this box. Pull it while you still have a network:

           ollama pull $MODEL
"
fi
echo "    model '$MODEL' is present"

mkdir -p "$LOG_DIR"

# -- 2. processes ------------------------------------------------------------

# Run from the repo root so the relative inbox/db paths land where the docs say,
# and start each process directly (not in a subshell) so $! is the real PID the
# stop script has to kill.
cd "$ROOT"

step "Starting the mock integration engine on :$ENGINE_PORT"
nohup "$PY" -m epicvibe.downtime.mock_engine --port "$ENGINE_PORT" --inbox .downtime-inbox \
  >"$ENG_LOG" 2>&1 &
ENGINE_PID=$!
echo "    pid $ENGINE_PID -> $ENG_LOG"

step "Starting the downtime app on :$PORT"
nohup env \
  EPICVIBE_DOWNTIME_PROVIDER=ollama \
  EPICVIBE_DOWNTIME_OLLAMA_MODEL="$MODEL" \
  EPICVIBE_DOWNTIME_WHISPER_MODEL="$WHISPER_MODEL" \
  EPICVIBE_DOWNTIME_OFFLINE_STRICT="$STRICT" \
  EPICVIBE_DOWNTIME_OLLAMA_KEEP_ALIVE=2h \
  EPICVIBE_DOWNTIME_ENGINE_PORT="$ENGINE_PORT" \
  EPICVIBE_DOWNTIME_PORT="$PORT" \
  "$PY" -m epicvibe.downtime >"$APP_LOG" 2>&1 &
APP_PID=$!
echo "    pid $APP_PID -> $APP_LOG"

# Under Git Bash these are bash job PIDs, not Windows PIDs, so the stop script
# will usually fall through to its port-listener path there. That is fine - it
# is what the fallback exists for - and on POSIX the PIDs are the real ones.
cat > "$PID_FILE" <<JSON
{"engine": $ENGINE_PID, "app": $APP_PID, "port": $PORT, "enginePort": $ENGINE_PORT,
 "model": "$MODEL", "whisper": "$WHISPER_MODEL"}
JSON

# -- 3. wait for the app -----------------------------------------------------

step "Waiting for http://localhost:$PORT/api/status"
STATUS=""
for _ in $(seq 1 120); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then break; fi
  if STATUS="$(curl -fsS --max-time 3 "http://localhost:$PORT/api/status" 2>/dev/null)"; then break; fi
  STATUS=""
  sleep 0.5
done
if [[ -z "$STATUS" ]]; then
  printf '\033[31m    the app did not come up. Last lines of %s:\033[0m\n' "$APP_LOG"
  tail -n 40 "$APP_LOG" || true
  fail "downtime app failed to start (strict offline mode refuses to start if anything is not local)"
fi
printf '%s' "$STATUS" | "$PY" -c "
import json,sys
s=json.load(sys.stdin)
print(f\"    up: provider {s['provider']} | model {s['model']} | engine {s['engine']} | {s['templates']} templates\")
"

# -- 4. warm-up --------------------------------------------------------------

step "Warming both local models (this is the slow part - once, here, not in the room)"
# 10 minutes: a 26B model loading from cold disk is minutes, and failing the
# demo setup on an impatient timeout helps nobody.
WARM="$(curl -fsS --max-time 600 -X POST "http://localhost:$PORT/api/warmup")" \
  || fail "warm-up call failed"
printf '%s' "$WARM" | "$PY" -c "
import json,sys
w=json.load(sys.stdin)
sw=w['whisper']
if sw.get('loaded'):
    print(f\"    whisper   {sw['model']:<18} loaded in {sw['elapsed_s']:6.1f}s\")
else:
    print(f\"    whisper   {sw.get('model','?'):<18} NOT loaded - {sw.get('reason')}\")
p=w['provider']
if p.get('loaded'):
    print(f\"    provider  {p['provider']:<18} loaded in {p.get('elapsed_s',0):6.1f}s (keep_alive {p.get('keep_alive','-')})\")
else:
    print(f\"    provider  {p.get('provider','?'):<18} NOT loaded - {p.get('error') or p.get('reason')}\")
"

# -- 5. the offline claim, checked ------------------------------------------

step "Offline checklist (GET /api/offline)"
curl -fsS --max-time 10 "http://localhost:$PORT/api/offline" | "$PY" -c "
import json,sys
off=json.load(sys.stdin)
labels=[('provider_local','inference runs on this box'),
        ('whisper_installed','faster-whisper installed'),
        ('whisper_model_cached','Whisper weights cached on disk'),
        ('engine_reachable','integration engine on a private address')]
for key,label in labels:
    ok=bool(off.get(key))
    print(f\"    {'\u2713' if ok else '\u2717'} {label}\")
    if not ok:
        print(f\"      {off.get('detail',{}).get(key,'')}\")
if off.get('all_local'):
    print('    all_local: true' + (' (STRICT)' if off.get('strict') else ''))
else:
    print('    all_local: FALSE - not_local: ' + ', '.join(off.get('not_local',[])))
"

# -- 6. ready ----------------------------------------------------------------

echo
printf '\033[32mREADY - open http://localhost:%s - you can disconnect the network now\033[0m\n' "$PORT"
echo "  engine pid $ENGINE_PID (:$ENGINE_PORT)   app pid $APP_PID (:$PORT)"
echo "  logs $LOG_DIR   pids $PID_FILE"
echo "  stop with: ./scripts/stop-offline-demo.sh"
