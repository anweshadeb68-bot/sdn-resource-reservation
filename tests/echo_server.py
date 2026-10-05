"""Day 1 check: a server that sends back whatever a client sends.

Run:  python3 tests/echo_server.py
Stop: Ctrl+C
"""

import socket

HOST = "127.0.0.1"  # this computer only; the real server will listen on h4
PORT = 5000         # same port as the real reservation server (config.SERVER_PORT)

# AF_INET = use IPv4 addresses; SOCK_STREAM = use TCP (reliable, in order).
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Lets you restart the server immediately. Without it, the OS keeps the port
# "reserved" for about a minute after the server stops.
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

server.bind((HOST, PORT))  # claim the address and port
server.listen()            # start waiting for clients
print("Echo server listening on {}:{}".format(HOST, PORT))

try:
    while True:
        conn, addr = server.accept()  # blocks until a client connects
        print("Client connected from {}:{}".format(addr[0], addr[1]))
        with conn:
            while True:
                data = conn.recv(1024)  # read up to 1024 bytes
                if not data:            # empty means the client hung up
                    break
                print("Received: {!r}".format(data))
                conn.sendall(data)      # send the same bytes back
        print("Client disconnected")
except KeyboardInterrupt:
    print("\nServer stopped")
finally:
    server.close()
