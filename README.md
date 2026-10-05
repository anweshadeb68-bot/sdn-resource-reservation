# Network Resource Reservation System

Computer Networks mini project (UE25CS243A): users book bandwidth over a TCP
service, and an SDN controller enforces approved bookings on Mininet switches.

## Layout

| Folder | Contents | Owner |
| --- | --- | --- |
| `common/` | Shared constants (`config.py`) | Both |
| `client/` | Client program | Member A |
| `server/` | Reservation server and booking logic | Member A |
| `topology/` | Mininet topology | Member B |
| `controller/` | Ryu controller | Member B |
| `tests/` | Echo test, fake controller, test scripts | Both |
| `experiments/` | Performance tests and graphs | Both |

Python 3.8 or 3.9. Server and client use only the standard library.

## Day 1 check

```bash
python3 tests/echo_server.py                         # terminal 1
python3 tests/echo_client.py "RESERVE h1 h4 5 60"    # terminal 2
```
