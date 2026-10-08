"""Race test: many clients try to book the Moon link at the same instant.

The Moon link (s1-s2) has 8 Mbps reservable. Eight clients each ask for
2 Mbps at once, so exactly 4 should succeed (4 x 2 = 8 Mbps) and 4 be
rejected with insufficient_capacity. (2 Mbps each keeps the number of
bookings at 4, well under the 8-booking limit, so this really tests the
capacity check rather than the booking-count limit.)

    python3.9 tests/race_test.py

Without the lock in BookingTable.reserve you will usually see MORE than 4
accepted: the link has been promised more than it can carry.
"""

import os
import socket
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

SERVER = ("127.0.0.1", config.SERVER_PORT)
CLIENTS = 8
MBPS_EACH = 2
start_gun = threading.Barrier(CLIENTS)   # makes all threads send together
replies = []


def one_client(i):
    src = ["h1", "h2", "h3"][i % 3]
    with socket.create_connection(SERVER, timeout=30) as sock:
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        start_gun.wait()
        sock.sendall("RESERVE {} h4 {} 120\n".format(src, MBPS_EACH).encode("utf-8"))
        replies.append(reader.readline().strip())
        sock.sendall(b"QUIT\n")
        reader.readline()


threads = [threading.Thread(target=one_client, args=(i,)) for i in range(CLIENTS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

accepted = [r for r in replies if r.startswith("OK RESERVED")]
rejected = [r for r in replies if r.startswith("ERR")]
for r in sorted(replies):
    print(r)
print("\naccepted: {}   rejected: {}".format(len(accepted), len(rejected)))
booked = len(accepted) * MBPS_EACH
reasons_ok = all(r == "ERR REJECTED insufficient_capacity" for r in rejected)
if booked == 8 and reasons_ok:
    print("PASS: exactly 8 Mbps booked on an 8 Mbps link")
elif booked > 8:
    print("FAIL: {} Mbps promised on an 8 Mbps link (overbooked)".format(booked))
else:
    print("FAIL: {} Mbps booked, or a rejection had the wrong reason".format(booked))
print("(restart the server before running this again: bookings last 120 s)")
