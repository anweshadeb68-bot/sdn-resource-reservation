"""Concurrency test: start N clients at the same moment and time them.

Each client connects, sends one command, reads the reply, then keeps the
connection open for HOLD_S seconds (like a slow user) before sending QUIT.

    python3.9 tests/many_clients.py            # 5 clients, 2 s each
    python3.9 tests/many_clients.py 10 1       # 10 clients, 1 s each

One-client-at-a-time server: total time is about N x HOLD_S.
Threaded server:             total time is about HOLD_S.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

SERVER = ("127.0.0.1", config.SERVER_PORT)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
HOLD_S = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

results = {}


def one_client(i):
    start = time.time()
    try:
        with socket.create_connection(SERVER, timeout=N * HOLD_S + 10) as sock:
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            sock.sendall("RESERVE h1 h4 1 60\n".encode("utf-8"))
            reply = reader.readline().strip()
            time.sleep(HOLD_S)  # a slow user, holding the connection open
            sock.sendall(b"QUIT\n")
            reader.readline()
    except OSError as e:
        reply = "ERROR {}".format(e)
    results[i] = (reply, time.time() - start)


threads = [threading.Thread(target=one_client, args=(i,)) for i in range(N)]
t0 = time.time()
for t in threads:
    t.start()
for t in threads:
    t.join()
total = time.time() - t0

for i in range(N):
    reply, took = results[i]
    print("client {:2d}: {:5.1f} s  {}".format(i + 1, took, reply))
print("\n{} clients, {} s each -> total {:.1f} s".format(N, HOLD_S, total))
print("(about {:.0f} s means one at a time; about {:.0f} s means concurrent)".format(
    N * HOLD_S, HOLD_S))
