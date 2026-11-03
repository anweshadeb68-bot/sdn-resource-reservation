# Network Resource Reservation System

Computer Networks mini project (UE25CS243A, PES University). Users book bandwidth
over a **TCP reservation service**; an **SDN controller** (Ryu, OpenFlow 1.3)
validates each approved booking and enforces it on Open vSwitch queues in a
Mininet network. D2 adds request logging, a congestion-triggered SDN policy,
automated tests and repeatable performance experiments.

**Storyline:** a lunar base shares one slow 10 Mbps radio link with Mission Control
on Earth. The astronaut's EVA camera (h1), the science lab (h2) and base systems
(h3) compete for it; Mission Control (h4) runs the reservation server.

## How it fits together

```
 h1 EVA camera ─┐                                   ┌─ h4 Mission Control
 h2 Science lab ─┼── s1 ════ 10 Mbps link ════ s2 ──┘   reservation server, TCP :5000
 h3 Base systems ┘   (queues 0-8 + D2 queue 9 at both ends of the slow link)
        │                                                  │ management cable
        └── booking requests: TCP to 10.0.0.4:5000 ────────┤ h4 192.168.100.4
                                                           ▼
                     Main machine (Ubuntu VM) 192.168.100.1
                     ├── Ryu controller: OpenFlow :6653, web API :8080
                     └── Dashboard: web page :8000
```

1. A client sends `RESERVE h1 h4 5 120` over TCP to the server on h4.
2. The server validates it, then, under one lock, checks that every link on the
   path stays within 80% of its capacity (8 of the 10 Mbps) and that fewer than 8
   bookings are active.
3. It asks the controller (`POST /reservations`, 3 s timeout) to install the
   booking: a priority-100 flow rule into a queue with a guaranteed minimum rate.
   Only if the controller confirms is the booking saved and `OK RESERVED` sent.
4. `RELEASE`, or the server's expiry thread when time runs out, sends
   `DELETE /reservations/<id>`. The switch rule's hard timeout is the backup.
5. **D2 policy (controller):** when a forward booking exists and the link stays
   congested while an unbooked host offers a high UDP load, the controller puts
   that host's UDP traffic to h4 into queue 9 (capped at 2 Mbps, priority 20).
   It removes the rule after the load has dropped for several readings.

The 2 Mbps above the bookable 8 Mbps is **never offered for booking**. It is
headroom, not a lane reserved for any traffic.

## Repository layout

| Folder | Contents | Owner |
| --- | --- | --- |
| `common/` | Shared constants (`config.py`): addresses, ports, link speeds, limits | Both |
| `client/` | `client.py`: one-shot or interactive TCP client | Member A |
| `server/` | `server.py` (threads, protocol, expiry), `bookings.py` (capacity check, lock, ownership), `controller_client.py` (controller API), `request_log.py` (D2 log and STATS) | Member A |
| `topology/` | `topo.py`: Mininet topology and management cable | Member B |
| `controller/` | `reservation_ctrl.py`: Ryu controller, queues, D2 congestion policy | Member B |
| `dashboard/` | `dashboard.py` + `index.html`: Mission Control web GUI | Anirudh |
| `tests/` | Fake controller, application tests, race and load tests, timing client | Member A |
| `experiments/` | `run_experiments.py` (Mininet E1-E4), `analyze.py` (tables, graphs), results | All |

Python 3.8 or 3.9. The server, client, tests and dashboard use only the standard
library; `experiments/analyze.py` also uses matplotlib for graphs.

## Quick start on a laptop (no Mininet)

Uses `tests/fake_controller.py`, which answers like the real controller but
installs nothing, so this checks the **application only**.

```bash
python3.9 tests/fake_controller.py --port 8080                          # terminal 1
python3.9 server/server.py --controller http://127.0.0.1:8080           # terminal 2
python3.9 client/client.py RESERVE h1 h4 5 60                           # terminal 3
python3.9 client/client.py STATUS
python3.9 dashboard/dashboard.py --controller http://127.0.0.1:8080     # then open http://127.0.0.1:8000
```

