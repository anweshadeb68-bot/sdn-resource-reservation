"""Automated application tests for the reservation server (Member A, D2: E5 and E6).

SIMULATED CONTROLLER: every case runs the real server against
tests/fake_controller.py, which answers like the real controller but installs no
switch rules. These results show the APPLICATION's behaviour (concurrency,
validation, error handling, timing). They are NOT evidence of SDN enforcement;
that comes from the Mininet experiments.

    python3.9 tests/app_tests.py                  # everything (about 40 s)
    python3.9 tests/app_tests.py --loads 10 25 50 100
    python3.9 tests/app_tests.py --only load invalid

Each case starts its own fresh server (and fake controller) on free ports, so
nothing is shared between cases and port 5000 is never needed. Results go to
tests/results/app_tests_<date-time>/ :
    results.csv    one row per request: case, condition, request, reply, ms, expected, pass
    requests_*.csv the server's own request log for each case
    summary.txt    one PASS/FAIL line per case (also printed)
"""

import argparse
import csv
import datetime
import os
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
from common import config  # noqa: E402

PY = sys.executable
RESERVABLE = config.LINKS["s1-s2"] * config.RESERVABLE_SHARE      # 8 Mbps


# --------------------------------------------------------------------------
# Starting and stopping processes
# --------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class Stack:
    """A fresh server (and optionally a fake controller) for one test case."""

    def __init__(self, out, name, controller="normal", idle_timeout=60):
        self.procs = []
        self.server_port = free_port()
        ctrl_port = free_port()
        if controller != "down":
            cmd = [PY, os.path.join(ROOT, "tests", "fake_controller.py"), "--port", str(ctrl_port)]
            if controller == "fail":
                cmd.append("--fail")
            elif controller.startswith("slow"):
                cmd += ["--slow", controller.split(":")[1]]
            self.procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_for_port(ctrl_port)
        self.request_log = os.path.join(out, "requests_%s.csv" % name)
        self.server_log = open(os.path.join(out, "server_%s.log" % name), "w")
        self.procs.append(subprocess.Popen(
            [PY, os.path.join(ROOT, "server", "server.py"), "--port", str(self.server_port),
             "--controller", "http://127.0.0.1:%d" % ctrl_port,
             "--request-log", self.request_log, "--idle-timeout", str(idle_timeout)],
            stdout=self.server_log, stderr=subprocess.STDOUT))
        if not wait_for_port(self.server_port, 8):
            raise RuntimeError("server did not start for case " + name)

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        self.server_log.close()

    def ask(self, line, timeout=10):
        """One request on a new connection: (reply, milliseconds)."""
        return ask(self.server_port, line, timeout)


def ask(port, line, timeout=10):
    start = time.perf_counter()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            sock.sendall((line + "\n").encode("utf-8"))
            reply = reader.readline().strip()
            ms = (time.perf_counter() - start) * 1000
            try:
                sock.sendall(b"QUIT\n")
            except OSError:
                pass
            return reply or "ERROR empty reply", ms
    except OSError as error:
        return "ERROR " + type(error).__name__, (time.perf_counter() - start) * 1000


def stats_dict(reply):
    return dict(p.split("=", 1) for p in reply.split()[2:] if "=" in p)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

