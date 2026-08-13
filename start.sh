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
#   ./start.sh smoke        # Opt-in LIVE smoke (issue #3): real Anthropic distill + real
#                           # gpt-image-1 render. Needs both keys, never runs in CI. Fetches the
#                           # REAL, non-spending token estimate for the actual node FIRST and
#                           # shows it, then asks before spending — never a stale constant.
#                           # SYNAPSE_SMOKE_YES=1 bypasses the interactive [y/N] confirm for
#                           # non-interactive/agent runs that already reviewed the printed cost.
#                           # SYNAPSE_SMOKE_REPORTS_DIR overrides where the transcript lands
#                           # (default: the active sprint's reports/ dir).
#                           # Exit codes: 1 = actionable refusal (nothing spent), 2 = unknown
#                           # command, 3 = an HTTP call in the sequence failed or returned
#                           # something unexpected — money may already have moved; see the
#                           # transcript's FAILED section.
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
  echo "                              ${C_DIM}(SYNAPSE_SMOKE_YES=1 skips the interactive [y/N] confirm;${C_OFF}"
  echo "                              ${C_DIM} SYNAPSE_SMOKE_REPORTS_DIR overrides the transcript location)${C_OFF}"
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
#   - refuses against a SYNAPSE_MOCK_MODELS=1 backend (a zero-spend run must never be labeled
#     a live smoke)
#   - selects the node and fetches a genuinely non-spending token estimate (POST /distill
#     {dry_run: true} — never calls the summarizer) BEFORE consent, and PRINTS the real
#     tokens_est/threshold/requires_confirmation for THIS node — never a stale constant (a
#     contributor's most obvious next edit, "just show the config threshold", was exactly the
#     bug this closes: it let the operator consent blind to a node the script hadn't even
#     chosen yet). Only the free dry-run POST may happen before consent; the ONE paid distill
#     call (never two — `confirm: false` alone is NOT free: below the server's cost-guard
#     threshold it still summarizes for real) and the render call both wait for it.
#     SYNAPSE_SMOKE_YES=1 bypasses the interactive [y/N] for non-interactive/agent runs — it
#     counts as informed consent only because the real estimate is always printed first.
#   - talks to an ALREADY-RUNNING backend (./start.sh dev / service) — it does not manage its
#     own stack, so it never touches the app lifecycle
#   - records a transcript under the active sprint's reports/ dir (mktemp — collision-proof;
#     SYNAPSE_SMOKE_REPORTS_DIR overrides it). A failed or contract-violating HTTP call (bad
#     status, or 2xx missing an expected field) gets a diagnostic, a FAILED section in the
#     transcript with the safe response context, and the documented exit code 3 — never a bare
#     curl exit status, never the plain-refusal code 1 once money may have moved, and never a
#     later call in the sequence after an earlier failure
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

# Whitespace trim via parameter expansion only — NEVER xargs (issue #3 fix-loop F3): xargs
# applies shell quote/glob semantics to arbitrary file content, so a value like `don't` throws
# "unmatched single quote" to stderr on every run and gets silently blanked.
_smoke_trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

