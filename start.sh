#!/usr/bin/env bash
# SynaptixLabs project start script (Linux/macOS/CI)
# Generic scaffold — edit the CONFIGURATION section for your project.
# Based on AGENTS project start.sh patterns.
#
# Usage:
#   ./start.sh              # Default: full dev stack (backend --reload + frontend) — same as `dev --ui`
#   ./start.sh setup        # One-time/refresh: install deps, create .env, verify — sprint-1 ready
#   ./start.sh dev          # Local dev: backend only (--reload); auto-updates deps when they changed
#   ./start.sh dev --ui     # Local dev: backend + frontend
#   ./start.sh production   # Production server (Docker/CI — no reload, no frontend)
#   ./start.sh test         # Run tests
#   ./start.sh status|stop  # Health check / kill project processes
#   ./start.sh service install|start|stop|restart|status|logs|uninstall
#                           # run as an APP: supervised, restarts on crash, survives a closed terminal
#   ./start.sh help         # This help + URLs and links
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================================
# CONFIGURATION — Edit this section per project
# ============================================================================
PROJECT_NAME="SYNAPSE"
PROJECT_TAGLINE="A second brain for your repos"
REPO_URL="https://github.com/SynaptixLabs/Synapse"
ORG_URL="https://synaptixlabs.ai"
LICENSE_NAME="MIT"
BACKEND_TYPE="python"           # "python" | "node"
BACKEND_DIR="backend"           # "." for monolith
BACKEND_CMD="uvicorn app.main:app"  # Python entrypoint
RELOAD_DIRS="app modules"       # Space-separated --reload-dir targets (empty = watch all)
FRONTEND_DIR="frontend"         # matches the shipped skeleton; "" if no separate frontend
DEFAULT_PORT=8000
UI_PORT=5173
HEALTH_PATH="/health"
ENV_FILE=".env"
# ============================================================================

: "${PORT:=$DEFAULT_PORT}"
log() { echo "[start.sh] $*"; }

# colors (auto-off when not a terminal)
if [ -t 1 ]; then
  C_B=$'\033[1m'; C_CY=$'\033[36m'; C_GR=$'\033[32m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_B=""; C_CY=""; C_GR=""; C_DIM=""; C_OFF=""
fi

# Rich info block — printed after setup and under the dev banner. $1 = "running"|"next".
info_links() {
  local mode="${1:-next}"
  echo "   ${C_B}Local URLs${C_OFF}$([ "$mode" = "next" ] && echo " ${C_DIM}(after ./start.sh)${C_OFF}")"
  echo "     Frontend         ${C_CY}http://localhost:$UI_PORT${C_OFF}"
  echo "     API              ${C_CY}http://localhost:$PORT${C_OFF}"
  echo "     API docs         ${C_CY}http://localhost:$PORT/docs${C_OFF}"
  echo "     Health           ${C_CY}http://localhost:$PORT$HEALTH_PATH${C_OFF}"
  # WSL: the Windows→WSL localhost relay can silently break (even with localhostForwarding=true).
  # Offer the direct-IP fallback so a Windows browser always has a working URL.
  if grep -qi microsoft /proc/version 2>/dev/null; then
    local wsl_ip; wsl_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$wsl_ip" ] && echo "     ${C_DIM}From Windows, if localhost fails: http://$wsl_ip:$UI_PORT · http://$wsl_ip:$PORT (relay fix: wsl --shutdown)${C_OFF}"
  fi
  echo ""
  echo "   ${C_B}Read me first${C_OFF}"
  echo "     Constitution     AGENTS.md   ·   Router  .claude/00_INDEX.md"
  echo "     Sprint 1 entry   project-management/sprints/sprint_01/index.md"
  echo "     Drift guard      python3 scripts/check_adapters.py"
  echo ""
  echo "   ${C_B}Project${C_OFF}"
  echo "     GitHub           ${C_CY}$REPO_URL${C_OFF}"
  echo "     Web              ${C_CY}$ORG_URL${C_OFF}"
  echo "     License          $LICENSE_NAME (see LICENSE)"
}

find_python() {
  for p in "$SCRIPT_DIR/$BACKEND_DIR/.venv/bin/python" "$SCRIPT_DIR/$BACKEND_DIR/venv/bin/python"; do
    [ -x "$p" ] && echo "$p" && return
  done
  echo "python3"
}

