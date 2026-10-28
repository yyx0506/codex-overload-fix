#!/usr/bin/env python3
"""Local retry relay for Codex /responses — universal edition.

Derived from https://github.com/NewBeeM4n/codex-overload-fix (MIT, © 2026 NewBeeM4n),
with the gateway improvements of the cockpit-tools edition folded in.
See ACKNOWLEDGEMENTS.md for the full list of changes.

Two modes, one binary
---------------------
--mode direct   Codex -> relay(8787) -> https://chatgpt.com/backend-api/codex
                Your own account: auth headers pass through untouched.

--mode gateway  Codex -> relay(8788) -> http(s)://<gateway> -> upstream
                For any account-pool gateway (sub2api, cockpit-tools, ...).
                ``--upstream`` is required; a trailing ``/v1`` is normalised
                (``http://host:8888/v1`` would otherwise build
                ``/v1/v1/responses`` and 404). API keys ride along in the
                Authorization header, byte-identical.

What this adds over the original shim
-------------------------------------
1. Separate budgets per failure class. A refused connection (gateway not
   running) fails in seconds; only genuine in-band overload gets the long
   retry budget. The original used one budget for both, so a dead upstream
   looked like the app "thinking" for five minutes.
2. Give-up returns HTTP 503 + ``Retry-After`` + a JSON error body.
   The original returned HTTP 200 with a JSON body while declaring
   ``Content-Type: text/event-stream``.
3. Image-generation events count as committed content, so an image task can
   no longer be held back for the whole commit deadline.
4. A process-wide gate caps concurrent upstream attempts and the global
   retry rate. Three 790 KB requests no longer turn into 27 simultaneous
   punts at the upstream.
5. Gateway awareness: fast 503s are recognised as pool-cooldown short
   circuits (``--cooling-latency``, on by default in gateway mode only),
   and session-affinity headers pass through untouched.
6. Non-streaming answers (``stream: false``) are relayed verbatim instead of
   being misread as "stream ended early" and retried until the client left.
7. ``GET /__relay/health`` returns JSON counters for the diagnostic flow.

Everything else is byte-transparent: request bodies (including compressed
ones) and auth headers are forwarded untouched. Only the head of the SSE
stream is inspected to decide whether the attempt succeeded.
"""

import argparse
import gzip
import http.client
import json
import os
import random
import select
import socket
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "3.0.0-universal"
STARTED_AT = time.time()

HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

OVERLOAD_CODES = {
    "server_overloaded",      # response.failed / response.error
    "server_is_overloaded",   # bare {"type":"error"} event (observed on real traffic)
    "service_unavailable_error",
    "flex_unavailable",
    "queue_full",
    "slow_down",
}

# Non-overload codes that are still worth another attempt: they describe a
# momentary upstream condition, not something wrong with the request.
TRANSIENT_CODES = {
    "rate_limit_exceeded",
    "server_error",
    "internal_error",
    "timeout",
    "upstream_error",
    "incomplete_upstream_stream",
}

# Events that mean "content is already on its way to the client". Once one of
# these has been seen, retrying would generate a second answer.
COMMIT_EVENTS = (
    "response.output_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.output_item.done",
    "response.completed",
    "response.incomplete",
    # Image generation: the original shim left these out, so an image task
    # could be held back for the whole --commit-deadline.
    "response.image_generation_call.partial_image",
    "response.image_generation_call.completed",
    "response.image_generation.partial_image",
)

# Events that prove the upstream has started producing this response but carry
# no user-visible content. They are *not* a commit: real overload events arrive
# right after response.created (sequence_number 3 in the captured traffic).
PRELUDE_EVENTS = (
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.content_part.added",
    "response.reasoning_summary_part.added",
)

# Default for --cooling-latency in gateway mode: a gateway 503 that comes back
# faster than this is the whole-pool cooldown short-circuit, not a real upstream
# answer. Retrying instantly is useless. Direct mode defaults it to 0 (off):
# chatgpt.com does not have this behaviour, and a genuinely fast 503 from a
# remote hop should not be mistaken for one.
COOLING_LATENCY_DEFAULT = 0.2

LOG_LOCK = threading.Lock()
LOG_SIZE = [0]
LOG_MAX = 8 * 1024 * 1024

