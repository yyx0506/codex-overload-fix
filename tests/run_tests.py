#!/usr/bin/env python3
"""Self-test for the universal edition. No third-party deps.

    python tests/run_tests.py            # everything
    python tests/run_tests.py --unit     # only the classifier

Each end-to-end case starts tests/mock_upstream.py on a throwaway port, starts
relay.py pointed at it on another port, drives one request through, and asserts
on what came back and on the counters.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "relay"))

import relay  # noqa: E402

PY = sys.executable
MOCK = os.path.join(HERE, "mock_upstream.py")
RELAY = os.path.join(ROOT, "relay", "relay.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Running:
    def __init__(self, args):
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    def tail(self, n=40):
        try:
            self.proc.terminate()
            out, _ = self.proc.communicate(timeout=5)
        except Exception:
            return ""
        return "\n".join(out.splitlines()[-n:])


def http_post(port, body=b'{"model":"mock","input":[]}', timeout=120):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def http_get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def run_pair(mode, fails, relay_flags=(), mock_flags=(), body=b'{"model":"mock","input":[]}',
             timeout=150):
    """Start mock + relay, return (status, headers, body, mock_hits, relay_log, relay)."""
    mport, rport = free_port(), free_port()
    mock = Running([PY, MOCK, mode, str(fails), str(mport)] + list(mock_flags))
    relayp = Running([PY, RELAY, "--port", str(rport),
                      "--upstream", f"http://127.0.0.1:{mport}",
                      "--backoff-base", "0.05", "--backoff-max", "0.2",
                      "--max-attempts", "6", "--conn-budget", "6",
                      "--retries-per-sec", "0"] + list(relay_flags))
    try:
        assert wait_port(mport), "mock did not start"
        assert wait_port(rport), "relay did not start"
        if body is None:
            return None, None, None, 0, "", relayp
        status, headers, data = http_post(rport, body=body, timeout=timeout)
        time.sleep(0.2)
        _, mock_state = http_get(mport, "/")
        return status, headers, data, mock_state["hits"], "", relayp
    finally:
        relayp.stop()
        mock.stop()


# ---------------------------------------------------------------- unit tests
def unit_tests():
    print("classifier")
    c = relay.classify_event
    check("in-band overload event -> retry",
          c(json.dumps({"type": "error", "sequence_number": 3,
                        "error": {"type": "service_unavailable_error",
                                  "code": "server_is_overloaded",
                                  "message": "Our servers are currently overloaded."}}))[0]
          == "retry")
    check("response.failed overload -> retry",
          c(json.dumps({"type": "response.failed",
                        "response": {"error": {"code": "server_overloaded"}}}))[0] == "retry")
    check("response.created -> hold (not commit)",
          c(json.dumps({"type": "response.created", "response": {"model": "m"}}))[0] == "hold")
    check("output_item.added -> hold (reasoning placeholder)",
          c(json.dumps({"type": "response.output_item.added", "output_index": 0}))[0] == "hold")
    check("output_text.delta -> commit",
          c(json.dumps({"type": "response.output_text.delta", "delta": "x"}))[0] == "commit")
    check("image partial_image -> commit",
          c(json.dumps({"type": "response.image_generation_call.partial_image"}))[0] == "commit")
    check("auth error event -> commit (client must see it)",
          c(json.dumps({"type": "error",
                        "error": {"type": "invalid_request_error", "code": "invalid_api_key"}}))[0]
          == "commit")
    check("transient code -> retry",
          c(json.dumps({"type": "error",
                        "error": {"code": "rate_limit_exceeded"}}))[0] == "retry")
    check("garbage payload -> ignored", c("not json")[0] is None)

    print("upstream parsing")
    s, h, p, prefix = relay.parse_upstream("http://127.0.0.1:8888")
    check("root upstream", (s, h, p, prefix) == ("http", "127.0.0.1", 8888, ""))
    s, h, p, prefix = relay.parse_upstream("http://127.0.0.1:8888/v1")
    check("/v1 upstream is stripped", prefix == "", f"got {prefix!r}")
    s, h, p, prefix = relay.parse_upstream("chatgpt.com/backend-api/codex")
    check("official upstream", (s, prefix) == ("https", "/backend-api/codex"))


# ------------------------------------------------------- config tool tests
def config_tool_tests():
    print("config tool (configure_codex)")
    import importlib.util
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "configure_codex", os.path.join(ROOT, "tools", "configure_codex.py"))
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)

    with tempfile.TemporaryDirectory() as td:
        # fresh machine: no config at all
        cfg = os.path.join(td, "config.toml")
        cc.write_config(cfg, "direct", "http://127.0.0.1:8787")
        text = open(cfg, encoding="utf-8").read()
        check("fresh: model_provider set", 'model_provider = "openai-retry"' in text)
        check("fresh: requires_openai_auth", "requires_openai_auth = true" in text)
        check("fresh: no backup (nothing to back up)",
              not os.path.exists(cfg + cc.BACKUP_SUFFIX))

        # existing config with unrelated tables and a different provider
        cfg2 = os.path.join(td, "existing.toml")
        original = (
            '# my config\nmodel = "gpt-5.6"\nmodel_provider = "other"\n\n'
            '[model_providers.other]\nname = "other"\nbase_url = "http://x/v1"\n\n'
            '[profiles.heavy]\nmodel_reasoning_effort = "high"\n'
        )
        with open(cfg2, "w", encoding="utf-8") as fh:
            fh.write(original)
        cc.write_config(cfg2, "gateway", "http://127.0.0.1:8788/v1", api_key="sk-1")
        text = open(cfg2, encoding="utf-8").read()
        check("existing: model_provider replaced", 'model_provider = "gateway-retry"' in text)
        check("existing: unrelated tables intact",
              "[profiles.heavy]" in text and "[model_providers.other]" in text)
        check("existing: key written as static header",
              'Authorization = "Bearer sk-1"' in text)
        check("existing: backup created", os.path.exists(cfg2 + cc.BACKUP_SUFFIX))
        with open(cfg2 + cc.BACKUP_SUFFIX, encoding="utf-8") as fh:
            pristine = fh.read()
        check("backup equals the original", pristine == original)

        # idempotent re-run
        cc.write_config(cfg2, "gateway", "http://127.0.0.1:8788/v1", api_key="sk-1")
        text = open(cfg2, encoding="utf-8").read()
        check("re-run: exactly one retry block",
              text.count("[model_providers.gateway-retry]") == 1)
        with open(cfg2 + cc.BACKUP_SUFFIX, encoding="utf-8") as fh:
            check("re-run: backup not overwritten", fh.read() == pristine)

        # no key -> env_key variant
        cc.write_config(cfg2, "gateway", "http://127.0.0.1:8788/v1")
        text = open(cfg2, encoding="utf-8").read()
        check("no key: env_key written", 'env_key = "SUB2API_API_KEY"' in text)

        # switching modes replaces the block
        cc.write_config(cfg2, "direct", "http://127.0.0.1:8787")
        text = open(cfg2, encoding="utf-8").read()
        check("mode switch removes the old block", "gateway-retry" not in text)
        check("mode switch writes the new block", "[model_providers.openai-retry]" in text)

        # TOML still parses (python 3.11+ has tomllib; skip silently below)
        try:
            import tomllib
            with open(cfg2, "rb") as fh:
                tomllib.load(fh)
            check("written config parses as TOML", True)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            check("written config parses as TOML", False, str(exc))

        # restore
        cc.restore_config(cfg2)
        with open(cfg2, encoding="utf-8") as fh:
            check("restore returns the original", fh.read() == original)


# ---------------------------------------------------------------- e2e tests
def e2e_tests():
    print("e2e: in-band overload absorbed (the production shape)")
    st, hd, data, hits, _, rp = run_pair("sse502", 2)
    check("client saw 200", st == 200, f"got {st}")
    check("client saw the real answer", b"MOCK_" in data and b"OK" in data, data[:120])
    check("upstream hit exactly 3 times", hits == 3, f"got {hits}")

    print("e2e: cockpit HTTP 503 absorbed")
    st, hd, data, hits, _, rp = run_pair("http503", 2)
    check("client saw 200", st == 200, f"got {st}")
    check("upstream hit exactly 3 times", hits == 3, f"got {hits}")

    print("e2e: post-commit overload is NOT retried (no duplicate output)")
    st, hd, data, hits, _, rp = run_pair("postcommit", 1)
    check("upstream hit exactly once", hits == 1, f"got {hits}")
    check("client received the partial token", b"PARTIAL" in data, data[:160])

    print("e2e: image generation commits on the image event")
    st, hd, data, hits, _, rp = run_pair("image", 1)
    check("client saw 200", st == 200, f"got {st}")
    check("upstream hit exactly 2 times", hits == 2, f"got {hits}")
    check("image payload reached client", b"image_generation_call" in data, data[:160])

    print("e2e: non-streaming (stream=false) answer relayed verbatim, one hit")
    st, hd, data, hits, _, rp = run_pair("json", 0)
    check("client saw 200", st == 200, f"got {st}")
    check("content-type stays application/json",
          "application/json" in (hd.get("Content-Type") or ""), hd.get("Content-Type"))
    check("body is the upstream JSON", b'"resp_mock"' in data, data[:160])
    check("upstream hit exactly once (no phantom retry)", hits == 1, f"got {hits}")

    print("e2e: non-streaming overload IS retried, then delivered")
    st, hd, data, hits, _, rp = run_pair("jsonoverload", 2)
    check("client saw 200", st == 200, f"got {st}")
    check("client got the real JSON answer", b'"resp_mock"' in data, data[:160])
    check("upstream hit exactly 3 times", hits == 3, f"got {hits}")

    print("e2e: pool cooling 503 is recognised and spaced out")
    t0 = time.time()
    st, hd, data, hits, _, rp = run_pair("cooling", 1,
                                         relay_flags=["--cooling-wait", "1.0",
                                                      "--cooling-latency", "0.5"])
    elapsed = time.time() - t0
    check("client saw 200", st == 200, f"got {st}")
    check("waited at least the cooling delay", elapsed > 1.0, f"{elapsed:.2f}s")

    print("e2e: cooling detection stays off in direct mode unless asked")
    t0 = time.time()
    st, hd, data, hits, _, rp = run_pair("cooling", 1,
                                         relay_flags=["--cooling-wait", "5.0"])
    elapsed = time.time() - t0
    check("client saw 200", st == 200, f"got {st}")
    check("fast 503 treated as a normal retry (no cooling wait)",
          elapsed < 3.0, f"{elapsed:.2f}s")

    print("e2e: give-up returns 503 + Retry-After + JSON (not 200/SSE)")
    st, hd, data, hits, _, rp = run_pair("always503", 999)
    check("status is 503", st == 503, f"got {st}")
    check("content-type is application/json",
          "application/json" in (hd.get("Content-Type") or ""), hd.get("Content-Type"))
    check("Retry-After present", hd.get("Retry-After") is not None)
    check("body is a JSON error", b"relay_retries_exhausted" in data, data[:160])

    print("e2e: dead upstream fails fast instead of thinking for 5 minutes")
    rport = free_port()
    dead = free_port()          # nothing listening there
    rp = Running([PY, RELAY, "--port", str(rport),
                  "--upstream", f"http://127.0.0.1:{dead}",
                  "--backoff-base", "0.05", "--backoff-max", "0.2",
                  "--max-attempts", "30", "--max-conn-attempts", "3",
                  "--conn-budget", "4", "--retries-per-sec", "0"])
    try:
        assert wait_port(rport)
        t0 = time.time()
        st, hd, data = http_post(rport)
        elapsed = time.time() - t0
        check("status is 503", st == 503, f"got {st}")
        check("fails in under 8s", elapsed < 8.0, f"{elapsed:.2f}s")
        check("names the unreachable upstream", b"upstream_unreachable" in data, data[:160])
    finally:
        rp.stop()

    print("e2e: health endpoint")
    mport, rport = free_port(), free_port()
    mock = Running([PY, MOCK, "sse502", "0", str(mport)])
    relayp = Running([PY, RELAY, "--port", str(rport),
                      "--upstream", f"http://127.0.0.1:{mport}",
                      "--backoff-base", "0.05", "--retries-per-sec", "0"])
    try:
        assert wait_port(mport) and wait_port(rport)
        http_post(rport)
        st, snap = http_get(rport, "/__relay/health")
        check("health is 200", st == 200)
        check("version reported", snap.get("version", "").endswith("universal"), snap.get("version"))
        check("mode reported (direct by default)", snap.get("mode") == "direct", snap)
        check("one request counted", snap.get("requests") == 1, snap)
        check("no giveups", snap.get("giveups") == 0, snap)
    finally:
        relayp.stop()
        mock.stop()

    print("e2e: --mode gateway reports mode and needs no explicit port/upstream")
    mport, rport = free_port(), free_port()
    mock = Running([PY, MOCK, "sse502", "0", str(mport)])
    relayp = Running([PY, RELAY, "--mode", "gateway",
                      "--port", str(rport),
                      "--upstream", f"http://127.0.0.1:{mport}/v1",
                      "--backoff-base", "0.05", "--retries-per-sec", "0"])
    try:
        assert wait_port(mport) and wait_port(rport)
        st, hd, data = http_post(rport)
        check("gateway mode still proxies",
              st == 200 and b"MOCK_" in (data or b"") and b"OK" in (data or b""),
              f"{st} {data[:80]}")
        st, snap = http_get(rport, "/__relay/health")
        check("mode reported (gateway)", snap.get("mode") == "gateway", snap)
    finally:
        relayp.stop()
        mock.stop()

    print("e2e: --upstream with a trailing /v1 is normalised (no /v1/v1 404)")
    mport, rport = free_port(), free_port()
    mock = Running([PY, MOCK, "sse502", "0", str(mport)])
    relayp = Running([PY, RELAY, "--port", str(rport),
                      "--upstream", f"http://127.0.0.1:{mport}/v1",
                      "--backoff-base", "0.05", "--retries-per-sec", "0"])
    try:
        assert wait_port(mport) and wait_port(rport)
        st, hd, data = http_post(rport)
        check("still 200", st == 200, f"got {st}")
        check("answer intact", b"MOCK_" in data and b"OK" in data, data[:120])
    finally:
        relayp.stop()
        mock.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", action="store_true")
    args = ap.parse_args()
    print(f"python {sys.version.split()[0]}\n")
    unit_tests()
    config_tool_tests()
    if not args.unit:
        e2e_tests()
    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