kill_port() {
  # Must always return 0: under `set -e`, a nonzero return from the port-is-free
  # case would abort the whole script. lsof may return multiple PIDs → xargs.
  local pids; pids=$(lsof -ti :"$1" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    log "Port $1 in use by PID(s) $(echo $pids | tr '\n' ' ')— killing..."
    echo "$pids" | xargs -r kill -9 2>/dev/null || true
    sleep 1
  fi
}

# dev-mode only (set in cmd_dev): kill the background frontend when the backend exits
cleanup() { log "Shutting down..."; jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true; }

backend_dir() { [ "$BACKEND_DIR" = "." ] && echo "$SCRIPT_DIR" || echo "$SCRIPT_DIR/$BACKEND_DIR"; }

# ── Preflight — check prerequisites, offer consented installs ────────────────
# Layman-first: name exactly what's missing, show the exact command that fixes it,
# run NOTHING without an explicit yes, and re-verify afterwards.
# SYNAPSE_SKIP_PREFLIGHT=1 skips all checks.
PY_RANGE="3.11 – 3.13"

pick_python() {
  # newest supported interpreter on PATH (README: Python 3.11–3.13)
  local c
  for c in python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null \
      && { echo "$c"; return 0; }
  done
  return 1
}

node_ok() {
  # vite 7 floor: ^20.19 || >=22.12
  command -v node >/dev/null 2>&1 || return 1
  local v maj min
  v=$(node --version 2>/dev/null); v=${v#v}
  maj=${v%%.*}; min=$(echo "$v" | cut -d. -f2)
  case "$maj" in ''|*[!0-9]*) return 1 ;; esac
  [ "$maj" -ge 23 ] && return 0
  [ "$maj" -eq 22 ] && [ "${min:-0}" -ge 12 ] && return 0
  [ "$maj" -eq 20 ] && [ "${min:-0}" -ge 19 ] && return 0
  return 1
}

preflight() {
  [ "${SYNAPSE_SKIP_PREFLIGHT:-}" = "1" ] && return 0
  local need_node="${1:-true}" missing=() apt_pkgs=() need_nodesource=false

  # WSL: a clone under /mnt/<drive> works but is much slower than a clone inside WSL
  case "$SCRIPT_DIR" in /mnt/*)
    echo "   ${C_DIM}Tip: this clone lives on the Windows drive — it works, but a clone inside WSL (~/) is much faster.${C_OFF}" ;;
  esac

  local py=""
  py=$(pick_python) || true
  if [ -n "$py" ]; then
    log "${C_GR}✔${C_OFF} Python $("$py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') ($py)"
    if ! "$py" -c 'import ensurepip' >/dev/null 2>&1; then
      missing+=("the Python venv module — Debian/Ubuntu ships it as a separate package")
      apt_pkgs+=("${py##*/}-venv")
    fi
  else
    if command -v python3 >/dev/null 2>&1; then
      missing+=("a supported Python ($PY_RANGE — found $(python3 --version 2>&1))")
    else
      missing+=("Python $PY_RANGE")
    fi
    apt_pkgs+=("python3" "python3-venv")
  fi

  if [ "$need_node" = true ]; then
    if node_ok && command -v npm >/dev/null 2>&1; then
      log "${C_GR}✔${C_OFF} Node $(node --version) · npm $(npm --version 2>/dev/null)"
    else
      if command -v node >/dev/null 2>&1; then
        missing+=("a supported Node.js (20.19+ / 22.12+ — found $(node --version 2>/dev/null || echo '?'))")
      else
        missing+=("Node.js 20.19+ / 22.12+ (with npm) — needed for the explorer UI")
      fi
      need_nodesource=true
    fi
  fi

  [ ${#missing[@]} -eq 0 ] && return 0

  echo ""
  log "Missing prerequisites:"
  local m; for m in "${missing[@]}"; do echo "     ✖ $m"; done
  echo ""
  if [ "${_PREFLIGHT_RETRIED:-}" = "1" ]; then
    log "✖ Still missing after the install — open a NEW terminal and run ./start.sh again."
    exit 1
  fi

  # Build the install plan for THIS machine (shown in full before anything runs)
  local plan=()
  if command -v apt-get >/dev/null 2>&1; then
    # curl/lsof ride along: nodesource needs curl; status/stop use lsof
    command -v curl >/dev/null 2>&1 || apt_pkgs+=("curl")
    command -v lsof >/dev/null 2>&1 || apt_pkgs+=("lsof")
    [ ${#apt_pkgs[@]} -gt 0 ] && plan+=("sudo apt-get update -qq && sudo apt-get install -y ${apt_pkgs[*]}")
    if $need_nodesource; then
      plan+=("curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -")
      plan+=("sudo apt-get install -y nodejs")
    fi
  elif command -v brew >/dev/null 2>&1; then
    [ ${#apt_pkgs[@]} -gt 0 ] && plan+=("brew install python@3.12")
    $need_nodesource && plan+=("brew install node@22 && brew link --overwrite node@22")
  else
    log "No supported package manager found (apt-get / brew) — install manually, then re-run ./start.sh:"
    echo "     Python $PY_RANGE:  https://www.python.org/downloads/"
    echo "     Node.js 22 LTS:    https://nodejs.org/"
    exit 1
  fi

  log "These commands will fix it (nothing runs without your OK):"
  local p; for p in "${plan[@]}"; do echo "     ${C_CY}$p${C_OFF}"; done
  echo ""
  if [ ! -t 0 ]; then
    log "Non-interactive session — run the commands above yourself, then re-run ./start.sh"
    exit 1
  fi
  printf "[start.sh] Install now? [Y/n] "
  local answer; read -r answer
  case "$answer" in [nN]*) log "Skipped — run the commands above, then re-run ./start.sh"; exit 1 ;; esac
  for p in "${plan[@]}"; do
    log "Running: $p"
    bash -c "$p" || { log "✖ Install step failed — fix the error above, then re-run ./start.sh"; exit 1; }
  done

  _PREFLIGHT_RETRIED=1
  preflight "$need_node"   # honest re-verify — success prints the ✔ lines
}

# Background verifier for dev mode: report when the stack ACTUALLY answers,
# so a first-time user gets an explicit "it works, open this URL" (or a clear failure).
watch_stack() {
  local want_ui="$1" be_ok=false fe_ok=false i=0
  command -v curl >/dev/null 2>&1 || return 0
  while [ $i -lt 90 ]; do
    if ! $be_ok && curl -sf -o /dev/null "http://localhost:$PORT$HEALTH_PATH" 2>/dev/null; then
      be_ok=true
      echo "[start.sh] ${C_GR}✔ Backend is UP${C_OFF} — http://localhost:$PORT (docs: /docs)"
    fi
    if [ "$want_ui" = true ] && ! $fe_ok && curl -sf -o /dev/null "http://localhost:$UI_PORT" 2>/dev/null; then
      fe_ok=true
      echo "[start.sh] ${C_GR}✔ Explorer is UP${C_OFF} — open ${C_CY}http://localhost:$UI_PORT${C_OFF} in your browser"
    fi
    if $be_ok && { [ "$want_ui" != true ] || $fe_ok; }; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  $be_ok || echo "[start.sh] ✖ Backend did not answer on :$PORT within 90s — scroll up for the first error." >&2
  [ "$want_ui" = true ] && ! $fe_ok && echo "[start.sh] ✖ Explorer did not answer on :$UI_PORT within 90s — scroll up for the first error." >&2
  return 0
}

# Create/refresh the backend venv. Cheap when current: a stamp file tracks the last install,
# so deps re-install only when requirements.txt is newer (that's the "update" in update-the-env).
ensure_backend_env() {
  [ "$BACKEND_TYPE" = "python" ] || return 0
  local be_dir; be_dir="$(backend_dir)"
  local req="$be_dir/requirements.txt" venv="$be_dir/.venv"
  [ -f "$req" ] || return 0
  if [ ! -x "$venv/bin/python" ]; then
    log "Creating backend venv..."
    local sys_py; sys_py=$(pick_python) || sys_py=python3
    "$sys_py" -m venv "$venv" || { log "✖ venv creation failed — on Debian/Ubuntu: sudo apt-get install ${sys_py##*/}-venv"; exit 1; }
  fi
  local stamp="$venv/.deps-stamp"
  if [ ! -f "$stamp" ] || [ "$req" -nt "$stamp" ]; then
    log "Installing/updating backend deps (requirements.txt)..."
    "$venv/bin/pip" install -q --disable-pip-version-check -r "$req" \
      || { log "✖ pip install failed — fix the error above, then re-run ./start.sh"; exit 1; }
    touch "$stamp"
  fi
}

# Same idea for the frontend: npm install when node_modules is missing or package.json changed.
ensure_frontend_env() {
  [ -n "$FRONTEND_DIR" ] || return 0
  local fe_dir="$SCRIPT_DIR/$FRONTEND_DIR" pkg stamp
  pkg="$fe_dir/package.json"; stamp="$fe_dir/node_modules/.deps-stamp"
  [ -f "$pkg" ] || return 0
  if [ ! -d "$fe_dir/node_modules" ] || [ ! -f "$stamp" ] || [ "$pkg" -nt "$stamp" ]; then
    log "Installing/updating frontend deps (package.json)..."
    (cd "$fe_dir" && npm install --silent) \
      || { log "✖ npm install failed — fix the error above, then re-run ./start.sh"; exit 1; }
    touch "$stamp"
  fi
}

# ── Commands ──────────────────────────────────────────
cmd_help() {
  echo ""
  echo "  ${C_B}$PROJECT_NAME${C_OFF}  ${C_DIM}· $PROJECT_TAGLINE${C_OFF}"
  echo ""
  echo "   ${C_B}Commands${C_OFF}"
  echo "     ./start.sh              full dev stack: backend (--reload) + frontend  ${C_DIM}(default)${C_OFF}"
  echo "     ./start.sh setup        install/update deps, create .env, verify — sprint-1 ready"
  echo "     ./start.sh dev [--ui]   backend only, or backend + frontend"
  echo "     ./start.sh production   production server (Docker/CI — no reload)"
  echo "     ./start.sh test         run the test suite"
  echo "     ./start.sh smoke        live-model smoke (real \$: Anthropic distill + gpt-image render) — opt-in, needs keys, never in CI"
  echo "     ./start.sh status       ports + health   ·   ./start.sh stop"
  echo "     ./start.sh preflight    check prerequisites only (Python, Node) — offers installs"
  echo ""
  info_links next
  echo ""
  echo "   ${C_DIM}Windows: .\\start.cmd (same commands as flags: -Setup, -Test, -Status, -Stop, -Help)${C_OFF}"
  echo ""
}

cmd_setup() {
  log "Setting up / updating the environment..."
  preflight true
  ensure_backend_env
  ensure_frontend_env
  # .env from the example (never overwrites an existing one)
  local env_dst; env_dst="$(backend_dir)/$ENV_FILE"
  if [ ! -f "$env_dst" ] && [ -f "$SCRIPT_DIR/.env.example" ]; then
    cp "$SCRIPT_DIR/.env.example" "$env_dst"
    log "Created $env_dst from .env.example — fill in real values."
  fi
  # verify: agent layer consistent + tests green (evidence, not assertion)
  [ -f "$SCRIPT_DIR/scripts/check_adapters.py" ] && python3 "$SCRIPT_DIR/scripts/check_adapters.py" "$SCRIPT_DIR"
  if [ "$BACKEND_TYPE" = "python" ]; then
    (cd "$(backend_dir)" && "$(find_python)" -m pytest -q) || log "WARNING: tests not green — fix before starting sprint work."
  fi
  echo ""
  echo "  ${C_CY}════════════════════════════════════════════════════${C_OFF}"
  echo "   ${C_B}$PROJECT_NAME${C_OFF}  ${C_DIM}· $PROJECT_TAGLINE${C_OFF}"
  echo "  ${C_CY}────────────────────────────────────────────────────${C_OFF}"
  echo "   ${C_GR}✔ Environment ready — you're set for sprint 1.${C_OFF}"
  echo ""
  echo "   ${C_B}Run it${C_OFF}"
  echo "     ./start.sh             dev: backend + frontend (default)"
  echo "     ./start.sh test        run the test suite"
  echo "     ./start.sh status      health check   ·   ./start.sh stop   ·   ./start.sh help"
  echo ""
  info_links next
  echo "  ${C_CY}════════════════════════════════════════════════════${C_OFF}"
  echo ""
  exit 0
}

cmd_stop() { kill_port "$PORT"; [ -n "$FRONTEND_DIR" ] && kill_port "$UI_PORT"; log "Done."; exit 0; }

cmd_status() {
  local be_up=false fe_up=false
  lsof -ti :"$PORT" >/dev/null 2>&1 && be_up=true
  [ -n "$FRONTEND_DIR" ] && lsof -ti :"$UI_PORT" >/dev/null 2>&1 && fe_up=true
  log "Backend  (port $PORT):   $($be_up && echo 'UP' || echo 'DOWN')"
  [ -n "$FRONTEND_DIR" ] && log "Frontend (port $UI_PORT): $($fe_up && echo 'UP' || echo 'DOWN')"
  if $be_up; then
    local health; health=$(curl -sf "http://localhost:$PORT$HEALTH_PATH" 2>/dev/null)
    if [ -n "$health" ]; then
      log "Health: $(echo "$health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d.get('status','?')} | build={d.get('build_stamp','?')}\")" 2>/dev/null || echo "$health")"
    else
      log "Health: endpoint unreachable"
    fi
  fi
  exit 0
}

cmd_test() {
  preflight false
  local PY; PY="$(find_python)"
  if [ "$BACKEND_TYPE" = "python" ]; then
    cd "$SCRIPT_DIR/$BACKEND_DIR"
    "$PY" -m pytest -v --tb=short || {
      rc=$?
      [ "$rc" -eq 5 ] && log "pytest collected no tests — the template ships none; add yours under $BACKEND_DIR/."
      exit "$rc"
    }
  else
    cd "$SCRIPT_DIR" && npm test
  fi
}

# ── smoke: wrap the two opt-in live-model smokes (issue #3, backlog #9) ─────
#
# The two paid-model flows — real Anthropic distill (backend/modules/distill/README.md) and real
# gpt-image-1 render (backend/modules/render/README.md) — were runbook steps run by hand
# (sprint-03 epic reports). This wraps them as one command, which is now the opt-in gate itself
# (you must invoke it, and it asks before spending) — with the safety contract as the
# load-bearing part:
#   - never runs in CI, no matter what
#   - refuses without both keys, actionably, exit 1 (never the unknown-command exit 2)
#   - shows the spend estimate and asks before making a paid call
#   - talks to an ALREADY-RUNNING backend (./start.sh dev / service) — it does not manage its
#     own stack, so it never touches the app lifecycle
#   - records a transcript under the active sprint's reports/ dir
smoke_is_ci() {
  case "${CI:-}" in true | TRUE | True | 1) return 0 ;; esac
  [ -n "${GITHUB_ACTIONS:-}" ] && return 0
  return 1
}

# The same env file the backend itself reads (app/core/config.py) — honors SYNAPSE_ENV_FILE so
# tests/scratch stacks never touch a real backend/.env.
smoke_env_file() {
  if [ -n "${SYNAPSE_ENV_FILE:-}" ]; then
    echo "$SYNAPSE_ENV_FILE"
  else
    echo "$(backend_dir)/$ENV_FILE"
  fi
}

smoke_is_placeholder() {
  case "$1" in "" | *REPLACE-ME*) return 0 ;; esac
  return 1
}

_SMOKE_ENV_LOADED=false
# Mirrors config.py's _load_dotenv: a real value already in the process env always wins; a
# placeholder in the process env never blocks the file's real value.
smoke_load_env_once() {
  $_SMOKE_ENV_LOADED && return 0
  _SMOKE_ENV_LOADED=true
  local f; f="$(smoke_env_file)"
  [ -f "$f" ] || return 0
  local line key value current
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ""|"#"*) continue ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    key="${line%%=*}"; value="${line#*=}"
    key="$(echo "$key" | xargs)"
    value="$(echo "$value" | xargs)"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    [ -z "$key" ] && continue
    current="${!key:-}"
    if [ -z "$current" ] || smoke_is_placeholder "$current"; then
      export "$key=$value"
    fi
  done <"$f"
}