On a Mac, if port 5000 is taken by AirPlay Receiver, turn it off in System
Settings > General > AirDrop & Handoff, or use `--port` on both server and client.

## Full system on the Ubuntu VM

Start in this order (each in its own terminal, from the project folder):

```bash
sudo mn -c                                                   # clear old Mininet state
sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py   # 1. controller
sudo python3 topology/topo.py                                # 2. network, opens the mininet> prompt
```

At the `mininet>` prompt:

```
h4 python3 -u server/server.py --host 0.0.0.0 > /tmp/server.log 2>&1 &
h4 cat /tmp/server.log                                       # should say "controller OK"
h1 python3 client/client.py --server 10.0.0.4 RESERVE h1 h4 5 120
sh ovs-ofctl -O OpenFlow13 dump-flows s1                     # a priority=100 rule appears
h1 python3 client/client.py --server 10.0.0.4 STATS
```

Dashboard, in a VM terminal: `python3 dashboard/dashboard.py --server 192.168.100.4`,
then open http://127.0.0.1:8000 in the VM's browser.

If a switch reconnects, the controller forgets its bookings: restart the
controller and Mininet together.

## Reservation protocol (TCP, port 5000)

One request per line, one reply per line, every message ends with `\n`.
Replies start with `OK` or `ERR`.

| Request | Success reply | Error replies |
| --- | --- | --- |
| `RESERVE <src> <dst> <mbps> <seconds>` | `OK RESERVED <id> <mbps> <seconds>` | `ERR BAD_REQUEST <reason>`, `ERR REJECTED insufficient_capacity \| max_reservations \| controller_error` |
| `RELEASE <id>` | `OK RELEASED <id>` | `ERR FORBIDDEN <id>` (not the owner), `ERR NOT_FOUND <id>` |
| `STATUS` | `OK STATUS capacity=10 reserved=5 free=3 used=4.8 active=1` | |
| `LIST` | `OK LIST <n> <id>:<src>><dst>:<mbps>:<seconds_left> ...` | |
| `STATS` (D2) | `OK STATS reserve_requests=14 accepted=9 rejected=3 invalid=2 insufficient_capacity=2 max_reservations=0 controller_error=1 released=4 forbidden=1 not_found=0 queries=120 bad_commands=1 log_errors=0` | |
| `QUIT` | `OK BYE` | |

Limits: 1-8 Mbps, 10-600 s, at most 8 active bookings, 8 Mbps bookable on the
Moon link. The owner of a booking is the client's IP address. A client silent for
60 s is disconnected (`--idle-timeout`). A half-sent line from a client that hangs
up is ignored.

**Request log (D2):** every answered request is appended to `logs/requests.csv`
(time, client IP, command, full request, category, reason, reply, server
processing time in ms). Only a successful `RESERVE` counts as **accepted**;
`STATUS`, `LIST` and `STATS` are counted as **queries**. Change the file with
`--request-log`, or switch it off with `--no-request-log`.

## Controller web API (port 8080)

| Call | Meaning |
| --- | --- |
| `GET /health` | 200 when the controller is up |
| `POST /reservations` `{"id","src","dst","mbps","duration_s"}` | Install a booking (IP addresses). 200 = installed; 400 bad body; 409 no room or no queue; 503 switches not ready |
| `DELETE /reservations/<id>` | Remove a booking (404 = already gone) |
| `GET /reservations` | Bookings the controller holds |
| `GET /stats/links` | Measured link use, reserved total, and the D2 `d2_congestion` block (active, limited host, queue, ingress per host) |

`tests/send_booking.sh` calls these by hand (`health`, `post`, `stats`, `d2`,
`flows`, `queues`, `policies`, `events`).

## Tests

