#!/bin/bash
# Codex Overload Fix - Linux installer.
#
# Registers relay/relay.py as a systemd --user service. This is the Linux
# counterpart of install-mac.sh: no root, no console window, and systemd
# restarts the process itself (Restart=always), so there is no watchdog script
# on this platform either.
#
# Options mirror install-mac.sh. --linger is the one addition: launchd on macOS
# starts a LaunchAgent at login automatically, while a systemd --user unit is
# stopped when the last session of that user ends unless lingering is enabled.
set -euo pipefail

PORT=""
MODE="direct"
UPSTREAM=""
API_KEY_ENV="SUB2API_API_KEY"
PROXY=""
PYTHON_PATH=""
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/codex-overload-fix"
NO_AUTOSTART=0
DRY_RUN=0
LINGER=0
SERVICE="codex-overload-fix"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$SERVICE.service"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!!] %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: install-linux.sh [options]

  --mode direct|gateway  direct: relay in front of chatgpt.com, your own
                         account (default). gateway: relay in front of an
                         account-pool gateway (sub2api, cockpit-tools, ...);
                         requires --upstream
  --upstream URL         gateway address, e.g. http://127.0.0.1:8888 or
                         https://sub2api.company.com; a trailing /v1 is fine
  --api-key-env NAME     env var Codex reads the gateway API key from
                         (gateway mode only, default SUB2API_API_KEY)
  --port PORT            Local port for the relay (default 8787 direct /
                         8788 gateway)
  --proxy URL            Upstream proxy, e.g. http://127.0.0.1:7890
  --python-path PATH     Use this interpreter instead of the first python3 on PATH
  --state-dir DIR        Where the log lives
  --linger               Enable lingering so the relay survives logout
  --no-autostart         Write the unit but do not start or enable it
  --dry-run              Print the unit and the commands; change nothing
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
        --linger)       LINGER=1; shift ;;
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
[ -n "$PYTHON_PATH" ] || die "python3 not found. Install Python 3.9+ (apt install python3, dnf install python3, or python.org) and re-run."
[ -x "$PYTHON_PATH" ] || die "not executable: $PYTHON_PATH"
"$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9+ required; $PYTHON_PATH is $("$PYTHON_PATH" --version 2>&1)"
ok "Python $("$PYTHON_PATH" -c 'import sys; print("%d.%d" % sys.version_info[:2])') at $PYTHON_PATH"

LOG_PATH="$STATE_DIR/relay.log"

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

# The argv systemd will run. Kept in one place so --dry-run, the written unit
# and CI all exercise exactly the same command line.
relay_argv() {
    ARGV=("$PYTHON_PATH" "$RELAY" --mode "$MODE" --port "$PORT" --log "$LOG_PATH"
          --max-attempts 30 --backoff-base 1 --backoff-max 8
          --total-budget 300 --commit-deadline 120)
    if [ -n "$UPSTREAM" ]; then
        ARGV+=(--upstream "$UPSTREAM")
    fi
    if [ -n "$PROXY" ]; then
        ARGV+=(--proxy "$PROXY")
    fi
}

quote_argv() {
    local out="" arg
    for arg in "${ARGV[@]}"; do
        # systemd's own quoting: leave ordinary tokens bare, quote the rest.
        case "$arg" in
            *[!A-Za-z0-9_@%+=:,./-]*|'') out+="\"${arg//\"/\\\"}\" " ;;
            *) out+="$arg " ;;
        esac
    done
    printf '%s' "${out% }"
}

emit_unit() {
    relay_argv
    cat <<EOF
[Unit]
Description=Codex overload retry relay
Documentation=https://github.com/NewBeeM4n/codex-overload-fix
# No After=/Wants=network-online.target here. That target belongs to the system
# manager; a --user manager does not pull it in and has no equivalent, so the
# lines would be inert while looking like real ordering. Restart=always is what
# actually covers a network that is not up yet.

[Service]
Type=simple
ExecStart=$(quote_argv)
WorkingDirectory=$STATE_DIR
Restart=always
# 10 s matches ThrottleInterval on macOS. If something else already holds the
# port - a hand-started relay, say - this keeps the crash loop at one line per
# 10 s instead of one per 3 s.
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
}