class Results:
    def __init__(self, out):
        self.out = out
        self.rows = []
        self.cases = []         # (case, passed, detail)

    def row(self, case, condition, request, reply, ms, expected, passed):
        self.rows.append([case, condition, request, reply, "%.2f" % ms, expected,
                          "PASS" if passed else "FAIL"])

    def case(self, name, passed, detail):
        self.cases.append((name, passed, detail))
        print("%-26s %s  %s" % (name, "PASS" if passed else "FAIL", detail), flush=True)

    def save(self):
        with open(os.path.join(self.out, "results.csv"), "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["case", "condition", "request", "reply", "ms", "expected", "result"])
            writer.writerows(self.rows)
        passed = sum(1 for _, ok, _ in self.cases if ok)
        lines = ["Application tests, %s" % datetime.datetime.now().isoformat(timespec="seconds"),
                 "Controller: SIMULATED (tests/fake_controller.py). Not SDN enforcement.",
                 ""]
        lines += ["%-26s %s  %s" % (n, "PASS" if ok else "FAIL", d) for n, ok, d in self.cases]
        lines += ["", "%d of %d cases passed" % (passed, len(self.cases))]
        with open(os.path.join(self.out, "summary.txt"), "w") as handle:
            handle.write("\n".join(lines) + "\n")
        return passed, len(self.cases)


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------

def case_load(res, out, n):
    """N clients ask for 2 Mbps at the same instant: exactly 4 fit in 8 Mbps."""
    name = "load_%d" % n
    stack = Stack(out, name)
    try:
        barrier = threading.Barrier(n)
        replies = [None] * n

        def client(i):
            src = ("h1", "h2", "h3")[i % 3]
            line = "RESERVE %s h4 2 120" % src
            barrier.wait()
            replies[i] = (line,) + stack.ask(line, timeout=20)

        threads = [threading.Thread(target=client, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        expected_ok = int(RESERVABLE // 2)
        accepted = [r for r in replies if r[1].startswith("OK RESERVED")]
        rejected = [r for r in replies if r[1] == "ERR REJECTED insufficient_capacity"]
        errors = [r for r in replies if r[1].startswith("ERROR")]
        for line, reply, ms in replies:
            ok = reply.startswith("OK RESERVED") or reply == "ERR REJECTED insufficient_capacity"
            res.row(name, "%d clients at once, 2 Mbps each" % n, line, reply, ms,
                    "OK RESERVED or ERR REJECTED insufficient_capacity", ok)
        status, _ = stack.ask("STATUS")
        reserved = float(stats_dict(status).get("reserved", "nan"))
        ms_all = [r[2] for r in replies]
        detail = ("%d accepted, %d rejected, %d errors; %g of %g Mbps booked; "
                  "median %.1f ms, worst %.1f ms" % (len(accepted), len(rejected), len(errors),
                                                     reserved, RESERVABLE,
                                                     statistics.median(ms_all), max(ms_all)))
        passed = (len(accepted) == expected_ok and len(rejected) == n - expected_ok
                  and not errors and reserved <= RESERVABLE)
        res.case(name, passed, detail)

        # STATS must agree with the server's own request log.
        stats, ms = stack.ask("STATS")
        counts = stats_dict(stats)
        time.sleep(0.2)
        with open(stack.request_log) as handle:
            rows = [r for r in csv.DictReader(handle) if r["command"] == "RESERVE"]
        log_acc = sum(1 for r in rows if r["category"] == "accepted")
        log_rej = sum(1 for r in rows if r["category"] == "rejected")
        same = (int(counts.get("accepted", -1)) == log_acc == len(accepted)
                and int(counts.get("rejected", -1)) == log_rej == len(rejected))
        res.row(name + "_stats", "STATS vs request log", "STATS", stats, ms,
                "counts equal the CSV rows", same)
        res.case(name + "_stats", same, "STATS accepted=%s rejected=%s; log %d/%d"
                 % (counts.get("accepted"), counts.get("rejected"), log_acc, log_rej))
    finally:
        stack.stop()


INVALID = [
    ("RESERVE h1 h4 5", "ERR BAD_REQUEST wrong_field_count"),
    ("RESERVE h9 h4 5 60", "ERR BAD_REQUEST unknown_host"),
    ("RESERVE h1 h1 5 60", "ERR BAD_REQUEST same_host"),
    ("RESERVE h1 h4 five 60", "ERR BAD_REQUEST not_a_number"),
    ("RESERVE h1 h4 2.5 60", "ERR BAD_REQUEST not_a_number"),
    ("RESERVE h1 h4 9 60", "ERR BAD_REQUEST mbps_out_of_range"),
    ("RESERVE h1 h4 0 60", "ERR BAD_REQUEST mbps_out_of_range"),
    ("RESERVE h1 h4 5 5", "ERR BAD_REQUEST seconds_out_of_range"),
    ("RESERVE h1 h4 5 601", "ERR BAD_REQUEST seconds_out_of_range"),
    ("RELEASE abc", "ERR BAD_REQUEST usage: RELEASE <id>"),
    ("RELEASE 42", "ERR NOT_FOUND 42"),
    ("reserve h1 h4 5 60", "ERR BAD_REQUEST unknown_command"),
    ("FLY TO MARS", "ERR BAD_REQUEST unknown_command"),
]


def case_invalid(res, out):
    stack = Stack(out, "invalid")
    try:
        bad = 0
        for line, expected in INVALID:
            reply, ms = stack.ask(line)
            ok = reply == expected
            bad += not ok
            res.row("invalid", "bad input", line, reply, ms, expected, ok)
        status, _ = stack.ask("STATUS")
        nothing_booked = stats_dict(status).get("reserved") in ("0", "0.0")
        res.case("invalid", bad == 0 and nothing_booked,
                 "%d of %d bad requests got the expected reply; nothing booked: %s"
                 % (len(INVALID) - bad, len(INVALID), nothing_booked))
    finally:
        stack.stop()


def case_disconnect(res, out):
    """Clients that vanish mid-request must not affect anyone else."""
    stack = Stack(out, "disconnect")
    try:
        # 1. Half a line, then close politely.
        s = socket.create_connection(("127.0.0.1", stack.server_port))
        s.sendall(b"RESERVE h1 h4")
        s.close()
        # 2. Abrupt reset (RST) straight after sending a full request.
        s = socket.create_connection(("127.0.0.1", stack.server_port))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.sendall(b"STATUS\n")
        s.close()
        # 3. Connect and close without sending anything.
        socket.create_connection(("127.0.0.1", stack.server_port)).close()
        time.sleep(0.5)
        reply, ms = stack.ask("STATUS")
        ok = reply.startswith("OK STATUS")
        res.row("disconnect", "after 3 broken clients", "STATUS", reply, ms, "OK STATUS ...", ok)
        partial_ignored = "RESERVE h1 h4" not in open(stack.request_log).read() \
            if os.path.exists(stack.request_log) else True
        res.case("disconnect", ok and partial_ignored,
                 "server still answers; half-sent request was not processed: %s" % partial_ignored)
    finally:
        stack.stop()


def case_idle(res, out, timeout_s=2.0):
    """A silent client is disconnected after the idle timeout; others are unaffected."""
    stack = Stack(out, "idle", idle_timeout=timeout_s)
    try:
        silent = socket.create_connection(("127.0.0.1", stack.server_port))
        silent.settimeout(timeout_s + 5)
        start = time.perf_counter()
        reply_other, ms_other = stack.ask("STATUS")        # served while the silent one waits
        try:
            data = silent.recv(100)                         # b"" = server closed it
        except OSError:
            data = b""
        waited = time.perf_counter() - start
        silent.close()
        closed = data == b"" and timeout_s - 0.5 <= waited <= timeout_s + 3
        other_ok = reply_other.startswith("OK STATUS")
        res.row("idle", "silent client, timeout %.0f s" % timeout_s, "(nothing sent)",
                "closed by server" if data == b"" else repr(data), waited * 1000,
                "closed after about %.0f s" % timeout_s, closed)
        res.row("idle", "other client meanwhile", "STATUS", reply_other, ms_other,
                "OK STATUS ...", other_ok)
        res.case("idle", closed and other_ok,
                 "silent client closed after %.1f s; another client served in %.1f ms"
                 % (waited, ms_other))
    finally:
        stack.stop()


def case_controller(res, out, mode, label, expect_ms=None):
    """The controller refuses, is too slow, or is down: no promise without enforcement."""
    stack = Stack(out, "controller_" + label, controller=mode)
    try:
        reply, ms = stack.ask("RESERVE h1 h4 5 60", timeout=20)
        expected = "ERR REJECTED controller_error"
        ok = reply == expected
        status, _ = stack.ask("STATUS")
        nothing_saved = stats_dict(status).get("reserved") in ("0", "0.0")
        timing_ok = True
        if expect_ms:
            low, high = expect_ms
            timing_ok = low <= ms <= high
        res.row("controller_" + label, label, "RESERVE h1 h4 5 60", reply, ms, expected, ok)
        res.row("controller_" + label, label, "STATUS", status, 0, "reserved=0", nothing_saved)
        res.case("controller_" + label, ok and nothing_saved and timing_ok,
                 "%s in %.0f ms; nothing booked: %s" % (reply, ms, nothing_saved))
    finally:
        stack.stop()


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Reservation server application tests")
    parser.add_argument("--loads", type=int, nargs="+", default=[10, 25, 50])
    parser.add_argument("--only", nargs="+", default=None,
                        help="run only some groups: load invalid disconnect idle controller")
    args = parser.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(ROOT, "tests", "results", "app_tests_" + stamp)
    os.makedirs(out, exist_ok=True)
    res = Results(out)
    want = (lambda g: args.only is None or g in args.only)
    print("Controller: SIMULATED (tests/fake_controller.py). Results in %s\n" % out, flush=True)

    if want("load"):
        for n in args.loads:
            case_load(res, out, n)
    if want("invalid"):
        case_invalid(res, out)
    if want("disconnect"):
        case_disconnect(res, out)
    if want("idle"):
        case_idle(res, out)
    if want("controller"):
        case_controller(res, out, "fail", "refuses")
        timeout_ms = config.CONTROLLER_TIMEOUT_S * 1000
        case_controller(res, out, "slow:5", "too_slow", (timeout_ms - 500, timeout_ms + 1500))
        case_controller(res, out, "down", "down")

    passed, total = res.save()
    print("\n%d of %d cases passed. Saved: %s" % (passed, total, out))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
