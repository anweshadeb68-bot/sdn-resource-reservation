"""Reservation client (Member A).

One command:   python3.9 client/client.py RESERVE h1 h4 5 60
Interactive:   python3.9 client/client.py          (type commands, QUIT to leave)
Other server:  python3.9 client/client.py --server 10.0.0.4 STATUS
Pretend to be another machine (ownership test):
               python3.9 client/client.py --bind 127.0.0.2 RELEASE 1
"""

import argparse
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Network resource reservation client")
    parser.add_argument("--server", default="127.0.0.1",
                        help="server address (10.0.0.4 inside Mininet)")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    parser.add_argument("--bind", default=None,
                        help="local address to send from (to look like a different client)")
    parser.add_argument("command", nargs="*", help="e.g. RESERVE h1 h4 5 60")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if args.bind:
        sock.bind((args.bind, 0))   # port 0 = let the OS pick any free port
    sock.connect((args.server, args.port))

    # makefile turns the socket into something we can read line by line.
    # WHY: TCP may split or join messages; reading up to "\n" gives exactly
    # one reply, however the bytes arrived.
    reader = sock.makefile("r", encoding="utf-8", newline="\n")

    def ask(line):
        sock.sendall((line + "\n").encode("utf-8"))
        reply = reader.readline()
        if not reply:
            print("Server closed the connection")
            return None
        reply = reply.strip()
        print(reply)
        return reply

    try:
        if args.command:                       # one-shot mode
            ask(" ".join(args.command))
            ask("QUIT")
        else:                                  # interactive mode
            print("Connected. Type commands, or QUIT to leave.")
            while True:
                try:
                    line = input("> ").strip()
                except EOFError:
                    line = "QUIT"
                if not line:
                    continue
                if ask(line) is None or line == "QUIT":
                    break
    finally:
        reader.close()
        sock.close()


if __name__ == "__main__":
    main()