# ---------------------------------------------------------------- dry run
if [ "$DRY_RUN" -eq 1 ]; then
    say "mode:      $MODE"
    [ -n "$UPSTREAM" ] && say "upstream:  $UPSTREAM"
    say "port:      $PORT"
    say "relay:     $RELAY"
    say "log:       $LOG_PATH"
    say "unit:      $UNIT_PATH"
    say "service:   $SERVICE"
    say "proxy:     ${PROXY:-(none)}"
    say ""
    say "systemctl --user daemon-reload"
    say "systemctl --user enable $SERVICE"
    say "systemctl --user restart $SERVICE"
    [ "$LINGER" -eq 1 ] && say "loginctl enable-linger $USER"
    say ""
    say "unit that would be written:"
    say "----------------------------------------"
    emit_unit
    exit 0
fi

mkdir -p "$STATE_DIR" "$UNIT_DIR"

# ---------------------------------------------------------------- write unit
emit_unit > "$UNIT_PATH"
ok "unit       $UNIT_PATH"

# ---------------------------------------------------------------- load it
# SYSTEMD_OK records whether we could actually hand this to systemd. It is a
# flag, not an early exit: the config.toml block at the end is the next step the
# user needs either way, and a machine that cannot use systemd is exactly the
# machine that most needs to be told how to point Codex at the relay.
SYSTEMD_OK=1
# Probed even under --no-autostart: the closing block asks the same question, and
# on a no-systemd box it must print the start command rather than a systemctl
# line the user cannot run.
if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    SYSTEMD_OK=0
    if [ "$NO_AUTOSTART" -eq 0 ]; then
        warn "systemd --user is not available on this machine"
        warn "the unit is written; start it by hand with:"
        warn "  $(quote_argv)"
    fi
fi

if [ "$NO_AUTOSTART" -eq 0 ] && [ "$SYSTEMD_OK" -eq 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE" >/dev/null
    # restart, not start: re-running the installer has to pick up a changed unit
    systemctl --user restart "$SERVICE"
    ok "systemctl --user enable + restart"

    # A cold start on a slow or freshly booted machine can take well over five
    # seconds: this waits up to 30, same as the macOS installer.
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
        warn "not listening after 30s - check: journalctl --user -u $SERVICE -n 50 --no-pager"
        systemctl --user status "$SERVICE" --no-pager 2>/dev/null | sed -n '1,5p' >&2 || true
    fi
elif [ "$NO_AUTOSTART" -eq 1 ]; then
    say "unit written; not loaded (--no-autostart)"
fi

# ---------------------------------------------------------------- linger
if [ "$LINGER" -eq 1 ]; then
    if command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$USER" 2>/dev/null; then
        ok "loginctl enable-linger $USER"
    else
        warn "could not enable lingering; run it yourself: loginctl enable-linger $USER"
    fi
fi

# ---------------------------------------------------------------- next steps
# On a box without systemd --user, "systemctl --user status" is not a thing the
# user can run, so print the command that actually starts the relay instead.
if [ "$SYSTEMD_OK" -eq 1 ]; then
    runtime_hint="Status:  systemctl --user status $SERVICE"
else
    runtime_hint="Start:   $(quote_argv)"
fi

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

Keep a backup of config.toml before editing.

Log:     $LOG_PATH
$runtime_hint
Stop:    bash scripts/uninstall-linux.sh

If you move or delete this folder, re-run the installer; systemd points at the
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
$runtime_hint
Stop:    bash scripts/uninstall-linux.sh

If you move or delete this folder, re-run the installer; systemd points at the
absolute path of relay/relay.py.
EOF
fi
