"""Request log and outcome counters for the reservation server (Member A, D2).

Every request the server answers becomes one CSV row:

    time, iso_time, client_ip, command, request, category, reason, reply, ms

`ms` is the server's processing time for that request: from receiving the
line to having the reply ready (it includes the controller call for RESERVE).

Categories (what each request counts as):
    RESERVE  accepted | rejected (valid, but refused: no room, 8 lanes, controller) | invalid (bad input)
    RELEASE  released | forbidden | not_found | invalid
    STATUS, LIST, STATS                     query   (ordinary successful queries, never "accepted")
    anything else (unknown or empty line)   bad_command
(the bad_commands counter also includes badly formed RELEASE requests)

The same counters answer the read-only STATS command:

    OK STATS reserve_requests=14 accepted=9 rejected=3 invalid=2 insufficient_capacity=2
             max_reservations=0 controller_error=1 released=4 forbidden=1 not_found=0
             queries=120 bad_commands=1 log_errors=0

(one line; reserve_requests = accepted + rejected + invalid, and the three reason
counts add up to rejected). Counts cover everything since the server started; a
STATS request does not include itself.

Many client threads write at once, so one lock guards both the counters and the
file. A failed write is counted (log_errors) and printed once, but never stops
the client's reply.
"""

import csv
import datetime
import os
import threading
import time

COLUMNS = ["time", "iso_time", "client_ip", "command", "request", "category",
           "reason", "reply", "ms"]
REJECT_REASONS = ("insufficient_capacity", "max_reservations", "controller_error")
QUERY_COMMANDS = ("STATUS", "LIST", "STATS")
COUNTER_ORDER = ["reserve_requests", "accepted", "rejected", "invalid",
                 "insufficient_capacity", "max_reservations", "controller_error",
                 "released", "forbidden", "not_found", "queries", "bad_commands",
                 "log_errors"]


def categorize(line, reply):
    """(command, category, reason) for one request line and its reply line."""
    parts = line.split()
    command = parts[0] if parts else ""
    words = reply.split()
    status = " ".join(words[:2])                    # e.g. "OK RESERVED", "ERR REJECTED"
    reason = words[2] if len(words) > 2 and words[0] == "ERR" else ""

    if command == "RESERVE":
        if status == "OK RESERVED":
            return command, "accepted", ""
        if status == "ERR REJECTED":
            return command, "rejected", reason
        return command, "invalid", reason
    if command == "RELEASE":
        if status == "OK RELEASED":
            return command, "released", ""
        if status == "ERR FORBIDDEN":
            return command, "forbidden", ""
        if status == "ERR NOT_FOUND":
            return command, "not_found", ""
        return command, "invalid", "bad_release"
    if command in QUERY_COMMANDS and words[:1] == ["OK"]:
        return command, "query", ""
    return command or "(empty)", "bad_command", reason


class RequestLog:
    def __init__(self, path=None):
        """path=None keeps counters only (no file)."""
        self.path = path
        self._lock = threading.Lock()
        self._counts = dict((name, 0) for name in COUNTER_ORDER)
        self._warned = False
        if path:
            folder = os.path.dirname(os.path.abspath(path))
            os.makedirs(folder, exist_ok=True)

    def record(self, client_ip, line, reply, ms):
        """Count one answered request and append its CSV row. Never raises."""
        command, category, reason = categorize(line, reply)
        now = time.time()
        row = ["%.3f" % now,
               datetime.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
               client_ip, command, line, category, reason, reply, "%.3f" % ms]
        with self._lock:
            self._count(command, category, reason)
            if not self.path:
                return
            try:
                new_file = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
                with open(self.path, "a", newline="") as handle:
                    writer = csv.writer(handle)
                    if new_file:
                        writer.writerow(COLUMNS)
                    writer.writerow(row)
            except OSError as error:
                self._counts["log_errors"] += 1
                if not self._warned:
                    print("[RequestLog] WARNING: cannot write {}: {} "
                          "(replies continue; counting failures)".format(self.path, error),
                          flush=True)
                    self._warned = True

    def _count(self, command, category, reason):
        c = self._counts
        if command == "RESERVE":
            c["reserve_requests"] += 1
            c[category] += 1                     # accepted / rejected / invalid
            if category == "rejected" and reason in REJECT_REASONS:
                c[reason] += 1
        elif category in ("released", "forbidden", "not_found"):
            c[category] += 1
        elif category == "query":
            c["queries"] += 1
        elif category == "bad_command" or category == "invalid":
            c["bad_commands"] += 1

    def counts(self):
        with self._lock:
            return dict(self._counts)

    def stats_reply(self):
        c = self.counts()
        return "OK STATS " + " ".join("{}={}".format(name, c[name]) for name in COUNTER_ORDER)
