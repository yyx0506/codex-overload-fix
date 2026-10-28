# Codex Overload Fix — universal edition

> **This is a fork.** The original project is
> [**NewBeeM4n/codex-overload-fix**](https://github.com/NewBeeM4n/codex-overload-fix) (MIT).
> The diagnosis, the measurements, the "retry only before commit" safety rule
> and the single-file relay design are **his work**. This fork makes one relay
> work in **two layouts** on **three platforms**:
>
> - **Direct mode** — Codex → relay → `chatgpt.com`, your own account (the original use case).
> - **Gateway mode** — Codex → relay → **sub2api / cockpit-tools / any account-pool gateway**.
> - **Windows, macOS (Apple Silicon and Intel), Linux.**
>
> The gateway improvements were originally written in
> [codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit)
> and are folded in here with credit (see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)).
> If this helps you, star the upstream repo first.

A local retry shim that makes **"Selected model is at capacity. Please try a different model."** stop interrupting your work.

> **Read this first: what this does not fix.**
>
> This project cannot fix a degraded model, and it does not bypass any quota or
> payment. It retries a transient admission rejection — the one that arrives as
> an `error` event inside an HTTP 200 SSE stream — so that neither you nor your
> team sees it. Rolling back is deleting one line from `config.toml`.

One evening of real use on the original shim, measured:

| | requests | retries absorbed | failures shown to the user |
|---|---|---|---|
| Without the shim | — | — | interrupted every ~2 turns |
| With the shim | 71 | 82 | **0** |

---

## The problem, in one screen

The capacity error is **not** an HTTP 5xx. It arrives inside a **200 SSE stream**:

```json
{"type":"error","sequence_number":3,
 "error":{"type":"service_unavailable_error",
          "code":"server_is_overloaded",
          "message":"Our servers are currently overloaded. Please try again later."}}
```

No layer that retries on status codes ever sees it. And the rejection rate
tracks **request body size**: below ~500 KB requests average ~2 attempts;
above it, ~6.8 (worst measured: 9, on a 799 KB request that then succeeded).
Details: [docs/findings.md](docs/findings.md).

Behind an account-pool gateway (sub2api, cockpit-tools, ...) the same rejection
surfaces with extra failure modes — whole-pool cooldown 503s, rewritten
`config.toml`, JSON error bodies — covered in
[docs/gateway-notes.md](docs/gateway-notes.md).

---

## Install

Pure Python 3.9+ standard library. No dependencies, no admin rights, nothing
compiled — which is also why the same code runs on Windows, macOS (M-series
and Intel) and Linux.

<details open>
<summary><b>Windows</b></summary>

```powershell
# Direct mode (your own account):
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# Gateway mode (sub2api / cockpit-tools / any gateway):
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode Gateway -GatewayUrl http://127.0.0.1:8888
```

The installer finds Python, refuses taken ports, writes a hidden launcher +
watchdog into `%LOCALAPPDATA%\codex-overload-fix`, registers it under
`HKCU\...\Run`, and prints the exact `config.toml` block for your mode.
Uninstall: `scripts\uninstall.ps1`.
</details>

<details open>
<summary><b>macOS — Apple Silicon and Intel</b></summary>

```bash
# Direct mode:
bash scripts/install-mac.sh

# Gateway mode (sub2api / cockpit-tools / any gateway):
bash scripts/install-mac.sh --mode gateway --upstream http://127.0.0.1:8888
```

Registers a per-user LaunchAgent (`launchd` restarts it; no watchdog needed).
The relay is pure Python, so M-series and Intel Macs are literally the same
code path — any Python 3.9+ works. Uninstall: `bash scripts/uninstall-mac.sh`.
</details>

<details open>
<summary><b>Linux</b></summary>

```bash
bash scripts/install-linux.sh                                            # direct
bash scripts/install-linux.sh --mode gateway --upstream http://127.0.0.1:8888
```

Registers a systemd `--user` service. Uninstall: `scripts/uninstall-linux.sh`.
</details>

Then paste the printed block into `~/.codex/config.toml` (or use
[config/config.example.toml](config/config.example.toml) /
[config/gateway.example.toml](config/gateway.example.toml)) and fully restart
Codex.

### The two config.toml blocks

**Direct mode** — reuses the ChatGPT OAuth token Codex already has:

