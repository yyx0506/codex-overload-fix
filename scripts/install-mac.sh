#!/bin/bash
# Codex Overload Fix - macOS installer (universal edition).
#
# Registers relay/relay.py as a per-user LaunchAgent. No admin rights, and no
# watchdog: launchd restarts the process itself (KeepAlive), and a LaunchAgent
# has no console window to flash. None of the Windows SW_HIDE machinery is
# needed here.
#
# Works on Apple Silicon (M-series) and Intel Macs alike: the relay is pure
# stdlib Python with no compiled extensions, so the CPU architecture never
# comes into it. Any Python 3.9+ (the macOS-bundled one, brew's, or
# python.org's, universal or not) runs it.
#
# Two modes:
#   --mode direct    Codex -> relay(8787) -> chatgpt.com          (your account)
#   --mode gateway   Codex -> relay(8788) -> sub2api/cockpit-tools -> upstream
#                    needs --upstream, e.g. http://127.0.0.1:8888
#
# Verification status: exercised end-to-end on macOS in CI
# (.github/workflows/macos.yml) -- launchctl accepts the plist, the agent
# reports state = running, the relay listens, and uninstall removes it.
set -euo pipefail

MODE="direct"
PORT=""
UPSTREAM=""
API_KEY_ENV="SUB2API_API_KEY"
PROXY=""
PYTHON_PATH=""
STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
NO_AUTOSTART=0
DRY_RUN=0
LABEL="com.codex-overload-fix.relay"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!!] %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: install-mac.sh [options]

  --mode direct|gateway  direct: relay in front of chatgpt.com, your own
                         account (default). gateway: relay in front of an
                         account-pool gateway (sub2api, cockpit-tools, ...);
                         requires --upstream
  --upstream URL         gateway address, e.g. http://127.0.0.1:8888 or
                         https://sub2api.company.com; a trailing /v1 is fine
  --api-key-env NAME     env var Codex reads the gateway API key from
                         (gateway mode only, default SUB2API_API_KEY)
  --port PORT            local port for the relay (default 8787 direct /
                         8788 gateway)
  --proxy URL            upstream proxy, e.g. http://127.0.0.1:7890
  --python-path PATH     Use this interpreter instead of the first python3 on PATH
  --state-dir DIR        Where the log lives
  --no-autostart         Write the plist but do not load it now
  --dry-run              Print the plist and the commands; change nothing
  -h, --help             This message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)         [ $# -ge 2 ] || die "--mode needs a value";         MODE="$2"; shift 2 ;;
        --upstream)     [ $# -ge 2 ] || die "--upstream needs a value";     UPSTREAM="$2"; shift 2 ;;
        --api-key-env)  [ $# -ge 2 ] || die "--api-key-env needs a value";  API_KEY_ENV="$2"; shift 2 ;;
        --port)         [ $# -ge 2 ] || die "--port needs a value";         PORT="$2"; shift 2 ;;
        --proxy)        [ $# -ge 2 ] || die "--proxy needs a value";        PROXY="$2"; shift 2 ;;
        --python-path)  [ $# -ge 2 ] || die "--python-path needs a value";  PYTHON_PATH="$2"; shift 2 ;;
        --state-dir)    [ $# -ge 2 ] || die "--state-dir needs a value";    STATE_DIR="$2"; shift 2 ;;
        --no-autostart) NO_AUTOSTART=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option: $1 (try --help)" ;;
    esac
done

case "$MODE" in
    direct|gateway) ;;
    *) die "--mode must be 'direct' or 'gateway', got: $MODE" ;;
esac
if [ "$MODE" = "gateway" ] && [ -z "$UPSTREAM" ]; then
    die "--mode gateway needs --upstream, e.g. --upstream http://127.0.0.1:8888"
fi
[ -n "$PORT" ] || { [ "$MODE" = "gateway" ] && PORT=8788 || PORT=8787; }

case "$PORT" in
    ''|*[!0-9]*) die "port must be a number: $PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "port out of range: $PORT"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
RELAY="$ROOT/relay/relay.py"
[ -f "$RELAY" ] || die "relay/relay.py not found under $ROOT"

# ---------------------------------------------------------------- find python
if [ -z "$PYTHON_PATH" ]; then
    PYTHON_PATH="$(command -v python3 || true)"
fi
[ -n "$PYTHON_PATH" ] || die "python3 not found. Install Python 3.9+ (brew install python, or python.org) and re-run."
[ -x "$PYTHON_PATH" ] || die "not executable: $PYTHON_PATH"
"$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9+ required; $PYTHON_PATH is $("$PYTHON_PATH" --version 2>&1)"
ok "Python $("$PYTHON_PATH" -c 'import sys; print("%d.%d" % sys.version_info[:2])') at $PYTHON_PATH ($("$PYTHON_PATH" -c 'import platform; print(platform.machine())'))"

LOG_PATH="$STATE_DIR/relay.log"
OUT_PATH="$STATE_DIR/relay.out"   # launchd's stdout/stderr, kept apart from the relay's own log

port_open() {
    "$PYTHON_PATH" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.5):
        pass
except OSError:
    raise SystemExit(1)
PY
}

# Never put the relay on the gateway's own port.
if [ "$MODE" = "gateway" ]; then
    GUP_PORT="$(UPSTREAM="$UPSTREAM" "$PYTHON_PATH" - <<'PY'
import os, re
u = os.environ["UPSTREAM"]
m = re.search(r":(\d+)(/|$)", u)
print(m.group(1) if m else ("443" if u.startswith("https") else "80"))
PY
)"
    [ "$GUP_PORT" != "$PORT" ] || die "the relay port equals the gateway port ($PORT); pass --port to pick another"