ARGS = None


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        if ARGS and ARGS.log:
            try:
                if LOG_SIZE[0] > LOG_MAX:
                    os.replace(ARGS.log, ARGS.log + ".1")
                    LOG_SIZE[0] = 0
                with open(ARGS.log, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                LOG_SIZE[0] += len(line) + 1
            except OSError:
                pass


# --------------------------------------------------------------------------
# counters
# --------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = 0
        self.attempts = 0
        self.retries = 0
        self.giveups = 0
        self.passthrough = 0
        self.overload_events = 0
        self.conn_errors = 0
        self.cooling_503 = 0
        self.commit_after_retry = 0

    def bump(self, name, n=1):
        with self.lock:
            setattr(self, name, getattr(self, name) + n)

    def snapshot(self):
        with self.lock:
            return {
                "uptime_s": round(time.time() - STARTED_AT, 1),
                "requests": self.requests,
                "attempts": self.attempts,
                "retries": self.retries,
                "giveups": self.giveups,
                "passthrough": self.passthrough,
                "overload_events": self.overload_events,
                "conn_errors": self.conn_errors,
                "cooling_503": self.cooling_503,
                "committed_after_retry": self.commit_after_retry,
            }


STATS = Stats()


# --------------------------------------------------------------------------
# global gate: concurrency + retry rate
# --------------------------------------------------------------------------
class Gate:
    """Caps concurrent upstream attempts and the global retry start rate.

    Without this, N concurrent large requests that all hit overload multiply
    into N x attempts of simultaneous traffic — a self-inflicted stampede.
    """

    def __init__(self, max_concurrent, retries_per_sec, burst):
        self.sem = threading.BoundedSemaphore(max(1, max_concurrent))
        self.rate = max(0.0, retries_per_sec)
        self.burst = max(1.0, burst)
        self.tokens = self.burst
        self.updated = time.monotonic()
        self.lock = threading.Lock()
        self.waiting = 0

    def _refill(self):
        now = time.monotonic()
        dt = now - self.updated
        self.updated = now
        if self.rate > 0:
            self.tokens = min(self.burst, self.tokens + dt * self.rate)

    def take_retry_token(self, deadline):
        """Block until a retry may start. False if the deadline passes."""
        if self.rate <= 0:
            return True
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                need = (1.0 - self.tokens) / self.rate
            if time.time() + need > deadline:
                return False
            self.waiting += 1
            try:
                time.sleep(min(need, 0.5))
            finally:
                self.waiting -= 1

    def acquire(self):
        self.sem.acquire()

    def release(self):
        try:
            self.sem.release()
        except ValueError:
            pass


GATE = None


# --------------------------------------------------------------------------
# upstream helpers
# --------------------------------------------------------------------------
def upstream_connection(timeout):
    if ARGS.proxy:
        phost, pport = ARGS.proxy
        conn = http.client.HTTPSConnection(phost, pport, timeout=timeout)
        conn.set_tunnel(ARGS.upstream_host, ARGS.upstream_port)
        return conn
    if ARGS.upstream_scheme == "http":
        return http.client.HTTPConnection(ARGS.upstream_host, ARGS.upstream_port, timeout=timeout)
    return http.client.HTTPSConnection(ARGS.upstream_host, ARGS.upstream_port, timeout=timeout)


def read_request_body(handler):
    te = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        chunks = []
        while True:
            line = handler.rfile.readline(65536)
            if not line:
                break
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline(65536)
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline(65536)
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


def forward_headers(handler):
    """Pass everything through except hop-by-hop headers.

    session-id / thread-id / x-codex-* must survive unchanged: account-pool
    gateways (sub2api, cockpit-tools, ...) key session affinity on them, and
    rewriting or dropping them silently breaks account pinning. The
    Authorization header carries either the Codex OAuth token (direct mode)
    or the gateway API key (gateway mode) — both pass through byte-identical.
    """
    out = {}
    for k, v in handler.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        out[k] = v
    out["Host"] = ARGS.upstream_host
    out["Accept-Encoding"] = "identity"
    return out


def is_overload(err):
    code = (err.get("code") or "").lower()
    etype = (err.get("type") or "").lower()
    msg = (err.get("message") or "").lower()
    return (
        code in OVERLOAD_CODES
        or etype in OVERLOAD_CODES
        or "at capacity" in msg
        or "overloaded" in msg
    )


def classify_event(payload):
    """Return ('retry'|'commit'|'hold'|None, info)."""
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return None, None
    kind = evt.get("type")

    if kind == "response.failed":
        err = (evt.get("response") or {}).get("error") or {}
        code = err.get("code") or ""
        msg = err.get("message") or ""
        if is_overload(err):
            return "retry", f"{code or 'overload'}: {msg[:60]}"
        return "commit", f"non-retryable failure {code}"

    if kind in COMMIT_EVENTS:
        return "commit", kind

    if kind == "error":
        err = evt.get("error") or {}
        code = (err.get("code") or "").lower()
        etype = (err.get("type") or "").lower()
        if is_overload(err):
            return "retry", f"error event overload: {(err.get('code') or err.get('type') or '')}"
        if code in TRANSIENT_CODES or etype in TRANSIENT_CODES:
            return "retry", f"error event transient: {code or etype}"
        # Anything else (invalid_request, auth, quota exhausted) is the
        # client's answer to see — passing it through is the honest choice.
        return "commit", f"error event {code or etype}"

    if kind in PRELUDE_EVENTS:
        return "hold", kind

    return None, None


def parse_retry_after(headers):
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


class Attempt:
    """One upstream attempt's outcome."""

    __slots__ = ("mode", "status", "headers", "prelude", "conn", "resp",
                 "reason", "retry_after", "elapsed")

    def __init__(self, mode, status=0, headers=None, prelude=b"", conn=None,
                 resp=None, reason="", retry_after=None, elapsed=0.0):
        self.mode = mode
        self.status = status
        self.headers = headers or {}
        self.prelude = prelude
        self.conn = conn
        self.resp = resp
        self.reason = reason
        self.retry_after = retry_after
        self.elapsed = elapsed


def attempt(path, headers, body, timeout, commit_by):
    """One upstream attempt.

    mode: "stream"      -> relay the SSE stream to the client
          "body"        -> a complete non-streaming body, already read
          "retry"       -> transient, try again
          "cooling"     -> cockpit cooldown short-circuit, wait longer
          "passthrough" -> non-retryable status, hand it to the client

    "body" exists because not every /responses call streams: `stream: false`
    and probe calls come back as application/json. Treating one of those as a
    stream made the relay read to EOF, find no events, and call it "stream
    ended early" — which is retryable, so it retried until the client left.
    """
    started = time.time()
    conn = upstream_connection(timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
    except Exception as exc:  # noqa: BLE001 - network failures are retryable
        conn.close()
        return Attempt("retry", reason=f"connect/read error: {exc.__class__.__name__}: {exc}",
                       elapsed=time.time() - started)

    if resp.status != 200:
        data = b""
        try:
            data = resp.read()
        except Exception:  # noqa: BLE001
            pass
        hdrs = dict(resp.getheaders())
        conn.close()
        status = resp.status
        elapsed = time.time() - started
        retry_after = parse_retry_after(hdrs)
        if status in (429, 500, 502, 503, 504):
            # Gateways convert an in-band overload into 503; when the whole
            # pool is cooling they answer in microseconds instead of waiting
            # on a real upstream. Same status, very different meaning.
            if status == 503 and ARGS.cooling_latency > 0 and elapsed < ARGS.cooling_latency:
                return Attempt("cooling", status, hdrs, data,
                               reason=f"http 503 in {elapsed*1000:.0f}ms (pool cooling)",
                               retry_after=retry_after, elapsed=elapsed)
            return Attempt("retry", status, hdrs, data, reason=f"http {status}",
                           retry_after=retry_after, elapsed=elapsed)
        return Attempt("passthrough", status, hdrs, data, elapsed=elapsed)

    ctype = (resp.getheader("Content-Type") or "").lower()
    if "text/event-stream" not in ctype:
        # Non-streaming call (stream=false, /models, a JSON error body). There
        # is no SSE to look for, so the whole body is the answer — unless that
        # answer is itself an overload, in which case it is worth another go.
        try:
            data = resp.read()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            return Attempt("retry", 0, {}, b"",
                           reason=f"read error: {exc.__class__.__name__}",
                           elapsed=time.time() - started)
        hdrs = dict(resp.getheaders())
        conn.close()
        rc = resp.status
        elapsed = time.time() - started
        if rc == 200:
            try:
                doc = json.loads(data)
            except (ValueError, TypeError):
                doc = None
            if isinstance(doc, dict):
                err = doc.get("error")
                if isinstance(err, dict) and (is_overload(err)
                                              or (err.get("code") or "").lower() in TRANSIENT_CODES):
                    STATS.bump("overload_events")
                    return Attempt("retry", 200, hdrs, data,
                                   reason=f"json error: {err.get('code')}", elapsed=elapsed)
            return Attempt("body", 200, hdrs, data, elapsed=elapsed)
        if rc in (429, 500, 502, 503, 504):
            if rc == 503 and ARGS.cooling_latency > 0 and elapsed < ARGS.cooling_latency:
                return Attempt("cooling", rc, hdrs, data,
                               reason=f"http 503 in {elapsed*1000:.0f}ms (pool cooling)",
                               elapsed=elapsed)
            return Attempt("retry", rc, hdrs, data, reason=f"http {rc}",
                           retry_after=parse_retry_after(hdrs), elapsed=elapsed)
        return Attempt("passthrough", rc, hdrs, data, elapsed=elapsed)

    prelude = bytearray()
    buf = b""
    seen = 0
    while True:
        try:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        except Exception as exc:  # noqa: BLE001
            conn.close()
            return Attempt("retry", 0, {}, bytes(prelude),
                           reason=f"stream error: {exc.__class__.__name__}",
                           elapsed=time.time() - started)
        if not chunk:
            conn.close()
            return Attempt("retry", 0, {}, bytes(prelude), reason="stream ended early",
                           elapsed=time.time() - started)
        prelude.extend(chunk)
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            seen += 1
            if seen <= 12:
                try:
                    evt = json.loads(payload)
                    etype = evt.get("type")
                except (ValueError, TypeError):
                    evt, etype = {}, "?"
                served = (evt.get("response") or {}).get("model") if evt else None
                tag = f"[served={served}] " if served else ""
                log(f"  evt#{seen} {etype} {tag}{payload[:200]!r}")
            verdict, info = classify_event(payload)
            if verdict == "retry":
                conn.close()
                STATS.bump("overload_events")
                return Attempt("retry", 200, {}, bytes(prelude), reason=info or "overload",
                               elapsed=time.time() - started)
            if verdict == "commit":
                return Attempt("stream", 200, dict(resp.getheaders()), bytes(prelude),
                               conn, resp, elapsed=time.time() - started)
        if len(prelude) > 262144:
            return Attempt("stream", 200, dict(resp.getheaders()), bytes(prelude),
                           conn, resp, elapsed=time.time() - started)
        if time.time() >= commit_by:
            return Attempt("stream", 200, dict(resp.getheaders()), bytes(prelude),
                           conn, resp, elapsed=time.time() - started)


def analyze(handler, body):
    """One-line size breakdown of a /responses body. Sizes only, no content."""
    if len(body) > 64 << 20:
        return f" | analyze-skipped({len(body)}B too large)"
    enc = (handler.headers.get("Content-Encoding") or "").lower()
    try:
        if enc in ("", "identity"):
            raw = body
        elif enc == "gzip":
            raw = gzip.decompress(body)
        elif enc == "deflate":
            raw = zlib.decompress(body)
        elif enc == "zstd":
            try:
                from compression import zstd
                raw = zstd.decompress(body)
            except Exception:
                import zstandard
                raw = zstandard.ZstdDecompressor().decompress(body, max_output_size=64 << 20)
        else:
            return f" enc={enc} analysis-skipped"
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return f" analyze-failed({exc.__class__.__name__})"

    parts = []
    if doc.get("model"):
        parts.append(f"model={doc['model']}")
    reasoning = doc.get("reasoning") or {}
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        parts.append(f"effort={reasoning['effort']}")
    if doc.get("service_tier"):
        parts.append(f"tier={doc['service_tier']}")
    if isinstance(doc.get("instructions"), str):
        parts.append(f"instructions={len(doc['instructions'])}c")
    tools = doc.get("tools") or []
    if tools:
        tb = len(json.dumps(tools, ensure_ascii=False).encode())
        parts.append(f"tools={tb}B/{len(tools)}")
    items = doc.get("input") or []
    if isinstance(items, list):
        by = {}
        for it in items:
            role = it.get("type") or "?"
            if role == "message":
                role = "msg:" + (it.get("role") or "?")
            b = len(json.dumps(it, ensure_ascii=False).encode())
            by[role] = by.get(role, 0) + b
        top = ",".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4])
        parts.append(f"input={len(json.dumps(items, ensure_ascii=False).encode())}B/{len(items)}it({top})")
    if doc.get("prompt_cache_key"):
        parts.append("cache_key=yes")
    return " | " + (" ".join(parts) if parts else "empty")