_SMOKE_ENV_LOADED=false
# Mirrors config.py's _load_dotenv EXACTLY: `line = raw.strip()` happens BEFORE the
# blank/comment test, so an INDENTED `  # comment` is a comment, never a KEY=VALUE line (F3 —
# the old `case "$line" in ""|"#"*)` tested the raw, unstripped line and mis-parsed an indented
# comment as `key="# ..."`, which then blew up bash's `${!key}` and killed the whole command).
# A real value already in the process env always wins; a placeholder in the process env never
# blocks the file's real value.
smoke_load_env_once() {
  $_SMOKE_ENV_LOADED && return 0
  _SMOKE_ENV_LOADED=true
  local f; f="$(smoke_env_file)"
  [ -f "$f" ] || return 0
  local raw line key value current
  # ASCII-explicit identifier guards, under a locale-independent LC_ALL=C for this loop
  # (delta-adversary D3): `[:alnum:]` is LOCALE-DEFINED — under a real UTF-8 locale a non-ASCII
  # byte like "Ä" IS alnum, so the old guard let it through and `${!key}` aborted the whole
  # command. Explicit `A-Za-z0-9` ranges plus `LC_ALL=C` make the match byte-value-based, so
  # the same input is skipped safely under every locale, not just the C/POSIX default the
  # shipped test's from-scratch env happened to run under.
  local _smoke_had_lc_all=false _smoke_saved_lc_all=""
  if [ -n "${LC_ALL+set}" ]; then _smoke_had_lc_all=true; _smoke_saved_lc_all="$LC_ALL"; fi
  LC_ALL=C
  while IFS= read -r raw || [ -n "$raw" ]; do
    line="$(_smoke_trim "$raw")"
    case "$line" in ""|"#"*) continue ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    key="$(_smoke_trim "${line%%=*}")"
    value="$(_smoke_trim "${line#*=}")"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    [ -z "$key" ] && continue
    # config.py accepts ANY non-empty string as an os.environ key (it's just a dict); bash
    # cannot export/expand a non-identifier name (`MY-VAR=1`, or any non-ASCII key) — skip it
    # safely rather than let `${!key}` abort the whole command under `set -e` (F3/D3).
    case "$key" in [A-Za-z_]*) : ;; *) continue ;; esac
    case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
    current="${!key:-}"
    if [ -z "$current" ] || smoke_is_placeholder "$current"; then
      export "$key=$value"
    fi
  done <"$f"
  if $_smoke_had_lc_all; then LC_ALL="$_smoke_saved_lc_all"; else unset LC_ALL; fi
}

smoke_key_present() {
  smoke_load_env_once
  local val="${!1:-}"
  ! smoke_is_placeholder "$val"
}

# Active sprint's reports dir, read from the sprint index graph node (never hardcoded — sprints
# close and a new one opens). Falls back to the newest sprint dir if the index can't be parsed.
# SYNAPSE_SMOKE_REPORTS_DIR overrides it (tests must never write a transcript into the tracked
# project tree — same override discipline as SYNAPSE_ENV_FILE).
smoke_reports_dir() {
  if [ -n "${SYNAPSE_SMOKE_REPORTS_DIR:-}" ]; then
    echo "$SYNAPSE_SMOKE_REPORTS_DIR"
    return 0
  fi
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

# Creates the transcript file LAZILY, on first use, and only once (issue #3 fix-loop E1) — a
# refused/declined run (nothing spent, no provider execution begun: CI/keys/backend/mock
# refusals, an empty node list, or a plain "no" at the consent prompt) must never litter the
# tracked reports dir with an orphan file that records nothing. The first caller that actually
# has something to write (a dry-run failure, or a genuinely consented run about to spend) is the
# one that creates it; every failure state from that point on (paid distill, missing
# summary_note_id, render) is guaranteed a transcript to record into (F4/D4 — evidence for
# successful AND post-spend-failed runs is never lost).
_SMOKE_TRANSCRIPT=""
smoke_ensure_transcript() {
  if [ -n "$_SMOKE_TRANSCRIPT" ]; then
    echo "$_SMOKE_TRANSCRIPT"
    return 0
  fi
  local reports_dir; reports_dir="$(smoke_reports_dir)"
  mkdir -p "$reports_dir"
  local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"
  # mktemp both creates the file AND guarantees a unique name (L3) — two runs in the same
  # second (a `date`-only name + `>`) would silently overwrite one transcript with the other.
  local t; t="$(mktemp "$reports_dir/live_smoke_${ts}_XXXXXX.md")" || return 1
  {
    echo "# Live smoke — $ts (issue #3)"
    echo ""
    echo "Backend: http://localhost:$PORT · model #1 ${SUMMARIZER_MODEL:-claude-sonnet-5} · model #2 ${IMAGE_MODEL:-gpt-image-1}"
    echo ""
  } >"$t"
  _SMOKE_TRANSCRIPT="$t"
  echo "$t"
}

# One safe HTTP JSON POST: captures the status code AND the body (NEVER `-f`, which discards
# the body on failure — issue #3 fix-loop F4). Sets SMOKE_LAST_STATUS/SMOKE_LAST_BODY; returns
# 0 for 2xx, 1 otherwise (a connection failure sets status "000", body empty) — it never aborts
# the script itself, so a provider failure gets a diagnostic + a transcript record instead of a
# bare, unexplained curl exit status (curl's own -f exit code, e.g. 22, meant nothing to anyone
# reading it and is not one of this command's own documented exit codes).
SMOKE_LAST_STATUS=""
SMOKE_LAST_BODY=""
smoke_post_json() {
  local url="$1" data="$2" tmp status
  tmp="$(mktemp)"
  status="$(curl -s -o "$tmp" -w '%{http_code}' -X POST "$url" -H 'Content-Type: application/json' -d "$data" 2>/dev/null)" || status="000"
  SMOKE_LAST_BODY="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  SMOKE_LAST_STATUS="$status"
  case "$status" in 2??) return 0 ;; esac
  return 1
}