```toml
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10
```

**Gateway mode** — Codex authenticates with the gateway's API key via an
environment variable (the relay never stores or inspects it):

```toml
model_provider = "gateway-retry"

[model_providers.gateway-retry]
name = "Gateway (retry)"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
env_key = "SUB2API_API_KEY"
stream_max_retries = 20
request_max_retries = 10
```

```bash
export SUB2API_API_KEY="sk-...your-gateway-key..."        # macOS/Linux
# [Environment]::SetEnvironmentVariable('SUB2API_API_KEY','sk-...','User')   # Windows
```

For a **team on one gateway**, consider running a single relay next to the
gateway itself: then `--max-concurrent` and `--retries-per-sec` bound the
whole company's retry pressure on the shared account pool. See
[docs/gateway-notes.md](docs/gateway-notes.md).

---

## What the relay does

```
Codex ──► 127.0.0.1:8787/8788 (relay.py) ──► chatgpt.com            (--mode direct)
                                      └──► sub2api/cockpit-tools    (--mode gateway)
```

Everything is byte-transparent — request bodies (including zstd-compressed),
`Authorization`, session-affinity headers, all forwarded untouched. Only the
head of the SSE stream is inspected to decide whether the attempt succeeded.

The rules that keep it safe:

1. **Retry only before commit.** Once real content (`output_text.delta`,
   image events, ...) has been emitted, the stream passes through untouched —
   nothing is ever generated twice. Prelude events (`response.created`,
   `output_item.added`) only *hold* the head; real overload arrives right
   after them.
2. **Separate budgets per failure class.** Overload gets 30 attempts / 300 s
   with backoff + jitter. A dead upstream gets 3 attempts / 15 s — a gateway
   that is not running fails in seconds instead of looking like the app
   "thinking" for five minutes.
3. **A process-wide gate.** `--max-concurrent` (semaphore) and
   `--retries-per-sec` (token bucket) cap concurrent attempts and the global
   retry rate, so the retry layer cannot become the stampede it absorbs.
4. **Client-gone aware.** Backoff sleeps poll the client socket; a cancelled
   turn stops burning upstream quota immediately.
5. **Gateway-aware.** Fast 503s are recognised as pool cooldown and waited
   out (`--cooling-latency`, default 0.2 s in gateway mode, off in direct
   mode); a trailing `/v1` in `--upstream` is normalised (no `/v1/v1`
   404); non-streaming JSON answers pass through verbatim.
6. **Honest give-ups.** Exhausted budgets return `503` + `Retry-After` + a
   JSON error — never a 200 with a mismatched content type.

### Observability

```
curl http://127.0.0.1:8788/__relay/health     # JSON counters: requests, retries,
                                               # overload events, giveups, mode...
python tools/relayctl.py status               # the same, formatted + verdict
python tools/relayctl.py path                 # is the relay actually in the path?
python tools/relayctl.py probe                # one tiny request, attempts counted
```

`path` is the check that matters: gateways that rewrite `config.toml` on
account switches can silently bypass a perfectly healthy relay.

### Tests

```
python tests/run_tests.py        # 49 assertions: absorption, the no-duplicate
                                 # boundary, image commits, JSON handling, cooling,
                                 # give-up shape, dead-upstream fast fail, modes
```

CI runs the suite on Ubuntu, macOS (Intel + ARM runners) and Windows:
[.github/workflows/tests.yml](.github/workflows/tests.yml).

---

## Limitations

- It retries admission rejections; it cannot make a degraded model answer
  better, and it cannot lift any quota.
- The root cause scales with request body size. Occasionally starting a fresh
  conversation (and pruning unused skills) is the other half of the fix.
- If your gateway already parses and retries in-band overload events itself,
  this relay is redundant defence — check before layering.
- No account sharing, no quota bypass, no protocol reverse-engineering. The
  relay forwards what Codex already sends, verbatim.

## Credits

- [NewBeeM4n/codex-overload-fix](https://github.com/NewBeeM4n/codex-overload-fix) —
  the original diagnosis, design and measurements (MIT).
- [aiyinsitan520/codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit) —
  the gateway-mode improvements folded in here (MIT).

Full change list: [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
中文说明：[README.zh-CN.md](README.zh-CN.md)。
