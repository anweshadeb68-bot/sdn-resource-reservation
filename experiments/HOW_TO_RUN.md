# Running the D2 experiments (Ubuntu VM)

All four experiments run from one script. It builds the agreed Mininet network,
starts the reservation server on h4, runs every trial and saves the raw data.

## Before each session

1. Get the latest code: `git pull`
2. Clear old Mininet state: `sudo mn -c`
3. Start the controller **fresh** in terminal 1 (it forgets bookings if switches reconnect):

   ```bash
   sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py
   ```

## Run

In terminal 2, from the project folder:

```bash
sudo python3 experiments/run_experiments.py                       # E1-E4, 3 runs each (about 25 min)
sudo python3 experiments/run_experiments.py --exp E1 --runs 5     # one experiment
sudo python3 experiments/run_experiments.py --exp E3 --runs 1     # quick look at the policy
```

Useful options: `--duration 30` (TCP test length), `--flood-mbps 12` (h3's UDP flood),
`--cooldown 12` (quiet time between trials), `--note "laptop on charger"` (saved with the results).

Do not touch Mininet or start other traffic while it runs.

## What you get

A new folder `experiments/results/<date-time>/` containing:

| File | What it is | Who uses it |
| --- | --- | --- |
| `summary.csv` | One row per trial and flow: mode, run, measured Mbps, seconds the congestion policy was on | Member A, for tables and graphs |
| `raw/` | Untouched iperf output, one row per second plus a total | Member A, for per-second graphs |
| `stats_timeline.jsonl` | Controller `/stats/links` once a second, labelled with the trial | Member A (E3 timeline), Member B |
| `flows/` | Switch rules and queues before, during and after the flood (E3) | Member B, D2 evidence |
| `e4_timing.csv` | Response time of every STATUS request | Member A (E4) |
| `server.log`, `controller_events.csv` | Server and controller logs for the session | Everyone |
| `conditions.json` | Every setting used | README / report |

## Also capture for the report

- A screen recording of E3 with the dashboard open (`python3 dashboard/dashboard.py --server 192.168.100.4`,
  then http://127.0.0.1:8000): the purple "congestion limit" band and the Network control panel
  should switch on during the flood and off afterwards.
- A screenshot of `flows/E3_run1_2_during_flood.txt` showing the priority-20 rule to queue 9.

Commit the whole results folder and tell Member A which folder is the final one.
