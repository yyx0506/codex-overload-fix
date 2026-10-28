#!/usr/bin/env python3
"""Small operator console for the relay: status, quota, and a live probe.

    python tools/relayctl.py status
    python tools/relayctl.py quota
    python tools/relayctl.py probe --model gpt-5.6-luna
    python tools/relayctl.py path

``status``  reads GET /__relay/health on the relay (tries 8788 then 8787).
``quota``   reads cockpit-tools' own /v1/cockpit/quota (needs the API key).
``probe``   sends one tiny non-streaming request through the relay and prints
            how many upstream attempts it took. Works in both modes: the key
            comes from the gateway env var (sub2api et al.) or, when present,
            cockpit-tools' own config file.
``path``    prints which provider config.toml currently selects, i.e. whether
            the relay is actually in the traffic path. This is the check that
            matters — a running relay with a reverted config protects nothing.

Standard library only. Windows, macOS (Apple Silicon and Intel) and Linux.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

GATEWAY_RELAY = "http://127.0.0.1:8788"   # gateway mode default port
DIRECT_RELAY = "http://127.0.0.1:8787"    # direct mode default port
DEFAULT_COCKPIT = "http://127.0.0.1:8888"


def _get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, repr(exc).encode()


def _default_relay():
    """Prefer whichever default port actually answers the health endpoint."""
    for base in (GATEWAY_RELAY, DIRECT_RELAY):
        st, _ = _get(base.rstrip("/") + "/__relay/health", timeout=2)
        if st == 200:
            return base
    return GATEWAY_RELAY


def _owner_hints(port):
    """How to find the process holding a port, per platform."""
    if sys.platform.startswith("win"):
        return [f"netstat -ano | findstr :{port}",
                'powershell -c "Get-CimInstance Win32_Process '
                "-Filter \'ProcessId=<pid>\' | Select CommandLine\""]
    if sys.platform == "darwin":
        return [f"lsof -nP -iTCP:{port} -sTCP:LISTEN"]
    return [f"ss -ltnp | grep :{port}", f"lsof -nP -iTCP:{port} -sTCP:LISTEN"]


def cmd_status(relay):
    st, body = _get(relay.rstrip("/") + "/__relay/health")
    if st != 200:
        port = re.search(r":(\d+)", relay)
        port = port.group(1) if port else "?"
        print(f"relay at {relay} is not answering ({st or 'no connection'})")
        if st in (404, 405):
            # Something IS listening — it just has no health endpoint. That is
            # the pre-2.0 relay (or a different process on this port), so the
            # useful next question is which build is running, not whether the
            # port is open.
            print("  a process is listening but has no /__relay/health endpoint:")
            print("  that is the old relay, or another service on this port.")
        print("  identify it:")
        for hint in _owner_hints(port):
            print(f"    {hint}")
        if port in ("8788", "8787"):
            other = "8787" if port == "8788" else "8788"
            print(f"  also try:  python tools/relayctl.py status --relay http://127.0.0.1:{other}")
        return 1
    snap = json.loads(body)
    print(f"relay      {snap.get('version')}  mode={snap.get('mode')}  up {snap.get('uptime_s')}s")
    print(f"upstream   {snap.get('upstream')}")
    print()
    rows = [
        ("requests", "req"),
        ("attempts", "upstream attempts"),
        ("retries", "retries issued"),
        ("committed_after_retry", "   of which then succeeded"),
        ("overload_events", "in-band overload events"),
        ("cooling_503", "gateway pool-cooling 503s"),
        ("conn_errors", "connection errors"),
        ("giveups", "gave up (503 to client)"),
        ("passthrough", "non-retryable passthrough"),
    ]
    for key, label in rows:
        print(f"  {label:32} {snap.get(key, 0)}")
    print()
    print(f"  in flight now                    {snap.get('in_flight')}")
    print(f"  waiting on the retry gate        {snap.get('gate_waiting')}")
    if snap.get("requests") and snap.get("giveups") == 0:
        print("\nverdict: no turn has been given up on since start.")
    return 0


def _gateway_key():
    """A bearer key for probe: env var first (sub2api et al.), then cockpit."""
    for var in ("SUB2API_API_KEY", "GATEWAY_API_KEY"):
        val = os.environ.get(var)
        if val:
            return val, False
    try:
        with open(os.path.expanduser("~/.cockpit_tools/codex_local_access.json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get("apiKey"), True
    except (OSError, ValueError):
        return None, False


def cmd_quota(cockpit):
    key, _ = _gateway_key()
    if not key:
        print("could not find a gateway key (SUB2API_API_KEY / GATEWAY_API_KEY env var")
        print("or ~/.cockpit_tools/codex_local_access.json)")
        return 1
    st, body = _get(cockpit.rstrip("/") + "/v1/cockpit/quota",
                    {"Authorization": f"Bearer {key}"})
    if st != 200:
        print(f"quota query failed: {st} {body[:200]!r}")
        return 1
    q = json.loads(body)
    print(f"accounts        {q.get('availableAccountCount')}/{q.get('accountCount')} available")
    print(f"five-hour       {q.get('fiveHourRemainingPercent')}% remaining")
    print(f"weekly          {q.get('weeklyRemainingPercent')}% remaining")
    print(f"cooling         {q.get('cooldownAccountCount')} account(s)")
    for plan in q.get("plans") or []:
        print(f"  plan {plan.get('plan')}: {plan.get('count')} acct, "
              f"weekly {plan.get('weeklyRemainingPercent')}%")
    print()
    if q.get("cooldownAccountCount"):
        print("NOTE: an account is cooling. Retrying harder will not help until it expires;")
        print("      the relay spaces cooling 503s out for --cooling-wait seconds.")
    return 0


def cmd_probe(relay, model):
    key, from_cockpit = _gateway_key()
    payload = json.dumps({
        "model": model,
        "stream": False,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "reply with OK"}]}],
    }).encode()
    st_before, body_before = _get(relay.rstrip("/") + "/__relay/health")
    before = json.loads(body_before) if st_before == 200 else {}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        if from_cockpit:
            headers["x-openai-actor-authorization"] = "cockpit-tools"
    req = urllib.request.Request(relay.rstrip("/") + "/v1/responses",
                                 data=payload, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        print(f"probe failed: {exc!r}")
        return 1
    elapsed = time.time() - t0
    st_after, body_after = _get(relay.rstrip("/") + "/__relay/health")
    after = json.loads(body_after) if st_after == 200 else {}
    attempts = after.get("attempts", 0) - before.get("attempts", 0)
    retries = after.get("retries", 0) - before.get("retries", 0)
    print(f"probe  http {status}  {elapsed:.1f}s  {len(body)}B  "
          f"attempts={attempts} retries={retries}")
    if status != 200:
        print(body[:300].decode("utf-8", "replace"))
    return 0 if status == 200 else 1


def cmd_path():
    cfg = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    if not os.path.exists(cfg):
        print(f"no config at {cfg}")
        return 1
    with open(cfg, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    m = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        print("config.toml does not set model_provider — Codex uses the built-in one;")
        print("the relay is NOT in the traffic path.")
        return 1
    provider = m.group(1)
    block = re.search(
        r'\[model_providers\.' + re.escape(provider) + r'\](.*?)(?=\n\[|\Z)', text, re.S)
    base = None
    if block:
        bm = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', block.group(1), re.M)
        base = bm.group(1) if bm else None
    print(f"model_provider  {provider}")
    print(f"base_url        {base or '(not found)'}")
    if base and re.search(r"127\.0\.0\.1:(87\d\d|88\d\d)", base):
        print("\nthe relay IS in the traffic path.")
        port = re.search(r":(\d+)", base).group(1)
        print(f"check it with:  python tools/relayctl.py status --relay http://127.0.0.1:{port}")
    else:
        print("\nthe relay is NOT in the traffic path — requests bypass it entirely.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "quota", "probe", "path"])
    ap.add_argument("--relay", default=None,
                    help="relay base URL; default auto-detects 8788 then 8787")
    ap.add_argument("--cockpit", default=DEFAULT_COCKPIT)
    ap.add_argument("--model", default="gpt-5.6-luna")
    args = ap.parse_args()
    relay = args.relay or _default_relay()
    if args.command == "status":
        return cmd_status(relay)
    if args.command == "quota":
        return cmd_quota(args.cockpit)
    if args.command == "probe":
        return cmd_probe(relay, args.model)
    return cmd_path()


if __name__ == "__main__":
    sys.exit(main())
