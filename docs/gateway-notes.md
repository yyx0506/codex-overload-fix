# Gateway deployment notes (sub2api / cockpit-tools)

Everything below generalises the measurements in
[codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit)'s
deployment notes (observed on Windows 11 + cockpit-tools 1.3.74) to any
account-pool gateway. Numbers are measurements from that machine, not
estimates; treat them as shapes, not constants.

## Topology

```
Codex desktop / CLI
  │  POST http://127.0.0.1:8788/v1/responses   (relay, this repo)
  ▼
relay.py :8788          --mode gateway
  │  POST http://<gateway>/v1/responses        (headers byte-identical)
  ▼
gateway (sub2api / cockpit-tools)
  │  account pool / session affinity / image generation
  │  https://chatgpt.com/backend-api/codex/responses
  ▼
upstream
```

Two places the relay can live:

- **Per client (default)**: every employee runs the relay locally against the
  shared gateway. Client-gone detection is exact, each user's retries are
  capped by their own gate.
- **Central**: one relay next to the gateway (e.g. on the same server,
  `--upstream http://127.0.0.1:8888`). Then `--max-concurrent` and
  `--retries-per-sec` bound the *whole company's* retry pressure on the shared
  account pool — usually the better trade for teams.

## What the relay must never do behind a gateway

The relay's `forward_headers()` drops only hop-by-hop headers. It must never
normalise, rewrite or synthesise `session-id`, `thread-id`, `x-codex-*` or
`Authorization`. Gateway session affinity keys on those headers; rewriting
them silently breaks account pinning, and the API key rides in
`Authorization` untouched.

## Things worth knowing before you debug

**Fast 503s are usually pool cooldown, not upstream.** Gateways with a whole-pool
cooldown answer in microseconds (measured: ~40 ms) instead of waiting on a real
upstream round trip. That is why a 503 answered faster than `--cooling-latency`
(default 0.2 s in gateway mode) is treated as cooling and waited out with
`--cooling-wait` — hammering a cooling pool extends the cooldown. Direct mode
disables the heuristic (`--cooling-latency 0`) because chatgpt.com does not
behave this way.

**The gateway may multiply your attempts.** cockpit-tools was measured to retry
each accepted request up to 3 times internally (`upstreamSendRetryAttempts = 3`),
so one relay attempt can mean up to three real upstream sends. If you reason
about upstream load, the effective ceiling is roughly
`--max-attempts × <gateway-internal-retries>`, and "one attempt succeeded" in
the relay log proves one *call*, not one upstream request. Check your gateway's
equivalent setting.

**The silent-failure trap.** Gateways that rewrite `config.toml` on account
switches (cockpit does) can revert `model_provider` while the relay keeps
running happily — listening, healthy, and protecting nothing. That is what
`python tools/relayctl.py path` is for: it reads `model_provider` out of
`~/.codex/config.toml` and answers "is the relay in the path?" Run it before
believing any log.

**Non-streaming answers are not SSE.** `stream: false` calls (and probes) come
back as `application/json`. The relay checks Content-Type first; a complete
JSON answer passes through verbatim, a JSON body whose `error` is an overload
is retried. Without this, a JSON answer looks like "stream ended early" — a
retryable condition — and produced a measured 17-retry storm with zero overload
events. A retry storm with zero overload events and zero connection errors is a
classifier bug, not upstream pressure.

## Measured traffic shape (one machine, one evening)

| time | body | attempts | outcome |
|---|---|---|---|
| 21:11 | 211 B | 1 | ok |
| 21:13 | 171 KB | 1 | ok |
| 21:13 | **798 KB** | 1 | ok |
| 21:13 | 107 KB | 1 | ok |
| 23:54 | 531 KB | 1 | ok |
| 23:54 | **790 KB** | 1 | ok |

Two readings: **~800 KB bodies are a normal pattern** (they arrive in the
first seconds of a heavy task — long conversation, big repo context), and the
absence of retries in one window says nothing about the next. The upstream
admission decision is per request and tracks body size (see
[findings.md](findings.md): > 500 KB averaged 6.75 attempts vs ~2 below).
For a shared company pool this matters double: one 800 KB request that fails
8 times eats retry budget from everyone. The relay absorbs it; starting a
fresh conversation occasionally removes the cause.

## Quota reports a pool, not a capacity signal

`/v1/cockpit/quota` on cockpit (sub2api has an equivalent dashboard) reported
"accounts 2/2 available, five-hour 112% remaining, weekly 186%" — `>100%` is
the gateway's own arithmetic across accounts. It is not a signal that the
upstream has spare capacity: admission is decided per request, by size, which
is the whole problem this relay exists to absorb.