smoke_key_present() {
  smoke_load_env_once
  local val="${!1:-}"
  ! smoke_is_placeholder "$val"
}

# Active sprint's reports dir, read from the sprint index graph node (never hardcoded — sprints
# close and a new one opens). Falls back to the newest sprint dir if the index can't be parsed.
smoke_reports_dir() {
  local idx="$SCRIPT_DIR/project-management/sprints/00_index.md" sprint=""
  if [ -f "$idx" ]; then
    sprint="$(grep -m1 -E 'OPEN' "$idx" 2>/dev/null | grep -oE 'sprint_[0-9]+' | head -1 || true)"
  fi
  if [ -z "$sprint" ]; then
    sprint="$(find "$SCRIPT_DIR/project-management/sprints" -maxdepth 1 -type d -name 'sprint_*' 2>/dev/null \
      | sort | tail -1 | xargs -r basename)"
  fi
  echo "$SCRIPT_DIR/project-management/sprints/${sprint:-sprint_06}/reports"
}

cmd_smoke() {
  # 1) CI refusal — unconditional, checked first, regardless of keys.
  if smoke_is_ci; then
    log "✖ Refusing: live-model smokes never run in CI (\$CI/\$GITHUB_ACTIONS detected)."
    log "  These make real, paid calls to Anthropic and OpenAI — see backend/modules/{distill,render}/README.md."
    exit 1
  fi

  # 2) Keyless refusal — actionable, exit 1 (distinct from the unknown-command exit 2).
  local missing=()
  smoke_key_present ANTHROPIC_API_KEY || missing+=("ANTHROPIC_API_KEY (Anthropic distill)")
  smoke_key_present OPENAI_API_KEY || missing+=("OPENAI_API_KEY (OpenAI gpt-image-1 render)")
  if [ ${#missing[@]} -gt 0 ]; then
    log "✖ Refusing: the live smoke needs real provider keys — missing:"
    local m; for m in "${missing[@]}"; do echo "     ✖ $m"; done
    log "  Set them in $(smoke_env_file) (see .env.example), then re-run ./start.sh smoke."
    exit 1
  fi

  # 3) Needs an already-running backend — this command never manages the app lifecycle.
  if ! curl -sf -o /dev/null "http://localhost:$PORT$HEALTH_PATH" 2>/dev/null; then
    log "✖ Refusing: no backend answering on http://localhost:$PORT$HEALTH_PATH"
    log "  Start one first — ./start.sh dev (or ./start.sh service start) — then re-run ./start.sh smoke."
    exit 1
  fi

  # 4) Spend estimate + explicit confirmation, BEFORE any paid call.
  echo ""
  log "About to make REAL, PAID provider calls:"
  echo "     Anthropic distill  ${C_DIM}model ${SUMMARIZER_MODEL:-claude-sonnet-5}; server-side cost guard at${C_OFF} ${SUMMARIZE_CONFIRM_THRESHOLD:-20000} ${C_DIM}est. tokens (SUMMARIZE_CONFIRM_THRESHOLD)${C_OFF}"
  echo "     OpenAI render      ${C_DIM}model ${IMAGE_MODEL:-gpt-image-1}; one image${C_OFF}"
  echo ""
  if [ "${SYNAPSE_SMOKE_YES:-}" != "1" ]; then
    if [ -t 0 ]; then
      printf "[start.sh] Proceed and spend real money? [y/N] "
      local answer; read -r answer
      case "$answer" in [yY]*) ;; *) log "Aborted — no calls made."; exit 1 ;; esac
    else
      log "✖ Non-interactive session — set SYNAPSE_SMOKE_YES=1 to confirm the spend and proceed."
      exit 1
    fi
  fi

  # 5) Run the two live smokes against the running backend; transcript every request/response.
  local reports_dir; reports_dir="$(smoke_reports_dir)"
  mkdir -p "$reports_dir"
  local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local transcript="$reports_dir/live_smoke_${ts}.md"
  {
    echo "# Live smoke — $ts (issue #3)"
    echo ""
    echo "Backend: http://localhost:$PORT · model #1 ${SUMMARIZER_MODEL:-claude-sonnet-5} · model #2 ${IMAGE_MODEL:-gpt-image-1}"
    echo ""
  } >"$transcript"

  local node_id="${SYNAPSE_SMOKE_NODE_ID:-}"
  if [ -z "$node_id" ]; then
    node_id="$(curl -sf "http://localhost:$PORT/api/v1/graph" 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); ns=d.get('nodes',[]); print(ns[0]['id'] if ns else '')" 2>/dev/null || true)"
  fi
  if [ -z "$node_id" ]; then
    log "✖ Refusing: no node to distill — set SYNAPSE_SMOKE_NODE_ID, or configure SYNAPSE_SOURCE_REPOS and rebuild first."
    exit 1
  fi

  log "1/2 Distill — node '$node_id'..."
  local estimate; estimate="$(curl -sf -X POST "http://localhost:$PORT/api/v1/distill" \
    -H 'Content-Type: application/json' \
    -d "{\"node_id\": \"$node_id\", \"scope\": \"node\", \"confirm\": false}")"
  {
    echo "## Distill — node \`$node_id\` — cost estimate (before spending)"
    echo '```json'
    echo "$estimate"
    echo '```'
  } >>"$transcript"
  log "  estimate: $estimate"

  local distill_result; distill_result="$(curl -sf -X POST "http://localhost:$PORT/api/v1/distill" \
    -H 'Content-Type: application/json' \
    -d "{\"node_id\": \"$node_id\", \"scope\": \"node\", \"confirm\": true}")"
  {
    echo ""
    echo "## Distill — result"
    echo '```json'
    echo "$distill_result"
    echo '```'
  } >>"$transcript"

  local summary_id; summary_id="$(echo "$distill_result" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('summary_note_id',''))" 2>/dev/null || true)"
  if [ -z "$summary_id" ]; then
    log "✖ Distill did not return a summary note — see $transcript for the raw response. Render skipped."
    exit 1
  fi

  log "2/2 Render — summary '$summary_id'..."
  local render_result; render_result="$(curl -sf -X POST "http://localhost:$PORT/api/v1/render" \
    -H 'Content-Type: application/json' \
    -d "{\"summary_note_id\": \"$summary_id\"}")"
  {
    echo ""
    echo "## Render — result"
    echo '```json'
    echo "$render_result"
    echo '```'
  } >>"$transcript"

  log "${C_GR}✔${C_OFF} Live smoke complete — transcript: $transcript"
}

