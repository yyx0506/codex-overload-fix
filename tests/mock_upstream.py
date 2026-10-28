#!/usr/bin/env python3
"""Programmable fake upstream for the relay's own test suite.

Usage:
    python tests/mock_upstream.py <mode> <fails> [port] [--delay MS]

Modes
    sse502      first <fails> attempts answer with the real in-band overload
                event inside an HTTP 200 SSE stream (the production shape)
    http503     first <fails> attempts answer HTTP 503 immediately
    cooling     first <fails> attempts answer HTTP 503 in <1 ms, imitating
                cockpit-tools' whole-pool cooldown short-circuit
    postcommit  stream tokens, then emit overload (must NOT be retried)
    image       overload once, then a stream whose first content event is an
                image-generation event (not output_text)
    slow        overload once, then a stream that stalls <delay> ms before
                its first byte (commit-deadline behaviour)
    always503   every attempt answers HTTP 503
    json        always answers a complete non-streaming 200 application/json
                answer (what `stream: false` really returns)
    jsonoverload  first <fails> attempts answer 200 application/json whose body
                is the overload error document, then the normal JSON answer
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = sys.argv[1] if len(sys.argv) > 1 else "sse502"
FAILS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 18090
DELAY_MS = 0
if "--delay" in sys.argv:
    DELAY_MS = int(sys.argv[sys.argv.index("--delay") + 1])

HITS = {"n": 0}
OVERLOAD = {"type": "error", "sequence_number": 3,
            "error": {"type": "service_unavailable_error",
                      "code": "server_is_overloaded",
                      "message": "Our servers are currently overloaded. Please try again later."}}


def created(model="mock-model"):
    return {"type": "response.created", "sequence_number": 0,
            "response": {"id": "resp_mock", "object": "response", "status": "in_progress",
                         "model": model, "output": []}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for ev in events:
            self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
            self.wfile.flush()
        self.close_connection = True

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self._json(200, {"hits": HITS["n"], "mode": MODE})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        HITS["n"] += 1
        hit = HITS["n"]
        fail = hit <= FAILS
        print(f"mock hit {hit} mode={MODE} fail={fail}", flush=True)

        if MODE in ("json", "jsonoverload"):
            if MODE == "jsonoverload" and fail:
                self._json(200, OVERLOAD)
                return
            self._json(200, {"id": "resp_mock", "object": "response",
                             "status": "completed", "model": "mock-model",
                             "output": [], "echo_len": len(raw)})
            return
        if MODE == "always503":
            self._json(503, {"error": {"code": "server_is_overloaded"}})
            return
        if MODE == "http503" and fail:
            self._json(503, {"error": {"code": "server_is_overloaded"}})
            return
        if MODE == "cooling" and fail:
            # No delay at all: this is what cockpit's cooldown looks like.
            self._json(503, {"error": {"code": "pool_cooling"}})
            return
        if MODE == "sse502" and fail:
            self._sse([OVERLOAD])
            return
        if MODE == "postcommit":
            self._sse([created(),
                       {"type": "response.output_text.delta", "sequence_number": 1, "delta": "PARTIAL"},
                       OVERLOAD])
            return
        if MODE == "slow" and DELAY_MS:
            time.sleep(DELAY_MS / 1000.0)
        if MODE == "image":
            if fail:
                self._sse([OVERLOAD])
                return
            if DELAY_MS:
                time.sleep(DELAY_MS / 1000.0)
            self._sse([
                created("gpt-image-2.5"),
                {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                 "item": {"id": "ig_1", "type": "image_generation_call", "status": "in_progress"}},
                {"type": "response.image_generation_call.partial_image",
                 "sequence_number": 2, "output_index": 0, "partial_image_b64": "AAAA"},
                {"type": "response.image_generation_call.completed",
                 "sequence_number": 3, "output_index": 0, "result": "AAAA"},
                {"type": "response.completed", "sequence_number": 4,
                 "response": {"id": "resp_mock", "status": "completed", "model": "gpt-image-2.5"}},
            ])
            return

        self._sse([
            created(),
            {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0},
            {"type": "response.output_text.delta", "sequence_number": 2, "delta": "MOCK_"},
            {"type": "response.output_text.delta", "sequence_number": 3, "delta": "OK"},
            {"type": "response.completed", "sequence_number": 4,
             "response": {"id": "resp_mock", "status": "completed", "model": "mock-model"}},
        ])


if __name__ == "__main__":
    print(f"mock upstream on {PORT} mode={MODE} fails={FAILS}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