# Appends a FAILED section to the transcript — the response context this command actually has
# (HTTP status + body; never a request payload, which is the only thing this command could
# leak on purpose). This is response-body content from the LOCAL backend, not a guarantee about
# what that backend chooses to put in an error body — see backend/modules/{distill,render}/src/
# api.py for what it actually returns (names a missing variable, never its value; unhandled
# exceptions surface as FastAPI's generic 500 with no detail). Whatever succeeded ABOVE this
# section in the transcript stays fully recorded (F4 — no silent truncation).
smoke_transcript_failure() {
  local transcript="$1" step="$2" status="$3" body="$4"
  {
    echo ""
    echo "## $step — FAILED (HTTP ${status:-unreachable})"
    echo '```json'
    echo "${body:-<no response body — connection failed>}"
    echo '```'
  } >>"$transcript"
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

  # 3b) Refuse a MOCKED backend (L1) — SYNAPSE_MOCK_MODELS=1 would make ZERO real provider
  # calls yet still print "Live smoke complete" and file a transcript headed with the real
  # model names, which is indistinguishable from a genuine live run except by reading the JSON
  # body's "model" field. Best-effort: an older backend without /api/v1/models/status is not
  # refused here — this is a safety net, not a new hard requirement on the backend.
  local mock_status; mock_status="$(curl -sf "http://localhost:$PORT/api/v1/models/status" 2>/dev/null || true)"
  if [ -n "$mock_status" ]; then
    local is_mock; is_mock="$(echo "$mock_status" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print('1' if d.get('mock') else '0')" 2>/dev/null || echo 0)"
    if [ "$is_mock" = "1" ]; then
      log "✖ Refusing: the running backend has SYNAPSE_MOCK_MODELS=1 — a run against it would"
      log "  make ZERO real provider calls yet still be labeled a live smoke."
      log "  Restart the backend WITHOUT SYNAPSE_MOCK_MODELS, then re-run ./start.sh smoke."
      exit 1
    fi
  fi

  # 4) Select the node — BEFORE consent (D1): the operator must see the estimate for the node
  # that will actually be distilled, not a generic promise. Nothing was spent and no transcript
  # exists yet if this refuses (E1 — no orphan file for the most likely first-run state: an
  # empty vault, nothing ingested).
  local node_id="${SYNAPSE_SMOKE_NODE_ID:-}"
  if [ -z "$node_id" ]; then
    node_id="$(curl -sf "http://localhost:$PORT/api/v1/graph" 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); ns=d.get('nodes',[]); print(ns[0]['id'] if ns else '')" 2>/dev/null || true)"
  fi
  if [ -z "$node_id" ]; then
    log "✖ Refusing: no node to distill — set SYNAPSE_SMOKE_NODE_ID, or configure SYNAPSE_SOURCE_REPOS and rebuild first."
    exit 1
  fi

  # 5) A REAL, NON-SPENDING estimate (dry_run: true — F1), fetched BEFORE consent (D1). The OLD
  # `confirm: false` call LOOKED free but was not: below the cost-guard threshold the server
  # runs the real summarizer regardless, so every run spent twice and the transcript mislabeled
  # a completed, paid summarization as "before spending". dry_run never reaches the summarizer,
  # so this is the ONLY network call this command ever makes before the operator has consented.
  # A FAILURE here is real diagnostic evidence (E1) — worth a transcript even though nothing was
  # spent; a clean refusal below (declined consent) is not, so the transcript is NOT created
  # until either this fails or consent is actually granted (smoke_ensure_transcript, lazy).
  log "Estimating cost for node '$node_id'... (non-spending)"
  if ! smoke_post_json "http://localhost:$PORT/api/v1/distill" \
      "{\"node_id\": \"$node_id\", \"scope\": \"node\", \"dry_run\": true}"; then
    local transcript; transcript="$(smoke_ensure_transcript)" || {
      log "✖ Distill estimate request failed AND could not create a transcript under $(smoke_reports_dir)"
      exit 3
    }
    smoke_transcript_failure "$transcript" "Distill — cost estimate" "$SMOKE_LAST_STATUS" "$SMOKE_LAST_BODY"
    log "✖ Distill estimate request failed (HTTP ${SMOKE_LAST_STATUS:-unreachable}) — see $transcript. No paid calls were made."
    exit 3
  fi
  local estimate="$SMOKE_LAST_BODY"
  log "  estimate: $estimate"

  # Parse the REAL numbers out of the estimate for the consent banner below — never re-derive
  # or guess them, and never fall back to the static SUMMARIZE_CONFIRM_THRESHOLD constant.
  local est_fields tokens_est_val threshold_val truncated_val requires_confirmation_val
  est_fields="$(echo "$estimate" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('tokens_est', '?'))