# ── service mode (sprint 06, Epic Q — "run as an APP and not die") ───────────
#
# `./start.sh dev` runs uvicorn in the FOREGROUND with a `trap cleanup EXIT`, and the frontend as
# a background job of that same shell. Close the terminal — or let any wrapper's timeout fire —
# and both die. That is the fragility this mode removes; it does not replace dev mode, which stays
# exactly as it was.
#
# Two systemd --user units, so a crashed backend does not take the UI with it and each restarts on
# its own. No --reload here: a reloader under a supervisor is two supervisors disagreeing about
# who owns the process.
#
# NOT YET DECIDED (sprint 06, D3): the bind address. The API is unauthenticated by design and
# currently binds 0.0.0.0, which is LAN-reachable. This mode leaves that default UNCHANGED so it
# does not pre-empt the ruling, but it prints the fact every time you install, and honours
# SYNAPSE_BIND if you want loopback today.
SERVICE_API="synapse-api"
SERVICE_WEB="synapse-web"
UNIT_DIR="$HOME/.config/systemd/user"
: "${SYNAPSE_BIND:=0.0.0.0}"

svc_write_units() {
  local be_dir="$SCRIPT_DIR/$BACKEND_DIR"; [ "$BACKEND_DIR" = "." ] && be_dir="$SCRIPT_DIR"
  local PY; PY="$(find_python)"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$SERVICE_API.service" <<UNIT
[Unit]
Description=SYNAPSE API (generated by start.sh — edit start.sh, not this file)
After=network.target

[Service]
Type=simple
WorkingDirectory=$be_dir
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PY -m $BACKEND_CMD --host $SYNAPSE_BIND --port $PORT
Restart=on-failure
RestartSec=3
# Do not retry forever on a config error that will never resolve itself.
StartLimitIntervalSec=120
StartLimitBurst=5

[Install]
WantedBy=default.target
UNIT
  if [ -n "$FRONTEND_DIR" ]; then
    cat > "$UNIT_DIR/$SERVICE_WEB.service" <<UNIT
[Unit]
Description=SYNAPSE explorer (generated by start.sh — edit start.sh, not this file)
After=$SERVICE_API.service

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR/$FRONTEND_DIR
ExecStart=$(command -v npx) vite --port $UI_PORT --host --clearScreen false --logLevel warn
Restart=on-failure
RestartSec=3
StartLimitIntervalSec=120
StartLimitBurst=5

[Install]
WantedBy=default.target
UNIT
  fi
  systemctl --user daemon-reload
}

