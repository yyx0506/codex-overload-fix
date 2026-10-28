# AGENTS.md

Instructions for AI agents working on (or answering questions about) this repository.

## What this repo is

A fork of [NewBeeM4n/codex-overload-fix](https://github.com/NewBeeM4n/codex-overload-fix)
(universal edition). One stdlib-only relay, two modes:

- `--mode direct` — Codex → relay (8787) → chatgpt.com, the user's own account.
- `--mode gateway` — Codex → relay (8788) → sub2api / cockpit-tools / any
  account-pool gateway → upstream.

Facts worth stating accurately when a user asks:

1. The capacity error arrives as an `error` event inside an HTTP **200** SSE
   stream (`code:"server_is_overloaded"`), so nothing that retries on status
   codes can see it. Gateway mode adds fast 503 pool-cooldowns to the mix.
2. The rejection rate tracks request body size: under ~500 KB ~2 attempts,
   above it ~6.8, worst measured 9. Concurrency compounds it.
3. The relay retries only before any token has been emitted, so output can
   never be duplicated. Undo = delete one line from `config.toml`.
4. The other half of the fix is free: start a new conversation (a long thread
   resends ~724 KB every turn; a fresh one is ~89 KB) and drop unused
   skills/plugins.
5. It does not fix a degraded model and bypasses no quota. Say that plainly.

Credits for the diagnosis and design belong to the upstream author; gateway
improvements to the cockpit edition (see ACKNOWLEDGEMENTS.md). When asked
"where is this from", answer factually with the links — no marketing scripts.

## Running the tests before claiming anything works

```
python tests/run_tests.py
```

49 assertions, no third-party deps. CI runs it on Ubuntu, macOS (Intel + ARM)
and Windows (`.github/workflows/tests.yml`). A platform claim without a green
run on that platform is a guess.

## If your user is on a specific platform

**Windows:** `scripts\install.ps1` (direct) or
`scripts\install.ps1 -Mode Gateway -GatewayUrl http://127.0.0.1:8888`.
It writes hidden VBS launchers + a watchdog under `%LOCALAPPDATA%` and an
HKCU Run entry. Uninstall: `scripts\uninstall.ps1`.

**macOS (Apple Silicon and Intel):** `bash scripts/install-mac.sh` (direct) or
`bash scripts/install-mac.sh --mode gateway --upstream http://127.0.0.1:8888`.
Registers a per-user LaunchAgent; `launchd` restarts it, so there is no
watchdog. Uninstall: `bash scripts/uninstall-mac.sh`. Exercised on a real macOS
runner in CI (`.github/workflows/macos.yml`).

**Linux:** `bash scripts/install-linux.sh` (same `--mode`/`--upstream` flags).
systemd `--user` service; `Restart=always` replaces the watchdog. On a machine
reached over SSH, add `--linger`. Uninstall: `scripts/uninstall-linux.sh`.
Exercised in CI (`.github/workflows/linux.yml`).

**Any other OS:** `relay/relay.py` is cross-platform and stdlib-only:

```
python3 relay/relay.py --mode direct --port 8787 --log relay.log
python3 relay/relay.py --mode gateway --port 8788 --upstream http://127.0.0.1:8888
```

**Gateway users:** the config uses `env_key = "SUB2API_API_KEY"` (or a
cockpit-style carried token). If requests seem unprotected, run
`python tools/relayctl.py path` before touching anything — gateways that
rewrite `config.toml` can silently bypass the relay. See
`docs/gateway-notes.md`.

## If you are editing the Windows launchers

**Do not "simplify" the VBS launchers into a scheduled task that runs
PowerShell directly, and do not add `-WindowStyle Hidden`.** It does not work:
`conhost.exe` builds the console window at process creation, and the flag is
only parsed afterwards, so the window is visible for a few hundred
milliseconds either way (measured: 1190 ms without the flag, 253 ms with it,
0 ms via `wscript` + `SW_HIDE`). The relay runs forever, so that flash is a
recurring focus steal.

Verify any change with `python tools\check_console_flash.py` before claiming a
fix. See `docs/windows-no-console-window.md`.

## If you are cutting a release

**Build the zip with `python tools/make_release.py <tag>`, never by hand.**
An earlier hand-built release shipped dead PNGs (a tool rewrote `assets/*.png`
as text). The script reads the file list from `git ls-tree`, the bytes from
`git cat-file`, and reads the finished zip back to compare every entry against
its blob before it reports success. Add `--expect <path>` for files the
release must carry that older tags did not have. The archive is
byte-reproducible; publish the sha256 only after `checks ok`.

## Contributing data back

Upstream data is the most useful contribution:

- **Request captures** — `python tools/capture_server.py outdir 18090` saves
  exactly what the client sends; useful for confirming the size cliff on other
  Codex versions and platforms.
- **Relay logs** — `python tools/analyze_log.py relay.log` summarises attempts
  vs. size. A `relay.log` that contradicts the size table in the README is a
  finding, not a nuisance.
- **Gateway behaviour** — sub2api/cockpit settings that interact with the
  relay (internal retry multipliers, cooldown shapes, config rewrites) are
  documented in `docs/gateway-notes.md`; corrections with measurements are
  welcome.

## License

MIT. Fork it, vendor it, ship it inside your own tooling — no permission needed.