| What | Command | Where it runs |
| --- | --- | --- |
| Application tests (E5 load 10/25/50 clients, E6 invalid input, disconnects, idle client, controller refusing / too slow / down) | `python3.9 tests/app_tests.py` | Laptop, simulated controller |
| Race test: 8 clients × 2 Mbps at once, exactly 4 accepted | `python3.9 tests/race_test.py` (server running) | Laptop |
| Threads vs one-at-a-time timing | `python3.9 tests/many_clients.py` | Laptop |
| Response time of STATUS (E4 tool) | `python3 tests/timing_client.py --server 10.0.0.4 --label idle` | Mininet host |

`tests/app_tests.py` starts its own server and fake controller on free ports for
every case, writes `tests/results/app_tests_<date-time>/` (`results.csv`,
`summary.txt`, the server's request logs) and exits non-zero if any case fails.

## Experiments (Ubuntu VM)

See `experiments/HOW_TO_RUN.md`. In short, with the controller running:

```bash
sudo python3 experiments/run_experiments.py              # E1-E4, 3 runs each
python3 experiments/analyze.py experiments/results/<date-time>
python3 experiments/analyze.py --app-tests tests/results/app_tests_<date-time>
```

| Experiment | Baseline | Proposed | Measured |
| --- | --- | --- | --- |
| E1 Protected throughput | h1 TCP, no booking, h3 UDP flood | h1 booked 5 Mbps | h1 Mbps over the test, per run |
| E2 Concurrent guarantees | h1 + h2 TCP, no bookings, h3 flood | h1 5 + h2 3 booked | each flow's Mbps |
| E3 Automatic policy | (timeline) | h1 booked; normal, flood, recovery | link use, h3 offered load, limit on/off, rules before/during/after |
| E4 TCP responsiveness | idle; flood without bookings | flood with a booking (policy can act) | STATUS response time (median, 95th percentile) |
| E5 Client load | | 10, 25, 50 simultaneous clients | accepted/rejected, response time (simulated controller) |
| E6 Failure handling | | bad input, disconnects, idle, controller down/slow/refusing | correct reply, nothing overbooked |

Baseline and booked trials alternate order between runs. `summary.csv` records
how many seconds the congestion policy was active during every trial, so its
effect on booked results is visible. `analyze.py` writes `analysis/tables.md`
and the graphs from the saved raw files only.

## Results

- **D1:** with h3 flooding, h1 got **0.73 Mbps** without a booking and
  **9.45 Mbps** with a 5 Mbps booking. A booking guarantees a minimum, not a cap:
  h1 can use spare capacity above 5 Mbps.
- **E5/E6 (simulated controller):** at 10, 25 and 50 simultaneous clients asking
  for 2 Mbps each, exactly 4 were accepted (8 Mbps) and the rest refused with
  `insufficient_capacity`; no errors, no overbooking. All failure cases passed,
  including a controller that answers after 5 s (refused with `controller_error`
  after the 3 s timeout, nothing booked). Tables: `tests/results/.../analysis/tables.md`.
- **E1-E4 (Mininet):** _to be added from `experiments/results/<final run>/analysis/tables.md`._

## Known limitations

- Ownership is by client IP address; anyone able to use that address could
  cancel the booking. Real deployments would authenticate clients.
- Bookings live in the server's memory; a server restart forgets them (the switch
  rules still expire through their hard timeout).
- The controller call happens inside the booking lock, so a slow controller
  delays other bookings by up to 3 s. A controller that installs a rule after the
  server has timed out leaves an unbooked rule until its hard timeout.
- The D2 policy identifies the flood by host-port load and UDP to h4; it is scoped
  to the controlled iperf experiment, not general traffic classification.
- One fixed path (two switches): there is no rerouting.
- Application tests use a simulated controller; only the Mininet experiments show
  real enforcement.

## Team

| Member | Responsibility |
| --- | --- |
| Member A (Anwesha) | TCP client and server, booking logic, request log and STATS, application tests, analysis, README |
| Member B | Mininet topology, Ryu controller, queues, D2 congestion policy |
| Anirudh | Mission Control dashboard, Ubuntu experiment runs and evidence |
