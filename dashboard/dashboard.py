"""Mission Control dashboard: a web page for booking the Moon link.

The browser talks to this small web backend; this backend talks to the
reservation server with the SAME TCP commands as client/client.py
(RESERVE, RELEASE, STATUS, LIST). Nothing in the server changes.

On the Mac (server and fake controller running locally):
    python3.9 dashboard/dashboard.py --controller http://127.0.0.1:8080
    then open http://127.0.0.1:8000

In the VM (server running on h4 inside Mininet, reached over the
management link; the controller runs on the VM itself):
    python3 dashboard/dashboard.py --server 192.168.100.4
    then open http://127.0.0.1:8000 in the VM's browser

D2 additions (read-only, nothing is changed on the server or controller):
  * link usage, reserved total and the congestion policy status come from the
    controller's GET /stats/links (the "d2_congestion" block, if present);
  * request counts come from the server's STATS command, once the server has it.
    Until then the page says the counts are not available.

Standard library only.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from common import config  # noqa: E402

SERVER = {"host": "127.0.0.1", "port": config.SERVER_PORT}
CONTROLLER = {"url": config.CONTROLLER_URL.rstrip("/")}

# Ignore any proxy settings: the controller is always reached directly.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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


def parse_stats(reply):
    """Request counts from the server's STATS reply, or None if unavailable.

    Expected shape (agreed with Member A):
    OK STATS reserve_requests=12 accepted=7 rejected=3 invalid=2 insufficient_capacity=2 ...
    Every key=value pair is passed through, so new counters need no change here.
    """
    if not reply or not reply.startswith("OK STATS"):
        return None
    counts = {}
    for part in reply.split()[2:]:
        if "=" in part:
            key, value = part.split("=", 1)
            try:
                counts[key] = int(value)
            except ValueError:
                counts[key] = value
    return counts


def ask_controller():
    """Link figures and congestion-policy status from the controller (read-only).

    Returns {"ok": False} if the controller cannot be reached, and
    {"ok": True, "link": None} if it answers but has no fresh measurement yet.
    """
    try:
        with _opener.open(CONTROLLER["url"] + "/stats/links", timeout=1.5) as resp:
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 503 = controller up, but no fresh measurement yet (switches not ready).
        return {"ok": True, "link": None, "note": "no fresh measurement yet (HTTP %d)" % e.code}
    except (urllib.error.URLError, OSError, ValueError):
        return {"ok": False}
    link = next((item for item in data.get("links", []) if item.get("link") == "s1-s2"), None)
    if link is None:
        return {"ok": True, "link": None, "note": "no s1-s2 figures in the reply"}
    used_forward = link.get("used_mbps_s1_to_s2")
    return {
        "ok": True,
        "link": {
            "used_mbps": link.get("used_mbps"),
            # Moon -> Earth direction, the one the bookings and the flood use.
            "used_moon_to_earth": used_forward if used_forward is not None else link.get("used_mbps"),
            "reserved_mbps": link.get("reserved_mbps"),
        },
        "policy": link.get("d2_congestion"),   # None for controllers without D2
    }


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
            status_reply = ask_server("STATUS")
            list_reply = ask_server("LIST")
            controller = ask_controller()
            if not (status_reply and status_reply.startswith("OK STATUS")
                    and list_reply and list_reply.startswith("OK LIST")):
                return self._send(200, {"server_ok": False, "controller": controller})
            state = parse_status(status_reply)
            state.update({
                "server_ok": True,
                "reservable": config.LINKS["s1-s2"] * config.RESERVABLE_SHARE,
                "max_active": config.MAX_ACTIVE,
                "bookings": parse_list(list_reply),
                "controller": controller,
                "requests": parse_stats(ask_server("STATS")),
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
    parser.add_argument("--controller", default=config.CONTROLLER_URL,
                        help="controller web interface for link and policy status "
                             "(on the Mac with the fake controller: http://127.0.0.1:8080)")
    args = parser.parse_args()
    SERVER["host"] = args.server
    CONTROLLER["url"] = args.controller.rstrip("/")
    web = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Dashboard on http://127.0.0.1:{}  (reservation server {}:{}, controller {})".format(
        args.port, SERVER["host"], SERVER["port"], CONTROLLER["url"]), flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")


if __name__ == "__main__":
    main()
