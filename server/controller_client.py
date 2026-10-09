"""Talks to the SDN controller's web interface (REST API).

This is the "Approved booking" arrow on the diagram. The messages follow the
interface agreement exactly:

    POST   /reservations        {"id","src","dst","mbps","duration_s"}  -> 200
    DELETE /reservations/<id>                                           -> 200 (404 = already gone)
    GET    /stats/links         -> {"links":[{"link":"s1-s2","used_mbps":...}, ...]}
    GET    /health              -> 200

Uses only Python's built-in urllib, so nothing needs installing in Mininet.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

# An opener that ignores any proxy settings on the machine: the controller is
# always reached directly (localhost on the Mac, the management link in Mininet).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ControllerClient:
    def __init__(self, url=config.CONTROLLER_URL, timeout=config.CONTROLLER_TIMEOUT_S):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        """Send one HTTP request. Returns (status_code, parsed_json).
        status_code is None if the controller could not be reached in time."""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with _opener.open(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:          # controller answered with 4xx/5xx
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError):   # down, refused, timed out
            return None, {}

    def install(self, booking):
        """Ask the controller to enforce a new booking. True only on a 200."""
        status, _ = self._request("POST", "/reservations", {
            "id": booking.id,
            "src": config.HOSTS[booking.src],      # the agreement sends IPs, not names
            "dst": config.HOSTS[booking.dst],
            "mbps": booking.mbps,
            "duration_s": booking.seconds,
        })
        return status == 200

    def remove(self, booking_id):
        """Ask the controller to remove a booking's rules. 404 counts as done."""
        status, _ = self._request("DELETE", "/reservations/{}".format(booking_id))
        return status in (200, 404)

    def link_used_mbps(self, link="s1-s2"):
        """Measured traffic on a link in Mbps, or None if unavailable."""
        status, data = self._request("GET", "/stats/links")
        if status != 200:
            return None
        for item in data.get("links", []):
            if item.get("link") == link:
                return item.get("used_mbps")
        return None

    def healthy(self):
        status, _ = self._request("GET", "/health")
        return status == 200
