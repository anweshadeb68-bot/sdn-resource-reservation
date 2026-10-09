"""A stand-in for Member B's Ryu controller, for testing the server alone.

It answers the same 5 web requests as the real controller (see the interface
agreement) and prints everything it receives. It installs nothing.

    python3.9 tests/fake_controller.py              # normal: every booking accepted
    python3.9 tests/fake_controller.py --fail       # every POST fails (500)
    python3.9 tests/fake_controller.py --slow 5     # waits 5 s before answering

Then start the server pointed at it:
    python3.9 server/server.py --controller http://127.0.0.1:8080
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

installed = {}            # id -> booking body, like the real controller's rules
lock = threading.Lock()
settings = {"fail": False, "slow": 0.0}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):     # quieter than the default log line
        pass

    def _say(self, text):
        print("[fake controller] " + text, flush=True)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"status": "ok"})
        if self.path == "/reservations":
            with lock:
                return self._reply(200, {"reservations": list(installed.values())})
        if self.path == "/stats/links":
            with lock:
                reserved = sum(b["mbps"] for b in installed.values())
            # Pretend the booked traffic is flowing at its booked speed.
            return self._reply(200, {"timestamp": time.time(), "links": [
                {"link": "s1-s2", "capacity_mbps": 10, "reserved_mbps": reserved,
                 "used_mbps": float(reserved)}]})
        return self._reply(404, {"status": "error", "reason": "unknown path"})

    def do_POST(self):
        if self.path != "/reservations":
            return self._reply(404, {"status": "error", "reason": "unknown path"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._say("POST   booking {id}: {src} -> {dst}, {mbps} Mbps, {duration_s} s".format(**body))
        time.sleep(settings["slow"])
        if settings["fail"]:
            self._say("       -> refusing (running with --fail)")
            return self._reply(500, {"status": "error", "reason": "simulated failure"})
        with lock:
            installed[body["id"]] = body
        return self._reply(200, {"status": "installed", "id": body["id"]})

    def do_DELETE(self):
        prefix = "/reservations/"
        if not self.path.startswith(prefix) or not self.path[len(prefix):].isdigit():
            return self._reply(404, {"status": "error", "reason": "unknown path"})
        booking_id = int(self.path[len(prefix):])
        with lock:
            found = installed.pop(booking_id, None)
        if found is None:
            self._say("DELETE booking {}: not installed (404)".format(booking_id))
            return self._reply(404, {"status": "error", "reason": "not found"})
        self._say("DELETE booking {}: rules removed".format(booking_id))
        return self._reply(200, {"status": "removed", "id": booking_id})


def main():
    parser = argparse.ArgumentParser(description="Fake SDN controller for testing")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fail", action="store_true", help="refuse every POST")
    parser.add_argument("--slow", type=float, default=0.0, help="seconds to wait before answering a POST")
    args = parser.parse_args()
    settings["fail"], settings["slow"] = args.fail, args.slow
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("[fake controller] listening on 127.0.0.1:{}  fail={} slow={}s".format(
        args.port, args.fail, args.slow), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fake controller] stopped")


if __name__ == "__main__":
    main()
