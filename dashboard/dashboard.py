"""Mission Control dashboard: a web page for booking the Moon link.

The browser talks to this small web backend; this backend talks to the
reservation server with the SAME TCP commands as client/client.py
(RESERVE, RELEASE, STATUS, LIST). Nothing in the server changes.

On the Mac (server running locally):
    python3.9 dashboard/dashboard.py
    then open http://127.0.0.1:8000

In the VM (server running on h4 inside Mininet), reached over the
management link:
    python3 dashboard/dashboard.py --server 192.168.100.4
    then open http://127.0.0.1:8000 in the VM's browser
    (the controller is read from http://127.0.0.1:8080; override with --controller)

Deliverable 2 additions (all read-only; booking/cancel behaviour is unchanged):
  * a background poller reads the controller (default http://127.0.0.1:8080,
    change with --controller) every 2 s and keeps the last 120 s of MEASURED
    traffic (s1->s2 counters) next to RESERVED Mbps, for the live chart;
  * controller congestion-policy status (the "d2_congestion" block of
    GET /stats/links) is passed through for the limiter panel;
  * request outcome counts come from the server's STATS command if the
    server has one; otherwise the panel says so (nothing is invented);
  * health is reported per component, because controller /health = 200 alone
    does not prove that the switches and queues are ready.

Standard library only.
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from common import config  # noqa: E402

SERVER = {"host": "127.0.0.1", "port": config.SERVER_PORT}
CONTROLLER = {"url": "http://127.0.0.1:8080"}   # dashboard runs on the VM host, next to Ryu

HISTORY_S = 120            # seconds of measured traffic kept for the chart
POLL_S = 2                 # same rhythm as the controller's own statistics loop
STALE_S = 3 * config.STATS_INTERVAL_S   # the controller itself calls a reading stale after this
REQUEST_POLL_S = 10        # how often the server's STATS command is tried

# Fields of the controller's "d2_congestion" block that are passed to the page.
POLICY_FIELDS = ("enabled", "active", "limited_host", "limited_pair", "queue", "priority",
                 "udp_cap_mbps", "congested", "candidate_host", "trigger_samples",
                 "last_action", "last_reason", "ingress_mbps")

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never use a proxy
LOCK = threading.Lock()
HISTORY = deque()          # one sample per poll: see poll_controller()
MONITOR = {"controller": {"state": "unknown"}, "policy": None, "requests": {"state": "unknown"},
           "checked_at": None, "requests_checked_at": 0.0}

FRIENDLY = {
    "insufficient_capacity": "Not enough room on the Moon link for that booking.",
    "max_reservations": "All 8 protected lanes are in use. Cancel one first.",
    "controller_error": "Network control did not confirm the lane, so nothing was booked.",
    "wrong_field_count": "The request was incomplete.",
    "unknown_host": "Unknown module.",
    "same_host": "A module cannot book a link to itself.",
    "not_a_number": "Speed and duration must be whole numbers.",
    "mbps_out_of_range": "Speed must be {}–{} Mbps.".format(config.MIN_MBPS, config.MAX_MBPS),
    "seconds_out_of_range": "Duration must be {}–{} seconds.".format(config.MIN_SECONDS, config.MAX_SECONDS),
}


def ask_server(command):
    """Send one command to the reservation server and return its reply line,
    or None if the server cannot be reached."""
    try:
        with socket.create_connection((SERVER["host"], SERVER["port"]), timeout=5) as sock:
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            sock.sendall((command + "\n").encode("utf-8"))
            reply = reader.readline().strip()
            sock.sendall(b"QUIT\n")
            return reply or None
    except OSError:
        return None


def parse_status(reply):
    # OK STATUS capacity=10 reserved=5 free=3 used=4.8 active=1
    fields = dict(part.split("=", 1) for part in reply.split()[2:] if "=" in part)
    used = fields.get("used", "unknown")
    return {
        "capacity": float(fields.get("capacity", 0)),
        "reserved": float(fields.get("reserved", 0)),
        "free": float(fields.get("free", 0)),
        "used": None if used == "unknown" else float(used),
        "active": int(fields.get("active", 0)),
    }


def parse_list(reply):
    # OK LIST 2 7:h1>h4:5:42 8:h2>h4:2:10
    bookings = []
    for item in reply.split()[3:]:
        booking_id, route, mbps, left = item.split(":")
        src, dst = route.split(">")
        bookings.append({"id": int(booking_id), "src": src, "dst": dst,
                         "mbps": int(mbps), "left": int(left)})
    return bookings


def explain(reply):
    """Turn a server reply into (ok, sentence for the page)."""
    if reply is None:
        return False, "Mission Control server is not reachable."
    parts = reply.split()
    if parts[0] == "OK":
        return True, reply
    reason = parts[2] if len(parts) > 2 else parts[-1]
    if parts[1] == "FORBIDDEN":
        return False, "That booking belongs to another station, so it can't be cancelled from here."
    if parts[1] == "NOT_FOUND":
        return False, "That booking has already ended."
    return False, FRIENDLY.get(reason, reply)


def controller_get(path):
    """GET one controller path. Returns (status, json). status is None if unreachable."""
    try:
        with _direct.open(urllib.request.Request(CONTROLLER["url"] + path), timeout=2) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return None, {}


def number_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def poll_controller(now):
    """Read /health and /stats/links once and store one history sample.

    Missing readings are stored as None (shown as "unknown"), never as 0."""
    health_code, _ = controller_get("/health")
    sample = {"t": now, "measured": None, "basis": None, "reserved": None, "limiter": None}
    policy = None
    if health_code is None:
        controller = {"state": "unreachable", "measurements": "unknown",
                      "detail": "No answer from {}".format(CONTROLLER["url"])}
    elif health_code != 200:
        controller = {"state": "error", "measurements": "unknown",
                      "detail": "/health returned HTTP {}".format(health_code)}
    else:
        stats_code, body = controller_get("/stats/links")
        link = None
        if stats_code == 200:
            for item in body.get("links", []):
                if item.get("link") == "s1-s2":
                    link = item
        if link is not None:
            one_way = number_or_none(link.get("used_mbps_s1_to_s2"))
            if one_way is not None:
                sample["measured"], sample["basis"] = one_way, "s1_to_s2"
            else:    # older controller: only the two-way total exists
                sample["measured"] = number_or_none(link.get("used_mbps"))
                sample["basis"] = "total" if sample["measured"] is not None else None
            sample["reserved"] = number_or_none(link.get("reserved_mbps"))
            block = link.get("d2_congestion")
            if isinstance(block, dict):
                policy = dict((key, block.get(key)) for key in POLICY_FIELDS)
                if isinstance(block.get("active"), bool):
                    sample["limiter"] = block["active"]
            controller = {"state": "up", "measurements": "fresh",
                          "detail": "Link counters are fresh."}
        elif stats_code == 503:
            controller = {"state": "up", "measurements": "unavailable",
                          "detail": body.get("reason", "no fresh measurement yet")}
        else:
            controller = {"state": "up", "measurements": "unavailable",
                          "detail": "/stats/links gave {}".format(
                              "no answer" if stats_code is None else "HTTP {}".format(stats_code))}
    with LOCK:
        HISTORY.append(sample)
        while HISTORY and HISTORY[0]["t"] < now - HISTORY_S - POLL_S:
            HISTORY.popleft()
        MONITOR["controller"], MONITOR["policy"], MONITOR["checked_at"] = controller, policy, now


def parse_request_stats(reply):
    """Read the server's STATS reply. The format is Anwesha's to define, so this accepts
    either  OK STATS accepted=3 rejected=1 invalid=2 [reason_x=1 ...]  or  OK STATS {json}.
    Anything else is reported as such; no value is ever made up."""
    if reply is None:
        return {"state": "unreachable"}
    if not reply.startswith("OK STATS"):
        return {"state": "unsupported", "detail": reply[:120]}
    rest = reply[len("OK STATS"):].strip()
    try:
        data = json.loads(rest)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        data = {}
        for part in rest.split():
            if "=" in part:
                key, value = part.split("=", 1)
                data[key] = value
    counts, reasons = {}, {}
    for key, value in data.items():
        if key == "reasons" and isinstance(value, dict):
            for name, number in value.items():
                if isinstance(number, int):
                    reasons[str(name)] = number
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if key in ("accepted", "rejected", "invalid"):
            counts[key] = number
        else:
            reasons[key[7:] if key.startswith("reason_") else key] = number
    if not counts:
        return {"state": "unrecognised", "detail": reply[:120]}
    counts.update({"state": "ok", "reasons": reasons})
    return counts


def monitor_loop():
    """Runs forever in its own thread: keeps the traffic history and request counts fresh."""
    while True:
        started = time.time()
        try:
            poll_controller(started)
            if started - MONITOR["requests_checked_at"] >= REQUEST_POLL_S:
                requests_view = parse_request_stats(ask_server("STATS"))
                with LOCK:
                    MONITOR["requests"] = requests_view
                    MONITOR["requests_checked_at"] = started
        except Exception as error:      # the poller must never die
            print("monitor error: {}".format(error), flush=True)
        time.sleep(max(0.2, POLL_S - (time.time() - started)))


def health_view(server_ok, now):
    """One status per component. 'ok' is only claimed for what was actually observed."""
    with LOCK:
        controller = dict(MONITOR["controller"])
        checked_at = MONITOR["checked_at"]
    if checked_at is None:
        controller = {"state": "unknown", "measurements": "unknown", "detail": "Not checked yet."}
    elif now - checked_at > STALE_S:
        controller = {"state": controller.get("state", "unknown"), "measurements": "stale",
                      "detail": "Last controller check was {:.0f} s ago.".format(now - checked_at)}
    return {"server": "ok" if server_ok else "unreachable", "controller": controller,
            "note": "Controller /health only shows the web interface is running. Fresh link "
                    "counters show both switches are reporting, but queue setup is not "
                    "reported, so a booking can still be refused."}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            now = time.time()
            status_reply = ask_server("STATUS")
            list_reply = ask_server("LIST")
            server_ok = bool(status_reply and status_reply.startswith("OK STATUS")
                             and list_reply and list_reply.startswith("OK LIST"))
            with LOCK:
                extra = {"history": list(HISTORY), "policy": MONITOR["policy"],
                         "requests": dict(MONITOR["requests"])}
            extra.update({"now": now, "health": health_view(server_ok, now),
                          "history_s": HISTORY_S, "poll_s": POLL_S})
            if not server_ok:
                extra["server_ok"] = False
                return self._send(200, extra)
            state = parse_status(status_reply)
            state.update(extra)
            state.update({
                "server_ok": True,
                "reservable": config.LINKS["s1-s2"] * config.RESERVABLE_SHARE,
                "max_active": config.MAX_ACTIVE,
                "bookings": parse_list(list_reply),
                "labels": config.HOST_LABELS,
                "limits": {"min_mbps": config.MIN_MBPS, "max_mbps": config.MAX_MBPS,
                           "min_s": config.MIN_SECONDS, "max_s": config.MAX_SECONDS},
            })
            return self._send(200, state)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if self.path == "/api/reserve":
            command = "RESERVE {} {} {} {}".format(
                body.get("src", ""), config.SERVER_HOST, body.get("mbps", ""), body.get("seconds", ""))
        elif self.path == "/api/release":
            command = "RELEASE {}".format(body.get("id", ""))
        else:
            return self._send(404, {"error": "not found"})
        reply = ask_server(command)
        ok, message = explain(reply)
        return self._send(200, {"ok": ok, "message": message, "reply": reply})


def main():
    parser = argparse.ArgumentParser(description="Mission Control dashboard")
    parser.add_argument("--server", default="127.0.0.1",
                        help="reservation server address (192.168.100.4 from the VM)")
    parser.add_argument("--port", type=int, default=8000, help="web page port")
    parser.add_argument("--controller", default=CONTROLLER["url"],
                        help="Ryu REST address for measured traffic and policy status "
                             "(default %(default)s; Ryu runs on the VM host)")
    args = parser.parse_args()
    SERVER["host"] = args.server
    CONTROLLER["url"] = args.controller.rstrip("/")
    threading.Thread(target=monitor_loop, name="Monitor", daemon=True).start()
    web = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Dashboard on http://127.0.0.1:{}  (reservation server {}:{}, controller {})".format(
        args.port, SERVER["host"], SERVER["port"], CONTROLLER["url"]), flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")


if __name__ == "__main__":
    main()
