"""Response-time client for experiment E4 (TCP responsiveness).

Sends the same read-only command (STATUS by default) many times and records how
long each one takes, from opening the connection to receiving the reply line.
It never books anything, so it is safe to run during other experiments.

    python3 tests/timing_client.py --server 10.0.0.4 --count 50 --label flood_booked --out e4.csv

Each CSV row: label, request number, start time, milliseconds, reply (or the error).
A summary (median, 95th percentile, worst, failures) is printed at the end.
Standard library only, so it runs inside a Mininet host as it is.
"""

import argparse
import csv
import os
import socket
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402


def one_request(server, port, command, timeout):
    """Return (milliseconds, reply). reply starts with ERROR on failure."""
    start = time.perf_counter()
    try:
        with socket.create_connection((server, port), timeout=timeout) as sock:
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            sock.sendall((command + "\n").encode("utf-8"))
            reply = reader.readline().strip()
            elapsed = (time.perf_counter() - start) * 1000
            try:
                sock.sendall(b"QUIT\n")
            except OSError:
                pass
            return elapsed, reply or "ERROR empty reply"
    except OSError as error:
        return (time.perf_counter() - start) * 1000, "ERROR " + type(error).__name__


def percentile(values, share):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser(description="Measure reservation-server response times")
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    parser.add_argument("--command", default="STATUS", help="read-only command to time")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--interval", type=float, default=0.2, help="seconds between requests")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--label", default="unlabelled", help="condition name stored in every row")
    parser.add_argument("--out", default=None, help="CSV file to append to")
    args = parser.parse_args()

    rows = []
    for number in range(1, args.count + 1):
        started = time.time()
        ms, reply = one_request(args.server, args.port, args.command, args.timeout)
        rows.append([args.label, number, "%.3f" % started, "%.2f" % ms, reply])
        time.sleep(args.interval)

    if args.out:
        new_file = not os.path.exists(args.out)
        with open(args.out, "a", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["label", "request", "start_time", "ms", "reply"])
            writer.writerows(rows)

    ok = [float(r[3]) for r in rows if not r[4].startswith("ERROR")]
    failed = len(rows) - len(ok)
    if ok:
        print("{}: {} ok, {} failed | median {:.1f} ms, p95 {:.1f} ms, worst {:.1f} ms".format(
            args.label, len(ok), failed, statistics.median(ok), percentile(ok, 0.95), max(ok)))
    else:
        print("{}: all {} requests failed".format(args.label, failed))


if __name__ == "__main__":
    main()
