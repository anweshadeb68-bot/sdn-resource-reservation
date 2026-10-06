"""Reservation server (Member A).

Day 2 version: accepts clients, reads one command per line, replies one line.
Booking logic comes on Day 3 (server/bookings.py); for now RESERVE etc.
send placeholder replies.

Run on your Mac:        python3.9 server/server.py
Run inside Mininet h4:  python3 server/server.py --host 0.0.0.0
"""

import argparse
import os
import socket
import sys
import threading

# Make "from common import config" work when run as python3.9 server/server.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import config  # noqa: E402

IDLE_TIMEOUT_S = 60  # close a connection that sends nothing for this long


def log(msg):
    """Print with the thread name, so you can see which client each line is about."""
    print("[{}] {}".format(threading.current_thread().name, msg), flush=True)


def send_line(conn, text):
    """Send one reply line. Every message ends with \\n so the other side
    knows where it stops (TCP is a stream: it has no message boundaries)."""
    conn.sendall((text + "\n").encode("utf-8"))


def handle_request(line, client_ip):
    """Turn one request line into one reply line. Never raises."""
    parts = line.split()
    if not parts:
        return "ERR BAD_REQUEST empty_line"

    command = parts[0]

    if command == "RESERVE":
        # Format: RESERVE <src> <dst> <mbps> <seconds>
        #
        # TODO 3 (Day 2): validate the request before anything else.
        #   Return "ERR BAD_REQUEST <detail>" when:
        #     - there are not exactly 5 parts           -> detail "wrong_field_count"
        #     - src or dst is not in config.HOSTS       -> detail "unknown_host"
        #     - src == dst                              -> detail "same_host"
        #     - mbps or seconds is not a whole number   -> detail "not_a_number"
        #       hint: int("5") works, int("5.5") and int("abc") raise ValueError
        #     - mbps outside config.MIN_MBPS..config.MAX_MBPS          -> "mbps_out_of_range"
        #     - seconds outside config.MIN_SECONDS..config.MAX_SECONDS -> "seconds_out_of_range"
        #   WHY: anything a client sends could be wrong or hostile. Checking it
        #   here, once, means the booking code (Day 3) can trust its inputs,
        #   and bad input can never crash the server.
        return "OK RESERVED 0 {} {}".format(parts[3] if len(parts) > 3 else "?",
                                            parts[4] if len(parts) > 4 else "?")

    if command == "RELEASE":
        if len(parts) != 2 or not parts[1].isdigit():
            return "ERR BAD_REQUEST usage: RELEASE <id>"
        return "OK RELEASED {}".format(parts[1])          # placeholder until Day 3

    if command == "STATUS":
        return "OK STATUS capacity=10 reserved=0 free=8 used=unknown active=0"  # placeholder

    if command == "LIST":
        return "OK LIST 0"                                 # placeholder

    return "ERR BAD_REQUEST unknown_command"


def handle_client(conn, addr):
    """Serve one client until it sends QUIT, hangs up, or goes idle."""
    client_ip = addr[0]
    log("connected from {}:{}".format(addr[0], addr[1]))

    # TODO 2 (Day 2): idle timeout.
    #   Make conn give up waiting after IDLE_TIMEOUT_S seconds of silence.
    #   hint: one line, a socket method that starts with "set..."
    #   Then, below, catch the error it raises (socket.timeout), log
    #   "idle timeout", and fall through to closing the connection.
    #   WHY: a client that connects and never speaks would otherwise hold a
    #   thread and a socket open forever. Enough of them and the server runs
    #   out of resources -- a real attack called "slowloris".
    conn.settimeout(IDLE_TIMEOUT_S)
    reader = conn.makefile("r", encoding="utf-8", newline="\n")
    try:
        for raw in reader:                 # one iteration per line received
            line = raw.strip()
            log("request: {}".format(line))
            if line == "QUIT":
                send_line(conn, "OK BYE")
                break
            reply = handle_request(line, client_ip)
            log("reply:   {}".format(reply))
            send_line(conn, reply)
    except socket.timeout:
        log("idle timeout")
    except (ConnectionResetError, BrokenPipeError):
        log("client dropped the connection")
    finally:
        reader.close()
        conn.close()
        log("disconnected")


def main():
    parser = argparse.ArgumentParser(description="Network resource reservation server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (use 0.0.0.0 inside Mininet)")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen()
    log("listening on {}:{}".format(args.host, args.port))

    try:
        while True:
            conn, addr = server.accept()

            # TODO 1 (Day 2): serve each client in its own thread.
            #   Right now the server calls handle_client directly, so it serves
            #   ONE client at a time: everyone else waits until that client
            #   finishes. Replace the line below with:
            #     - a threading.Thread whose target is handle_client and whose
            #       args are (conn, addr)
            #     - daemon=True, so these threads don't stop Ctrl+C from exiting
            #     - then start() it
            #   WHY: "handle concurrent requests" is in the marks. With threads,
            #   a slow client can't block anyone else.
            #   TEST: run tests/many_clients.py before and after this change and
            #   compare the total time it reports.
            worker = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            worker.start()
    except KeyboardInterrupt:
        log("server stopped")
    finally:
        server.close()


if __name__ == "__main__":
    main()
