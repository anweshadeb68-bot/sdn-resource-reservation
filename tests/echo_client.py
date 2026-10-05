"""Day 1 check: send one message to the echo server and print the reply.

Run (with the echo server running in another terminal):
    python3 tests/echo_client.py "RESERVE h1 h4 5 60"
"""

import socket
import sys

HOST = "127.0.0.1"
PORT = 5000

message = sys.argv[1] if len(sys.argv) > 1 else "hello"

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect((HOST, PORT))                    # "dial" the server
client.sendall((message + "\n").encode("utf-8"))  # text -> bytes, one line
reply = client.recv(1024).decode("utf-8")       # bytes -> text
print("Server replied: {}".format(reply.strip()))
client.close()