def backoff_delay(attempt_no, extra=0.0):
    delay = min(ARGS.backoff_max, ARGS.backoff_base * (1.6 ** (attempt_no - 1)))
    delay *= random.uniform(0.7, 1.3)
    return max(delay, extra)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- entry
    def _proxy(self):
        path = self.path
        if path.split("?")[0] == "/__relay/health":
            self._health()
            return
        if path.split("?")[0] == "/__relay/stats":
            self._health()
            return

        upstream_path = ARGS.upstream_prefix + path
        body = read_request_body(self)
        headers = forward_headers(self)

        is_responses = self.command == "POST" and path.split("?")[0].endswith("/responses")
        if not is_responses:
            self._passthrough(upstream_path, headers, body)
            return

        STATS.bump("requests")
        cport = self.client_address[1]
        log(f"[{cport}] {self.command} {path} ({len(body)}B){analyze(self, body)}")

        deadline = time.time() + ARGS.total_budget
        conn_deadline = time.time() + ARGS.conn_budget
        attempt_no = 0
        conn_failures = 0
        last = None

        while True:
            if self._client_gone():
                log(f"abandoning retries after {attempt_no} attempts (client gone)")
                return
            attempt_no += 1
            STATS.bump("attempts")

            GATE.acquire()
            try:
                res = attempt(upstream_path, headers, body, ARGS.timeout,
                              time.time() + ARGS.commit_deadline)
            finally:
                GATE.release()

            if res.mode == "stream":
                log(f"[{cport}] attempt {attempt_no}: ok, streaming "
                    f"({res.elapsed:.1f}s)")
                if attempt_no > 1:
                    STATS.bump("commit_after_retry")
                self._stream_back(res.headers, res.prelude, res.conn, res.resp)
                return

            if res.mode == "body":
                # Non-streaming answer (stream=false). Hand it back verbatim;
                # there is no SSE and nothing left to retry.
                log(f"[{cport}] attempt {attempt_no}: ok, non-streaming "
                    f"({len(res.prelude)}B, {res.elapsed:.1f}s)")
                self._send_simple(200, res.headers, res.prelude)
                return

            if res.mode == "passthrough":
                STATS.bump("passthrough")
                log(f"[{cport}] attempt {attempt_no}: upstream {res.status}, passthrough")
                self._send_simple(res.status, res.headers, res.prelude)
                return

            last = res
            if res.mode == "cooling":
                STATS.bump("cooling_503")
            if "connect/read error" in res.reason:
                STATS.bump("conn_errors")
                conn_failures += 1
            log(f"[{cport}] attempt {attempt_no}: {res.mode} ({res.reason})")

            # --- give-up decisions -------------------------------------
            # A refused connection means cockpit is not listening. Retrying it
            # thirty times makes the app look frozen; fail fast and let Codex
            # show the real error. Only connection errors use this budget —
            # an overload that happens to outlast conn_budget must keep going.
            if conn_failures and (conn_failures >= ARGS.max_conn_attempts
                                  or time.time() >= conn_deadline):
                log(f"[{cport}] giving up after {attempt_no} attempts: upstream unreachable")
                STATS.bump("giveups")
                self._send_error(503, "upstream_unreachable",
                                 f"cannot reach {ARGS.upstream_host}:{ARGS.upstream_port}: {res.reason}",
                                 retry_after=5)
                return
            if attempt_no >= ARGS.max_attempts or time.time() >= deadline:
                log(f"[{cport}] giving up after {attempt_no} attempts")
                STATS.bump("giveups")
                self._send_error(503, "relay_retries_exhausted",
                                 f"upstream still overloaded after {attempt_no} attempts "
                                 f"({res.reason})",
                                 retry_after=10)
                return

            # --- wait before the next attempt ---------------------------
            STATS.bump("retries")
            extra = 0.0
            if res.mode == "cooling":
                # Pool cooldown: hammering it just extends the cooldown.
                extra = ARGS.cooling_wait
            if res.retry_after:
                extra = max(extra, min(res.retry_after, ARGS.backoff_max))
            if not GATE.take_retry_token(deadline):
                log(f"[{cport}] giving up: global retry budget unavailable")
                STATS.bump("giveups")
                self._send_error(503, "relay_gate_exhausted",
                                 "relay global retry rate limit reached", retry_after=10)
                return
            delay = backoff_delay(attempt_no, extra)
            self._sleep_watching(delay)

    # ---------------------------------------------------------------- utils
    def _sleep_watching(self, seconds):
        """Sleep, but bail out early if the client disappears."""
        end = time.time() + seconds
        while time.time() < end:
            if self._client_gone():
                return
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def _client_gone(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _health(self):
        snap = STATS.snapshot()
        snap.update({
            "version": VERSION,
            "mode": ARGS.mode,
            "port": ARGS.port,
            "upstream": f"{ARGS.upstream_scheme}://{ARGS.upstream_host}:{ARGS.upstream_port}"
                        f"{ARGS.upstream_prefix}",
            "in_flight": ARGS.max_concurrent - GATE.sem._value
            if hasattr(GATE.sem, "_value") else None,
            "gate_waiting": GATE.waiting,
            "client_gone_aware": True,
        })
        data = json.dumps(snap, ensure_ascii=False).encode()
        self._send_simple(200, {"Content-Type": "application/json"}, data)

    def _passthrough(self, path, headers, body):
        started = time.time()
        conn = upstream_connection(ARGS.timeout)
        try:
            conn.request(self.command, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            hdrs = dict(resp.getheaders())
            # We asked for identity encoding; if the upstream compressed
            # anyway, the body we hold is already the encoded bytes, so the
            # header stays consistent. Just never re-declare content-length.
            self._send_simple(resp.status, hdrs, data)
            log(f"[{self.client_address[1]}] {self.command} {path} -> {resp.status} "
                f"{len(data)}B in {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"[{self.client_address[1]}] {self.command} {path} -> error {exc!r}")
            self._send_error(502, "upstream_unreachable", str(exc))
        finally:
            conn.close()

    def _stream_back(self, resp_headers, prelude, conn, resp):
        self.send_response(200)
        ctype = resp_headers.get("Content-Type") or "text/event-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent_text = b"response.output_text.delta" in prelude
        buf = b""
        try:
            self.wfile.write(prelude)
            self.wfile.flush()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                buf += chunk
                drop = False
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    low = payload.lower()
                    if not (
                        b"overload" in low
                        or b"at capacity" in low
                        or b'"response.failed"' in payload
                    ):
                        continue
                    try:
                        evt = json.loads(payload)
                    except (ValueError, TypeError):
                        continue
                    err = (evt.get("response") or {}).get("error") or evt.get("error") or {}
                    if is_overload(err) and not sent_text:
                        log("mid-stream overload after commit -> dropping connection for client retry")
                        drop = True
                        break
                if drop:
                    conn.close()
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
                if b"response.output_text.delta" in chunk:
                    sent_text = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def _send_simple(self, status, headers, data):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            if k.lower() in ("content-length", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(self, status, code, message, retry_after=None):
        body = json.dumps({"error": {"type": "relay_error", "code": code,
                                     "message": message}}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", str(int(retry_after)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy


def parse_upstream(value):
    scheme = "https"
    if value.startswith("http://"):
        scheme, value = "http", value[len("http://"):]
    elif value.startswith("https://"):
        value = value[len("https://"):]
    hostport, _, path = value.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port) if port else (80 if scheme == "http" else 443)
    prefix = "/" + path.strip("/") if path.strip("/") else ""
    # The provider's base_url already carries /v1, so clients send
    # /v1/responses. An upstream of http://host:8888/v1 would build
    # /v1/v1/responses and 404. Strip it instead of failing silently.
    if prefix.rstrip("/").endswith("/v1"):
        stripped = prefix.rstrip("/")[: -len("/v1")]
        print(f"relay: --upstream ends with /v1; using {stripped or '/'} as prefix", flush=True)
        prefix = stripped
    return scheme, host, port, prefix


def main():
    global ARGS, GATE
    p = argparse.ArgumentParser(
        description="Local retry relay for Codex /responses (universal edition)")
    p.add_argument("--mode", choices=["direct", "gateway"], default="direct",
                   help="direct: in front of chatgpt.com, your own account (default). "
                        "gateway: in front of an account-pool gateway "
                        "(sub2api, cockpit-tools, ...); --upstream is required then")
    p.add_argument("--port", type=int, default=None,
                   help="local port (default 8787 direct / 8788 gateway)")
    p.add_argument("--upstream", default=None,
                   help="direct: chatgpt.com/backend-api/codex (default). "
                        "gateway: your gateway, e.g. http://127.0.0.1:8888 or "
                        "https://gw.example.com:8443; a trailing /v1 is stripped")
    p.add_argument("--proxy", default="", help="e.g. http://127.0.0.1:7897")
    p.add_argument("--max-attempts", type=int, default=30,
                   help="overload retry budget (default 30)")
    p.add_argument("--max-conn-attempts", type=int, default=3,
                   help="connection-error retry budget; short on purpose (default 3)")
    p.add_argument("--conn-budget", type=float, default=15.0,
                   help="seconds to spend on connection errors before failing (default 15)")
    p.add_argument("--total-budget", type=float, default=300.0)
    p.add_argument("--backoff-base", type=float, default=1.0)
    p.add_argument("--backoff-max", type=float, default=8.0)
    p.add_argument("--cooling-wait", type=float, default=5.0,
                   help="minimum wait after a pool-cooling 503 (default 5s)")
    p.add_argument("--cooling-latency", type=float, default=None,
                   help="a 503 answered faster than this (seconds) counts as the "
                        "gateway's pool-cooldown short circuit; 0 disables "
                        "(default 0.2 gateway / 0 direct)")
    p.add_argument("--commit-deadline", type=float, default=120.0,
                   help="max seconds to hold the stream head while deciding to retry")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--max-concurrent", type=int, default=4,
                   help="concurrent upstream attempts across all clients (default 4)")
    p.add_argument("--retries-per-sec", type=float, default=2.0,
                   help="global retry start rate, 0 disables (default 2)")
    p.add_argument("--retry-burst", type=float, default=4.0)
    p.add_argument("--log", default="")
    ARGS = p.parse_args()

    # Mode defaults: anything the user passed explicitly wins.
    if ARGS.mode == "gateway":
        if not ARGS.upstream:
            p.error("--mode gateway requires --upstream "
                    "(e.g. http://127.0.0.1:8888 or https://gw.example.com)")
        if ARGS.port is None:
            ARGS.port = 8788
        if ARGS.cooling_latency is None:
            ARGS.cooling_latency = COOLING_LATENCY_DEFAULT
    else:
        if ARGS.upstream is None:
            ARGS.upstream = "chatgpt.com/backend-api/codex"
        if ARGS.port is None:
            ARGS.port = 8787
        if ARGS.cooling_latency is None:
            ARGS.cooling_latency = 0.0

    if ARGS.log and os.path.exists(ARGS.log):
        try:
            LOG_SIZE[0] = os.path.getsize(ARGS.log)
        except OSError:
            LOG_SIZE[0] = 0
    ARGS.upstream_scheme, ARGS.upstream_host, ARGS.upstream_port, ARGS.upstream_prefix = (
        parse_upstream(ARGS.upstream))
    if ARGS.proxy:
        host, _, port = ARGS.proxy.replace("http://", "").replace("https://", "").partition(":")
        ARGS.proxy = (host, int(port or 80))

    GATE = Gate(ARGS.max_concurrent, ARGS.retries_per_sec, ARGS.retry_burst)
    ARGS.gate = GATE

    log(f"relay {VERSION} mode={ARGS.mode} listening on 127.0.0.1:{ARGS.port} -> "
        f"{ARGS.upstream_scheme}://{ARGS.upstream_host}:{ARGS.upstream_port}"
        f"{ARGS.upstream_prefix} via {ARGS.proxy or 'direct'}")
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