# Q2 — a reserved port, not a grabbed one. `dev` kills whatever holds the port; a service that
# does that could kill an unrelated process at boot, so this one refuses instead.
svc_port_free_or_die() {
  local port="$1" name="$2" pids
  pids="$(lsof -ti :"$port" 2>/dev/null || true)"
  [ -z "$pids" ] && return 0
  # Our own unit holding it is fine — that is a restart, not a conflict.
  if systemctl --user is-active --quiet "$name" 2>/dev/null; then return 0; fi
  log "REFUSING to start: port $port is held by PID(s) $(echo "$pids" | tr '\n' ' ')"
  log "  This mode never kills the occupant — that is what './start.sh stop' is for."
  exit 1
}

cmd_service() {
  local action="${1:-status}"
  case "$action" in
    install)
      preflight "$([ -n "$FRONTEND_DIR" ] && echo true || echo false)"
      [ "$BACKEND_TYPE" = "python" ] && ensure_backend_env
      [ -n "$FRONTEND_DIR" ] && ensure_frontend_env
      svc_port_free_or_die "$PORT" "$SERVICE_API"
      [ -n "$FRONTEND_DIR" ] && svc_port_free_or_die "$UI_PORT" "$SERVICE_WEB"
      svc_write_units
      systemctl --user enable --now "$SERVICE_API" ${FRONTEND_DIR:+"$SERVICE_WEB"}
      echo ""
      log "Installed and started. It now survives a closed terminal."
      echo "   ${C_B}Disarm at any time:${C_OFF}  ./start.sh service uninstall"
      echo "   ${C_DIM}(that is: systemctl --user disable --now $SERVICE_API $SERVICE_WEB)${C_OFF}"
      echo ""
      log "Survives logout/WSL restart only with lingering ON — it is OFF by default and this"
      log "  script will not enable it for you:   loginctl enable-linger \$USER"
      log "Surviving a WINDOWS reboot needs a Task Scheduler entry (sprint 06 Q5, pending D3)."
      echo ""
      log "⚠ The API is UNAUTHENTICATED by design and is bound to $SYNAPSE_BIND."
      [ "$SYNAPSE_BIND" = "0.0.0.0" ] && \
        log "  0.0.0.0 means reachable from your LAN. Loopback today: SYNAPSE_BIND=127.0.0.1 ./start.sh service install"
      ;;
    start)     systemctl --user start "$SERVICE_API" ${FRONTEND_DIR:+"$SERVICE_WEB"}; log "Started." ;;
    stop)      systemctl --user stop "$SERVICE_API" ${FRONTEND_DIR:+"$SERVICE_WEB"};  log "Stopped (still enabled — use uninstall to disarm)." ;;
    restart)   systemctl --user restart "$SERVICE_API" ${FRONTEND_DIR:+"$SERVICE_WEB"}; log "Restarted." ;;
    logs)      shift; journalctl --user -u "$SERVICE_API" -u "$SERVICE_WEB" -n "${1:-50}" --no-pager ;;
    uninstall)
      systemctl --user disable --now "$SERVICE_API" ${FRONTEND_DIR:+"$SERVICE_WEB"} 2>/dev/null || true
      rm -f "$UNIT_DIR/$SERVICE_API.service" "$UNIT_DIR/$SERVICE_WEB.service"
      systemctl --user daemon-reload
      log "Disarmed and removed. Nothing of SYNAPSE starts on its own any more."
      ;;
    status|*)
      local any=false
      for u in "$SERVICE_API" "$SERVICE_WEB"; do
        [ -f "$UNIT_DIR/$u.service" ] || continue
        any=true
        log "$u: $(systemctl --user is-active "$u" 2>&1) · $(systemctl --user is-enabled "$u" 2>&1)"
      done
      $any || log "No service installed — this is a foreground-only stack (./start.sh)."
      log "Lingering (survives logout): $(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
      ;;
  esac
  exit 0
}

