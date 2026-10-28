# Acknowledgements and provenance / 来源与致谢

## Upstream

This repository is a **fork** of:

> **codex-overload-fix** — https://github.com/NewBeeM4n/codex-overload-fix
> Author: **NewBeeM4n** — License: MIT (see `LICENSE`)

The original idea, the diagnosis that the capacity error arrives as an
`error` event inside an HTTP 200 SSE stream, the size-vs-attempts measurement,
the "retry only before commit" safety rule, and the single-file stdlib relay
design are all **his work**. Nothing here would exist without it.

This fork also incorporates the gateway improvements of
[aiyinsitan520/codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit)
(MIT, itself derived from the same upstream) — see "What came from the cockpit
edition" below. Full credit to both authors.

If this helps you, star the upstream repo first: it is where the diagnosis lives.

## What this fork changes

This is the **universal edition**: one relay, two modes, three platforms.

| # | Change | Why |
|---|---|---|
| 1 | **Two modes in one binary** (`--mode direct\|gateway`) | `direct` keeps the original layout (relay in front of chatgpt.com, your own OAuth account); `gateway` puts the relay in front of any account-pool gateway (sub2api, cockpit-tools, ...) with `--upstream`. Ports, defaults and the cooling heuristic switch per mode. |
| 2 | **Cross-platform installers** | Windows `install.ps1 -Mode Direct\|Gateway`, macOS `install-mac.sh` (LaunchAgent; Apple Silicon and Intel — the relay is pure stdlib Python, the CPU never comes into it), Linux `install-linux.sh` (systemd --user). All print the exact per-mode config.toml block. |
| 3 | **Cooling detection is opt-in per mode** (`--cooling-latency`) | The cockpit edition's "503 under 200 ms = pool cooldown" heuristic is right for local gateways but wrong for chatgpt.com itself. Default: 0.2 s in gateway mode, off in direct mode. |
| 4 | **relayctl works everywhere** | Port-owner hints per OS, default relay auto-detection (8788 then 8787), probe auth from `SUB2API_API_KEY` / `GATEWAY_API_KEY` env or the cockpit config file. |
| 5 | **Tests run on all three OSes in CI** | The suite (64 assertions) runs on ubuntu, macOS (Intel + ARM runners) and windows, so the cross-platform claim is exercised, not asserted. |
| 6 | **`tools/configure_codex.py` + `--write-config` installers** | The original (and the cockpit edition) only *print* the config.toml snippet — a real barrier for non-technical users. Installers can now edit `~/.codex/config.toml` themselves: pristine backup kept, idempotent re-runs, mode switching, one-command restore. Double-click `quick-install-*.bat/.command` wrappers make the whole install zero-touch apart from pasting the gateway key. |

## What came from the cockpit edition

The gateway improvements folded in here were written for
`codex-overload-fix-cockpit` and are preserved with minimal modification:

1. Separate budgets per failure class (`--max-conn-attempts`, `--conn-budget`).
2. Give-up returns `503` + `Retry-After` + JSON instead of 200/SSE.
3. Image-generation events count as committed content.
4. A process-wide gate (`--max-concurrent`, `--retries-per-sec`).
5. `response.output_item.added` is a hold, not a commit.
6. Gateway pool-cooling detection (now latency-configurable).
7. `--upstream` trailing `/v1` normalisation.
8. `GET /__relay/health` JSON counters.
9. Interruptible backoff sleeps (client-gone aware).
10. Bare `error` events triaged by code (transient vs fatal).
11. `tools/relayctl.py` and the test suite (`tests/`).

## What was deliberately kept identical

- Byte-transparent proxying of request bodies (including zstd) and auth headers.
- The **never retry after content is committed** rule — including the
  mid-stream "drop the connection so the client retries" behaviour.
- The client-gone check (`select` + `MSG_PEEK`).
- The one-line rollback story.
- MIT licensing and the "no account sharing, no quota bypass, no protocol
  reverse-engineering" position.

## 中文摘要

本仓库是 [NewBeeM4n/codex-overload-fix](https://github.com/NewBeeM4n/codex-overload-fix)（MIT）
的 fork：核心诊断、「只在对内容提交前重试」的安全规则和单文件设计全部来自原作者。
本通用版同时融入了 [aiyinsitan520/codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit)
的网关改进，并在此基础上做了：单二进制双模式（账号直连 / sub2api 等网关转发）、
Windows + macOS（M 系列/Intel）+ Linux 三平台安装器、按模式开关的冷却检测、
跨平台 relayctl，以及三平台 CI 测试。如果你觉得有用，请先去给上游点 star。
