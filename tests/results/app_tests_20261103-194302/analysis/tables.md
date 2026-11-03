# Application tests: app_tests_20261103-194302

Controller: SIMULATED (tests/fake_controller.py). These show application behaviour, not SDN enforcement.

## E5: simultaneous booking requests (2 Mbps each, 8 Mbps bookable)

| Clients | Accepted | Rejected (no room) | Errors | Median ms | 95th pct ms | Worst ms | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | 4 | 6 | 0 | 3.4 | 4.0 | 4.1 | PASS |
| 25 | 4 | 21 | 0 | 5.0 | 6.4 | 6.6 | PASS |
| 50 | 4 | 46 | 0 | 8.2 | 10.9 | 12.1 | PASS |

At every load exactly 4 × 2 = 8 Mbps was admitted: no overbooking.

## E6: failure handling

| Case | Condition | Request | Reply | ms | Result |
| --- | --- | --- | --- | --- | --- |
| invalid | bad input | `RESERVE h1 h4 5` | `ERR BAD_REQUEST wrong_field_count` | 0.42 | PASS |
| invalid | bad input | `RESERVE h9 h4 5 60` | `ERR BAD_REQUEST unknown_host` | 0.33 | PASS |
| invalid | bad input | `RESERVE h1 h1 5 60` | `ERR BAD_REQUEST same_host` | 0.26 | PASS |
| invalid | bad input | `RESERVE h1 h4 five 60` | `ERR BAD_REQUEST not_a_number` | 0.26 | PASS |
| invalid | bad input | `RESERVE h1 h4 2.5 60` | `ERR BAD_REQUEST not_a_number` | 0.24 | PASS |
| invalid | bad input | `RESERVE h1 h4 9 60` | `ERR BAD_REQUEST mbps_out_of_range` | 0.23 | PASS |
| invalid | bad input | `RESERVE h1 h4 0 60` | `ERR BAD_REQUEST mbps_out_of_range` | 0.25 | PASS |
| invalid | bad input | `RESERVE h1 h4 5 5` | `ERR BAD_REQUEST seconds_out_of_range` | 0.23 | PASS |
| invalid | bad input | `RESERVE h1 h4 5 601` | `ERR BAD_REQUEST seconds_out_of_range` | 0.24 | PASS |
| invalid | bad input | `RELEASE abc` | `ERR BAD_REQUEST usage: RELEASE <id>` | 0.21 | PASS |
| invalid | bad input | `RELEASE 42` | `ERR NOT_FOUND 42` | 0.23 | PASS |
| invalid | bad input | `reserve h1 h4 5 60` | `ERR BAD_REQUEST unknown_command` | 0.26 | PASS |
| invalid | bad input | `FLY TO MARS` | `ERR BAD_REQUEST unknown_command` | 0.22 | PASS |
| disconnect | after 3 broken clients | `STATUS` | `OK STATUS capacity=10 reserved=0 free=8 used=0 active=0` | 4.94 | PASS |
| idle | silent client, timeout 2 s | `(nothing sent)` | `closed by server` | 2001.52 | PASS |
| idle | other client meanwhile | `STATUS` | `OK STATUS capacity=10 reserved=0 free=8 used=0 active=0` | 0.73 | PASS |
| controller_refuses | refuses | `RESERVE h1 h4 5 60` | `ERR REJECTED controller_error` | 0.93 | PASS |
| controller_refuses | refuses | `STATUS` | `OK STATUS capacity=10 reserved=0 free=8 used=0 active=0` | 0.00 | PASS |
| controller_too_slow | too_slow | `RESERVE h1 h4 5 60` | `ERR REJECTED controller_error` | 3003.15 | PASS |
| controller_too_slow | too_slow | `STATUS` | `OK STATUS capacity=10 reserved=0 free=8 used=0 active=0` | 0.00 | PASS |
| controller_down | down | `RESERVE h1 h4 5 60` | `ERR REJECTED controller_error` | 0.55 | PASS |
| controller_down | down | `STATUS` | `OK STATUS capacity=10 reserved=0 free=8 used=unknown active=0` | 0.00 | PASS |

_Generated 2026-11-03T20:03:37 by experiments/analyze.py from the saved files in this folder._