print(d.get('threshold', '?'))
print('yes' if d.get('truncated') else 'no')
print('yes' if d.get('requires_confirmation') else 'no')
" 2>/dev/null)" || est_fields=$'?\n?\nno\nno'
  { IFS= read -r tokens_est_val; IFS= read -r threshold_val; IFS= read -r truncated_val; \
    IFS= read -r requires_confirmation_val; } <<<"$est_fields"

  # Refuse rather than consent to a number that doesn't exist (N2) — a 2xx response with an
  # unparsable/non-object body would otherwise degrade the banner to "? tokens" and let
  # SYNAPSE_SMOKE_YES=1 spend blind. This is genuine evidence of a real problem — worth a
  # transcript, same as any other dry-run failure above.
  if [ "$tokens_est_val" = "?" ] || [ "$threshold_val" = "?" ]; then
    local transcript; transcript="$(smoke_ensure_transcript)" || {
      log "✖ Could not parse the cost estimate AND could not create a transcript under $(smoke_reports_dir)"
      exit 3
    }
    smoke_transcript_failure "$transcript" "Distill — cost estimate (unparsable)" "$SMOKE_LAST_STATUS" "$estimate"
    log "✖ Could not parse the cost estimate response — refusing rather than risk spending blind. See $transcript."
    exit 3
  fi

  # 6) Informed consent, BEFORE any paid call (D1) — honours requires_confirmation by SHOWING
  # it rather than silently overriding it: the operator (or the documented SYNAPSE_SMOKE_YES
  # bypass) decides with the real number in hand, not a config constant. SYNAPSE_SMOKE_YES=1
  # only counts as consent BECAUSE the estimate above is always printed first, interactive or
  # not — it is the ONLY way past this gate without a real TTY [y/N] answer.
  echo ""
  log "About to make REAL, PAID provider calls for node '$node_id':"
  echo "     Anthropic distill  ${C_DIM}model ${SUMMARIZER_MODEL:-claude-sonnet-5}${C_OFF}"
  echo "     ${C_B}Estimated cost:${C_OFF} ${tokens_est_val} tokens ${C_DIM}(cost-guard threshold: ${threshold_val})${C_OFF}"
  if [ "$requires_confirmation_val" = "yes" ]; then
    echo "     ${C_B}⚠ OVER the server's cost-guard threshold${C_OFF} — the server would normally require a second confirmation; this command is that confirmation."
  fi
  [ "$truncated_val" = "yes" ] && echo "     ${C_DIM}Note: the source set is truncated by the size cap — this estimate covers only what will actually be sent.${C_OFF}"
  echo "     OpenAI render      ${C_DIM}model ${IMAGE_MODEL:-gpt-image-1}; one image${C_OFF}"
  echo ""
  if [ "${SYNAPSE_SMOKE_YES:-}" != "1" ]; then
    if [ -t 0 ]; then
      printf "[start.sh] Proceed and spend real money? [y/N] "
      # `|| answer=""` (N1): EOF (Ctrl-D) makes `read` return nonzero — under `set -e` that used
      # to abort the script silently, one line after the prompt, with no "Aborted" message. EOF
      # now degrades to the same "empty answer" default-No path as pressing Enter.
      local answer; read -r answer || answer=""
      case "$answer" in [yY]*) ;; *) log "Aborted — no calls made."; exit 1 ;; esac
    else
      log "✖ Non-interactive session — set SYNAPSE_SMOKE_YES=1 to confirm the spend and proceed."
      exit 1
    fi
  fi

  # 7) Consent granted — NOW the transcript exists for the rest of this run (E1: lazy creation;
  # a declined run above never reaches this line, so it never created one). Records the
  # already-fetched, already-shown estimate first, then the ONE paid distill call — never a
  # second one (F1: no double-spend).
  local transcript; transcript="$(smoke_ensure_transcript)" || {
    log "✖ Could not create a transcript file under $(smoke_reports_dir)"
    exit 1
  }
  {
    echo "## Distill — node \`$node_id\` — cost estimate (before spending, zero-cost dry run)"
    echo '```json'
    echo "$estimate"
    echo '```'
  } >>"$transcript"

  log "1/2 Distill — node '$node_id'... (paid)"
  if ! smoke_post_json "http://localhost:$PORT/api/v1/distill" \
      "{\"node_id\": \"$node_id\", \"scope\": \"node\", \"confirm\": true}"; then
    smoke_transcript_failure "$transcript" "Distill — result" "$SMOKE_LAST_STATUS" "$SMOKE_LAST_BODY"
    log "✖ Distill request failed (HTTP ${SMOKE_LAST_STATUS:-unreachable}) — see $transcript. No render call was made."
    exit 3
  fi
  local distill_result="$SMOKE_LAST_BODY"
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
    # D4 — the paid call already spent (2xx); a missing summary_note_id is a backend contract
    # violation, not a refusal. This must NOT be the plain-refusal exit 1 (which the docs and
    # this file's own usage block reserve for "nothing spent") — it is exit 3, with a FAILED
    # transcript section, exactly like any other HTTP-layer failure in this sequence.
    smoke_transcript_failure "$transcript" "Distill — missing summary_note_id" "$SMOKE_LAST_STATUS" "$distill_result"
    log "✖ Distill returned HTTP $SMOKE_LAST_STATUS but no summary_note_id — see $transcript. No render call was made."
    exit 3
  fi

  log "2/2 Render — summary '$summary_id'..."
  if ! smoke_post_json "http://localhost:$PORT/api/v1/render" \
      "{\"summary_note_id\": \"$summary_id\"}"; then
    smoke_transcript_failure "$transcript" "Render" "$SMOKE_LAST_STATUS" "$SMOKE_LAST_BODY"
    log "✖ Render request failed (HTTP ${SMOKE_LAST_STATUS:-unreachable}) — see $transcript. The distill above already spent; this failure did not, and nothing further was attempted."
    exit 3
  fi
  local render_result="$SMOKE_LAST_BODY"
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
