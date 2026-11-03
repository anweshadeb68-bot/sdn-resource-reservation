# Screenshot runbook (D2 evidence)

One live session on the Ubuntu VM captures every LIVE item of Anirudh's checklist
(S01-S17), in an order where each step sets up the next. The SAVED items are
read from the original files afterwards. Anwesha's items run on her laptop.

Rules (from the checklist): real screenshots only, command and result visible
together, legible zoom, unedited PNGs, explicit names, original logs kept
separately. Save PNGs in `docs/screenshots/`.

## Terminals on the VM

| Terminal | Used for |
| --- | --- |
| T1 | Ryu controller |
| T2 | Mininet prompt (`mininet>`) |
| T3 | Normal shell in the project folder: `tests/send_booking.sh`, `cat` of saved files |
| T4 | Dashboard backend |
| Browser | http://127.0.0.1:8000 |

## Part 1: live session (about 20 minutes)

| Step | Where | Do this | Screenshot |
| --- | --- | --- | --- |
| 1 | T3 | `sudo mn -c` | |
| 2 | T1 | `sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py` | |
| 3 | T2 | `sudo python3 topology/topo.py`, then `net` and `pingall` | **S01_topology_pingall.png**: startup lines "Port numbers match… yes", "Management cable… yes", `net`, 0% dropped |
| 4 | T1 + T3 | `bash tests/send_booking.sh health` | **S02_controller_health.png**: Ryu shows both switches and queue set-up; T3 shows HTTP 200 |
| 5 | T2 | `h4 python3 -u server/server.py --host 0.0.0.0 > /tmp/server.log 2>&1 &` then `h4 cat /tmp/server.log` | (part of S03: "controller OK") |
| 6 | T4 | `python3 dashboard/dashboard.py --server 192.168.100.4`, open the browser | **S13_dashboard_overview.png**: link bar, facts, chart, Network control "Controller up", no lanes yet |
| 7 | Browser | Book **EVA camera, 5 Mbps, 10 minutes** | **S14_dashboard_booking.png**: "Booked…" message, orange lane, Active lanes shows it |
| 8 | T2 | `h2 python3 client/client.py --server 10.0.0.4 RESERVE h2 h4 3 600` | **S03_tcp_reservations.png**: the `OK RESERVED` reply (and step 7's booking in the dashboard log, or book h1 from the CLI as well) |
| 9 | T3 | `bash tests/send_booking.sh list` then `bash tests/send_booking.sh flows` | Live versions of S04 / S05 (optional: the SAVED files are the official ones) |
| 10 | T3 | `bash tests/send_booking.sh queues` | **S06_queues.png**: 10 Mbps parent, queue 1 min 5 Mbps, queue 2 min 3 Mbps, queue 9 max 2 Mbps. Bookings must still be active |
| 11 | Browser | Book **Base systems, 2 Mbps** | **S15_capacity_rejection_gui.png**: "Not enough room…" (8 of 8 Mbps already booked) |
| 12 | T2 | `h3 python3 client/client.py --server 10.0.0.4 RESERVE h3 h4 2 60` | **S15_capacity_rejection_tcp.png**: the actual `ERR REJECTED insufficient_capacity` |
| 13 | T3 | `bash experiments/watch_policy.sh` (leave it waiting) | |
| 14 | T2 | `h4 iperf -s -u -p 5002 &` then `h3 iperf -c 10.0.0.4 -u -p 5002 -b 12M -t 60 &` | |
| 15 | T3 | wait until the watcher prints and saves the rule (about 6-10 s) | **S11_priority20_rule_active.png**: `priority=20 … udp … nw_src=10.0.0.3 … set_queue:9`, plus `"active": true` in the stats |
| 16 | Browser | while the flood runs | **S11b_dashboard_limit_active.png**: purple band on the chart, Network control "Limit active", h3's ingress bar full |
| 17 | T3 | after the flood ends (60 s) wait about 10 s, then `bash tests/send_booking.sh policies` and `bash tests/send_booking.sh events` | **S12_policy_removed_live.png**: no priority-20 rule, events show activated then removed |
| 18 | Browser | Cancel the EVA camera lane | **S14b_dashboard_release.png**: lane gone; T3 `bash tests/send_booking.sh flows` shows its priority-100 rule removed |

Optional for the robustness mark: stop Ryu (Ctrl+C in T1), try a booking from the
dashboard ("Network control did not confirm", status light red), restart Ryu, book
again. Note: restarting the controller makes it forget bookings, so do this last.

## Part 2: saved evidence (any time, T3)

| Screenshot | Command |
| --- | --- |
| S04_E2_reservations.png | `cat experiments/d2_handoff/two_reservations/reservations.txt` |
| S05_E2_priority100_rules.png | `cat experiments/d2_handoff/two_reservations/flows.txt` |
| S07_E1_baseline.png | `cat experiments/d2_handoff/h3_baseline.log` (and a fresh h1 baseline run if a log of h1 is needed) |
| S08_E1_booked.png | `cat experiments/d2_handoff/h1_final_test.log` and `h3_final_test.log` |
| S09_E2_concurrent.png | `cat …/e2_h1.log`, `cat …/e2_h2.log`, `tail -n 12 …/e2_h3.log` |
| S10_E2_link_stats.png | `cat experiments/d2_handoff/two_reservations/congestion_status.txt` |
| S12_E2_policy_history.png | `cat …/policies.txt` and `cat …/controller_events_e2.csv` |

## Part 3: Anwesha's laptop (S16, S17)

| Screenshot | Command |
| --- | --- |
| S16_app_tests.png | `python3.9 tests/app_tests.py`: 12 PASS lines, SIMULATED controller stated |
| S16b_race_test.png | server + fake controller running, then `python3.9 tests/race_test.py` |
| S17_results_graphs | `python3.9 experiments/analyze.py --app-tests tests/results/app_tests_<…>`, and the E1/E2 comparison graph |

Commit `docs/screenshots/`, `experiments/d2_handoff/` and the evidence text files
that `watch_policy.sh` saves.