fi

# ---------------------------------------------------------------- dry run
if [ "$DRY_RUN" -eq 1 ]; then
    say "mode:     $MODE"
    [ -n "$UPSTREAM" ] && say "upstream: $UPSTREAM"
    say "port:      $PORT"
    say "relay:     $RELAY"
    say "log:       $LOG_PATH"
    say "plist:     $PLIST_PATH"
    say "label:     $LABEL"
    say "proxy:     ${PROXY:-(none)}"
    say ""
    say "launchctl bootout gui/$UID/$LABEL  (ignore failure)"
    say "launchctl bootstrap gui/$UID $PLIST_PATH"
    say ""
    say "plist that would be written:"
    say "----------------------------------------"
    DRY=1 LABEL="$LABEL" PLIST_PATH="$PLIST_PATH" PY_PATH="$PYTHON_PATH" RELAY="$RELAY" \
        MODE="$MODE" UPSTREAM="$UPSTREAM" PORT="$PORT" LOG_PATH="$LOG_PATH" OUT_PATH="$OUT_PATH" STATE_DIR="$STATE_DIR" PROXY="$PROXY" \
        "$PYTHON_PATH" - <<'PY'
import os, plistlib
e = os.environ
args = [e["PY_PATH"], e["RELAY"], "--mode", e["MODE"], "--port", e["PORT"], "--log", e["LOG_PATH"]]
if e.get("UPSTREAM"):
    args += ["--upstream", e["UPSTREAM"]]
if e.get("PROXY"):
    args += ["--proxy", e["PROXY"]]
plist = {
    "Label": e["LABEL"],
    "ProgramArguments": args,
    "WorkingDirectory": e["STATE_DIR"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": e["OUT_PATH"],
    "StandardErrorPath": e["OUT_PATH"],
}
print(plistlib.dumps(plist).decode())
PY
    exit 0
fi

mkdir -p "$STATE_DIR" "$(dirname "$PLIST_PATH")"

# ---------------------------------------------------------------- write plist
LABEL="$LABEL" PLIST_PATH="$PLIST_PATH" PY_PATH="$PYTHON_PATH" RELAY="$RELAY" \
    MODE="$MODE" UPSTREAM="$UPSTREAM" PORT="$PORT" LOG_PATH="$LOG_PATH" OUT_PATH="$OUT_PATH" STATE_DIR="$STATE_DIR" PROXY="$PROXY" \
    "$PYTHON_PATH" - <<'PY'
import os, plistlib
e = os.environ
args = [e["PY_PATH"], e["RELAY"], "--mode", e["MODE"], "--port", e["PORT"], "--log", e["LOG_PATH"]]
if e.get("UPSTREAM"):
    args += ["--upstream", e["UPSTREAM"]]
if e.get("PROXY"):
    args += ["--proxy", e["PROXY"]]
plist = {
    "Label": e["LABEL"],
    "ProgramArguments": args,
    "WorkingDirectory": e["STATE_DIR"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "StandardOutPath": e["OUT_PATH"],
    "StandardErrorPath": e["OUT_PATH"],
}
with open(e["PLIST_PATH"], "wb") as fh:
    plistlib.dump(plist, fh)
PY
ok "plist      $PLIST_PATH"

# ---------------------------------------------------------------- load it
if [ "$NO_AUTOSTART" -eq 0 ]; then
    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1; then
        ok "launchctl bootstrap gui/$UID"
    else
        warn "launchctl bootstrap failed, trying the legacy loader"
        launchctl load -w "$PLIST_PATH" || die "could not load $PLIST_PATH"
        ok "launchctl load -w"
    fi

    # A cold start on a slow or freshly booted machine can take well over five
    # seconds: this waits up to 30.
    i=0
    while [ "$i" -lt 120 ]; do
        if port_open "$PORT"; then
            ok "listening on 127.0.0.1:$PORT"
            break
        fi
        i=$((i + 1))
        sleep 0.25
    done
    if [ "$i" -ge 120 ]; then
        warn "not listening after 30s - check $LOG_PATH"
        launchctl print "gui/$UID/$LABEL" 2>/dev/null | sed -n '1,5p' >&2 || true
    fi
else
    say "plist written; not loaded (--no-autostart)"
fi

# ---------------------------------------------------------------- next steps
if [ "$MODE" = "gateway" ]; then
    cat <<EOF

Add this to ~/.codex/config.toml, then restart Codex:

model_provider = "gateway-retry"

[model_providers.gateway-retry]
name = "Gateway (retry)"
base_url = "http://127.0.0.1:$PORT/v1"
wire_api = "responses"
env_key = "$API_KEY_ENV"
stream_max_retries = 20
request_max_retries = 10

The key itself is NOT stored anywhere by the installer: export it in your
shell profile so Codex can read it,

    export $API_KEY_ENV="sk-...your-gateway-key..."

Works on Apple Silicon and Intel Macs - the relay is pure Python.

Keep a backup of config.toml before editing.

Log:     $LOG_PATH
Stop:    bash scripts/uninstall-mac.sh

If you move or delete this folder, re-run the installer; launchd points at the
absolute path of relay/relay.py.
EOF
else
    cat <<EOF

Add this to ~/.codex/config.toml, then restart Codex:

model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:$PORT"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10

Keep a backup of config.toml before editing.

Log:     $LOG_PATH
Stop:    bash scripts/uninstall-mac.sh

If you move or delete this folder, re-run the installer; launchd points at the
absolute path of relay/relay.py.
EOF
fi