cmd_production() {
  local be_dir="$SCRIPT_DIR/$BACKEND_DIR"
  [ "$BACKEND_DIR" = "." ] && be_dir="$SCRIPT_DIR"
  cd "$be_dir"
  if [ "$BACKEND_TYPE" = "python" ]; then
    ensure_backend_env
    local PY; PY="$(find_python)"
    log "Starting $BACKEND_CMD on 0.0.0.0:${PORT} (production)"
    exec "$PY" -m $BACKEND_CMD --host 0.0.0.0 --port "${PORT}"
  else
    npm run build && exec npm run start
  fi
}

cmd_dev() {
  trap cleanup EXIT   # dev runs a background frontend job; reap it on exit
  local with_ui=false
  [[ "${1:-}" == "--ui" ]] && with_ui=true
  preflight "$with_ui"

  local PY=""
  if [ "$BACKEND_TYPE" = "python" ]; then
    ensure_backend_env
    PY="$(find_python)"
  fi

  # Kill stale, clean caches
  kill_port "$PORT"
  $with_ui && kill_port "$UI_PORT"
  local be_dir="$SCRIPT_DIR/$BACKEND_DIR"
  [ "$BACKEND_DIR" = "." ] && be_dir="$SCRIPT_DIR"
  if [ "$BACKEND_TYPE" = "python" ]; then
    export PYTHONDONTWRITEBYTECODE=1
    find "$be_dir" -type d -name __pycache__ -not -path '*/.venv/*' -exec rm -rf {} + 2>/dev/null || true
  fi

  # Build stamp (shown in the banner)
  BUILD_STAMP=$(date "+%Y-%m-%d_%H:%M:%S"); export BUILD_STAMP

  # Start frontend in background
  if $with_ui && [ -n "$FRONTEND_DIR" ]; then
    local fe_dir="$SCRIPT_DIR/$FRONTEND_DIR"
    ensure_frontend_env
    (cd "$fe_dir" && npx vite --port "$UI_PORT" --host --clearScreen false --logLevel warn) &
  fi

  # Banner
  echo ""
  echo "  ${C_CY}════════════════════════════════════════════════════${C_OFF}"
  echo "   ${C_B}$PROJECT_NAME${C_OFF}  ${C_DIM}· $PROJECT_TAGLINE${C_OFF}"
  echo "  ${C_CY}────────────────────────────────────────────────────${C_OFF}"
  echo "   Build $BUILD_STAMP   ·   ${C_B}Ctrl+C to stop${C_OFF}"
  echo ""
  if $with_ui; then
    info_links running
  else
    echo "   API   ${C_CY}http://localhost:$PORT${C_OFF}   docs ${C_CY}/docs${C_OFF}   health ${C_CY}$HEALTH_PATH${C_OFF}"
    echo "   ${C_DIM}(frontend not started — use ./start.sh dev --ui)${C_OFF}"
  fi
  echo "  ${C_CY}════════════════════════════════════════════════════${C_OFF}"
  echo ""

  ( watch_stack "$with_ui" ) &   # prints "✔ … is UP" when the stack actually answers

  cd "$be_dir"
  if [ "$BACKEND_TYPE" = "python" ]; then
    local reload_args="--reload"
    for rd in $RELOAD_DIRS; do
      [ -n "$rd" ] && reload_args="$reload_args --reload-dir $rd"
    done
    "$PY" -m $BACKEND_CMD --host 0.0.0.0 --port "${PORT}" $reload_args
  else
    PORT="$PORT" npm run dev
  fi
}

# ── Main dispatch ─────────────────────────────────────
case "${1:-}" in
  "")             cmd_dev --ui ;;     # bare `./start.sh` = full dev stack (parity with .\start.ps1)
  setup)          cmd_setup ;;
  stop)           cmd_stop ;;
  status)         cmd_status ;;
  service)        shift; cmd_service "$@" ;;
  test)           shift; cmd_test "$@" ;;
  smoke)          cmd_smoke ;;
  dev)            shift; cmd_dev "$@" ;;
  preflight)      preflight true && log "All prerequisites OK."; exit 0 ;;
  production)     preflight false; cmd_production ;;
  help|-h|--help) cmd_help; exit 0 ;;
  *)              log "Unknown command: '$1'"; cmd_help; exit 2 ;;
esac
