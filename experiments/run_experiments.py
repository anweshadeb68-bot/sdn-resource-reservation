#!/usr/bin/env python3
"""D2 experiment runner (Ubuntu VM, Mininet). Runs E1-E4 and saves the raw data.

Start the controller FIRST, fresh, in its own terminal (bookings are forgotten if
switches reconnect, so restart it before each session):

    sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py

Then, from the project folder, in a second terminal:

    sudo python3 experiments/run_experiments.py                    # all experiments, 3 runs
    sudo python3 experiments/run_experiments.py --exp E1 --runs 5  # just one experiment
    sudo python3 experiments/run_experiments.py --exp E1,E2 --flood-mbps 12 --duration 30

This script builds the same network as topology/topo.py (it imports it), starts
the reservation server on h4, runs each trial, and writes everything into
experiments/results/<date-time>/ :

    conditions.json        every setting used, so a run can be repeated exactly
    summary.csv            one row per trial and flow: the numbers to graph
    raw/                   untouched iperf output (-y C: one row per second + a total)
    stats_timeline.jsonl   /stats/links once a second, labelled with the trial it belongs to
    flows/                 switch rules and queues captured at key moments (E3)
    e4_timing.csv          response time of every STATUS request (E4)
    server.log             reservation server log
    controller_events.csv  copy of the controller's event log, if found

What each experiment does (baseline = no booking, proposed = with booking):
  E1  h1 TCP to h4 while h3 floods UDP. Baseline vs h1 booked at 5 Mbps.
  E2  h1 and h2 TCP at the same time while h3 floods. Baseline vs h1 5 + h2 3 booked.
  E3  One timeline with h1 booked: normal, then h3 floods, then the flood stops.
      Shows the congestion limit switching on and off by itself.
  E4  STATUS response time from h2: idle, flood without bookings, flood with a booking.

Notes for the write-up:
  * The order of baseline and booked trials alternates between runs, so a slow
    drift in the VM does not always favour the same mode.
  * In "booked" trials the controller's congestion policy may also switch on
    (it needs a booking plus a sustained flood). summary.csv records, for every
    trial, how many seconds the policy was active, so this is never hidden.
  * The reservation service is TCP only. UDP iperf is only the competing load.
"""

import argparse
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
from common import config  # noqa: E402

H4_IP = config.HOSTS["h4"]
TCP_PORT = 5001            # iperf TCP server on h4
UDP_PORT = 5002            # iperf UDP server on h4 (the flood's destination)
CONTROLLER_LOCAL = "http://127.0.0.1:8080"   # the controller runs on this VM

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def say(text):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), text), flush=True)


def controller_get(path, timeout=2):
    try:
        with _opener.open(CONTROLLER_LOCAL + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except Exception as error:  # noqa: BLE001  any failure = "not available"
        return None, {"error": str(error)}


def load_topology():
    """Import topology/topo.py so the network is exactly the agreed one."""
    path = os.path.join(ROOT, "topology", "topo.py")
    spec = importlib.util.spec_from_file_location("topo", path)
    topo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(topo)
    return topo


def iperf_total_mbps(path):
    """Total throughput from an iperf2 -y C file: the row covering the whole test."""
    best, best_span = None, -1.0
    try:
        with open(path) as handle:
            for line in handle:
                parts = line.strip().split(",")
                if len(parts) < 9 or "-" not in parts[6]:
                    continue
                try:
                    start, end = (float(x) for x in parts[6].split("-"))
                    bps = float(parts[8])
                except ValueError:
                    continue
                if start == 0.0 and end - start > best_span:
                    best, best_span = bps / 1e6, end - start
    except OSError:
        return None
    return None if best is None else round(best, 3)


def background(host, command):
    """Start a command in a Mininet host and return its process id."""
    out = host.cmd(command + " & echo $!")
    try:
        return int(out.strip().split()[-1])
    except (ValueError, IndexError):
        return None


def stop(host, pid):
    if pid:
        host.cmd("kill %d 2>/dev/null" % pid)


def capture(name, folder, commands):
    """Save switch rules / queues (run on the VM itself, not in a host)."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + ".txt"), "w") as handle:
        for command in commands:
            handle.write("$ %s\n" % " ".join(command))
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                handle.write(result.stdout + result.stderr + "\n")
            except (OSError, subprocess.TimeoutExpired) as error:
                handle.write("failed: %s\n\n" % error)


FLOW_COMMANDS = [
    ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"],
    ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s2"],
    ["ovs-appctl", "qos/show", "s1-eth4"],
]


# --------------------------------------------------------------------------
# Background sampler: /stats/links once a second, labelled with the trial
# --------------------------------------------------------------------------

class StatsSampler(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.label = "idle"
        self.running = True
        self.lock = threading.Lock()
        self.active_seconds = {}     # label -> seconds with the congestion policy on

    def run(self):
        with open(self.path, "a") as handle:
            while self.running:
                status, data = controller_get("/stats/links", timeout=1.5)
                with self.lock:
                    label = self.label
                record = {"time": round(time.time(), 3), "label": label, "http": status}
                if status == 200:
                    link = next((l for l in data.get("links", []) if l.get("link") == "s1-s2"), {})
                    policy = link.get("d2_congestion") or {}
                    record.update({
                        "used_mbps": link.get("used_mbps"),
                        "used_mbps_s1_to_s2": link.get("used_mbps_s1_to_s2"),
                        "reserved_mbps": link.get("reserved_mbps"),
                        "policy_active": policy.get("active"),
                        "limited_host": policy.get("limited_host"),
                        "ingress_mbps": policy.get("ingress_mbps"),
                    })
                    if policy.get("active"):
                        with self.lock:
                            self.active_seconds[label] = self.active_seconds.get(label, 0) + 1
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                time.sleep(1)

    def set_label(self, label):
        with self.lock:
            self.label = label

    def policy_seconds(self, label):
        with self.lock:
            return self.active_seconds.get(label, 0)


# --------------------------------------------------------------------------
# The experiment session
# --------------------------------------------------------------------------

class Session:
    def __init__(self, net, args, out):
        self.net, self.args, self.out = net, args, out
        self.h = {name: net.get(name) for name in ("h1", "h2", "h3", "h4")}
        self.raw = os.path.join(out, "raw")
        os.makedirs(self.raw, exist_ok=True)
        self.summary_path = os.path.join(out, "summary.csv")
        with open(self.summary_path, "w") as handle:
            handle.write("experiment,mode,run,flow,booked_mbps,flood_mbps,duration_s,"
                         "measured_mbps,policy_active_s,raw_file\n")
        self.sampler = StatsSampler(os.path.join(out, "stats_timeline.jsonl"))
        self.server_pid = None
        self.tcp_pid = None

    # ---- set-up ----------------------------------------------------------

    def start_services(self):
        h4 = self.h["h4"]
        server_log = os.path.join(self.out, "server.log")
        self.server_pid = background(
            h4, "cd %s && python3 -u server/server.py --host 0.0.0.0 > %s 2>&1" % (ROOT, server_log))
        time.sleep(2)
        log = open(server_log).read() if os.path.exists(server_log) else ""
        if "controller OK" not in log:
            say("WARNING: server log does not say 'controller OK'. Bookings may fail:\n" + log)
        else:
            say("reservation server running on h4, controller OK")
        self.tcp_pid = background(h4, "iperf -s -p %d > /dev/null 2>&1" % TCP_PORT)
        time.sleep(1)
        self.sampler.start()

    def stop_services(self):
        self.sampler.running = False
        stop(self.h["h4"], self.tcp_pid)
        stop(self.h["h4"], self.server_pid)

    # ---- bookings through OUR TCP client (the real application path) -----

    def reserve(self, host, mbps, seconds):
        reply = self.h[host].cmd("python3 %s/client/client.py --server %s RESERVE %s h4 %d %d"
                                 % (ROOT, H4_IP, host, mbps, seconds))
        first = reply.strip().splitlines()[0] if reply.strip() else ""
        say("  %s RESERVE %d Mbps -> %s" % (host, mbps, first))
        parts = first.split()
        if len(parts) >= 3 and parts[:2] == ["OK", "RESERVED"]:
            return int(parts[2])
        raise RuntimeError("booking for %s failed: %s" % (host, first))

    def release(self, host, booking_id):
        self.h[host].cmd("python3 %s/client/client.py --server %s RELEASE %d"
                         % (ROOT, H4_IP, booking_id))

    # ---- traffic ---------------------------------------------------------

    def start_flood(self, name, seconds):
        h3, h4 = self.h["h3"], self.h["h4"]
        udp_server = background(h4, "iperf -s -u -p %d -i 1 -y C > %s 2>&1"
                                % (UDP_PORT, os.path.join(self.raw, name + "_h4_udp_server.csv")))
        time.sleep(0.5)
        flood = background(h3, "iperf -c %s -u -p %d -b %sM -t %d -i 1 -y C > %s 2>&1"
                           % (H4_IP, UDP_PORT, self.args.flood_mbps, seconds,
                              os.path.join(self.raw, name + "_h3_udp.csv")))
        return udp_server, flood

    def tcp_command(self, name, flow, seconds):
        path = os.path.join(self.raw, "%s_%s_tcp.csv" % (name, flow))
        return path, "iperf -c %s -p %d -t %d -i 1 -y C > %s 2>&1" % (H4_IP, TCP_PORT, seconds, path)

    def record(self, exp, mode, run, flow, booked, raw_path, label, duration=None):
        mbps = iperf_total_mbps(raw_path)
        duration = self.args.duration if duration is None else duration
        with open(self.summary_path, "a") as handle:
            handle.write("%s,%s,%d,%s,%s,%s,%d,%s,%d,%s\n" % (
                exp, mode, run, flow, booked, self.args.flood_mbps, duration,
                "" if mbps is None else mbps, self.sampler.policy_seconds(label),
                os.path.relpath(raw_path, self.out)))
        say("  %s %s run %d %s: %s Mbps (policy on %d s)" % (
            exp, mode, run, flow, mbps, self.sampler.policy_seconds(label)))

    def cool_down(self):
        self.sampler.set_label("cooldown")
        time.sleep(self.args.cooldown)

    def modes_for(self, run):
        """Alternate the order so neither mode always goes first."""
        return ["baseline", "booked"] if run % 2 == 1 else ["booked", "baseline"]

    # ---- E1 --------------------------------------------------------------

    def e1(self):
        d = self.args.duration
        for run in range(1, self.args.runs + 1):
            for mode in self.modes_for(run):
                name = "E1_%s_run%d" % (mode, run)
                say("E1 %s, run %d" % (mode, run))
                booking = self.reserve("h1", 5, d + 40) if mode == "booked" else None
                self.sampler.set_label(name)
                udp_server, flood = self.start_flood(name, d + 6)
                time.sleep(3)                                   # flood settles first
                path, command = self.tcp_command(name, "h1", d)
                self.h["h1"].cmd(command)                       # blocks for d seconds
                time.sleep(4)
                stop(self.h["h3"], flood)
                stop(self.h["h4"], udp_server)
                self.record("E1", mode, run, "h1", 5 if booking else 0, path, name)
                if booking:
                    self.release("h1", booking)
                self.cool_down()

    # ---- E2 --------------------------------------------------------------

    def e2(self):
        d = self.args.duration
        for run in range(1, self.args.runs + 1):
            for mode in self.modes_for(run):
                name = "E2_%s_run%d" % (mode, run)
                say("E2 %s, run %d" % (mode, run))
                bookings = []
                if mode == "booked":
                    bookings = [("h1", self.reserve("h1", 5, d + 40)),
                                ("h2", self.reserve("h2", 3, d + 40))]
                self.sampler.set_label(name)
                udp_server, flood = self.start_flood(name, d + 6)
                time.sleep(3)
                path2, command2 = self.tcp_command(name, "h2", d)
                pid2 = background(self.h["h2"], command2)
                path1, command1 = self.tcp_command(name, "h1", d)
                self.h["h1"].cmd(command1)                      # both run side by side
                time.sleep(4)
                stop(self.h["h2"], pid2)
                stop(self.h["h3"], flood)
                stop(self.h["h4"], udp_server)
                self.record("E2", mode, run, "h1", 5 if bookings else 0, path1, name)
                self.record("E2", mode, run, "h2", 3 if bookings else 0, path2, name)
                for host, booking_id in bookings:
                    self.release(host, booking_id)
                self.cool_down()

    # ---- E3 --------------------------------------------------------------

    def e3(self):
        normal, flood_s, recovery = self.args.e3_phases
        total = normal + flood_s + recovery
        flows = os.path.join(self.out, "flows")
        for run in range(1, self.args.runs + 1):
            name = "E3_run%d" % run
            say("E3 run %d: %d s normal, %d s flood, %d s recovery" % (run, normal, flood_s, recovery))
            booking = self.reserve("h1", 5, total + 40)
            path, command = self.tcp_command(name, "h1", total)
            tcp = background(self.h["h1"], command)
            self.sampler.set_label(name + "_normal")
            time.sleep(normal - 5)
            capture(name + "_1_normal", flows, FLOW_COMMANDS)
            time.sleep(5)
            self.sampler.set_label(name + "_flood")
            udp_server, flood = self.start_flood(name, flood_s)
            time.sleep(flood_s - 5)                             # policy should be on by now
            capture(name + "_2_during_flood", flows, FLOW_COMMANDS)
            time.sleep(5)
            stop(self.h["h3"], flood)
            stop(self.h["h4"], udp_server)
            self.sampler.set_label(name + "_recovery")
            time.sleep(recovery - 3)
            capture(name + "_3_after_recovery", flows, FLOW_COMMANDS)
            time.sleep(5)
            stop(self.h["h1"], tcp)
            self.record("E3", "booked", run, "h1", 5, path, name + "_flood", duration=total)
            self.release("h1", booking)
            self.cool_down()

    # ---- E4 --------------------------------------------------------------

    def e4(self):
        timing_out = os.path.join(self.out, "e4_timing.csv")
        count = self.args.e4_requests
        for run in range(1, self.args.runs + 1):
            for condition in ("idle", "flood", "flood_booked"):
                label = "E4_%s_run%d" % (condition, run)
                say("E4 %s, run %d" % (condition, run))
                booking, udp_server, flood = None, None, None
                if condition == "flood_booked":
                    booking = self.reserve("h1", 5, count + 120)
                self.sampler.set_label(label)
                if condition != "idle":
                    udp_server, flood = self.start_flood(label, int(count * 0.5) + 60)
                    time.sleep(12)          # give the congestion policy time to react
                reply = self.h["h2"].cmd(
                    "python3 %s/tests/timing_client.py --server %s --count %d --interval 0.2 "
                    "--label %s --out %s" % (ROOT, H4_IP, count, label, timing_out))
                say("  " + reply.strip())
                stop(self.h["h3"], flood)
                stop(self.h["h4"], udp_server)
                if booking:
                    self.release("h1", booking)
                self.cool_down()


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Run the D2 experiments in Mininet")
    parser.add_argument("--exp", default="E1,E2,E3,E4", help="comma-separated list, e.g. E1,E3")
    parser.add_argument("--runs", type=int, default=3, help="repeats per condition")
    parser.add_argument("--duration", type=int, default=30, help="TCP test length in seconds (E1, E2)")
    parser.add_argument("--flood-mbps", default="12", help="h3 UDP flood rate in Mbps")
    parser.add_argument("--cooldown", type=int, default=12, help="quiet seconds between trials")
    parser.add_argument("--e3-phases", type=int, nargs=3, default=[20, 40, 30],
                        metavar=("NORMAL", "FLOOD", "RECOVERY"), help="E3 phase lengths in seconds")
    parser.add_argument("--e4-requests", type=int, default=50, help="STATUS requests per E4 condition")
    parser.add_argument("--note", default="", help="free text saved in conditions.json")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 experiments/run_experiments.py")
    experiments = [e.strip().upper() for e in args.exp.split(",") if e.strip()]
    for e in experiments:
        if e not in ("E1", "E2", "E3", "E4"):
            sys.exit("Unknown experiment %s (use E1, E2, E3, E4)" % e)

    status, _ = controller_get("/health")
    if status != 200:
        sys.exit("The controller is not answering on %s. Start it first:\n"
                 "  sudo ~/ryu-env/bin/ryu-manager controller/reservation_ctrl.py" % CONTROLLER_LOCAL)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(ROOT, "experiments", "results", stamp)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "conditions.json"), "w") as handle:
        json.dump({"started": stamp, "experiments": experiments, "runs": args.runs,
                   "duration_s": args.duration, "flood_mbps": args.flood_mbps,
                   "flood_type": "iperf2 UDP from h3 to h4", "tcp_tool": "iperf2 TCP, -i 1 -y C",
                   "cooldown_s": args.cooldown, "e3_phases_s": args.e3_phases,
                   "e4_requests": args.e4_requests, "bottleneck_mbps": config.LINKS["s1-s2"],
                   "reservable_share": config.RESERVABLE_SHARE, "note": args.note},
                  handle, indent=2)
    say("results folder: " + out)

    topo = load_topology()
    from mininet.log import setLogLevel
    setLogLevel("warning")
    net = topo.build_network(SimpleNamespace(mininet_limit=False, delay_ms=0, delay_link="s1-s2"))
    net.start()
    session = None
    try:
        topo.check_ports(net)
        if not topo.add_management_link(net):
            raise RuntimeError("management cable from h4 to the VM is not working")
        time.sleep(4)                       # switches connect, queues are set up
        loss = net.pingAll()
        if loss != 0:
            say("WARNING: pingall lost %s%% of packets" % loss)
        session = Session(net, args, out)
        session.start_services()
        for e in experiments:
            getattr(session, e.lower())()
        say("all done")
    finally:
        if session:
            session.stop_services()
        for name in ("controller_events.csv", "link_usage.csv"):
            source = os.path.join(ROOT, "logs", name)
            if os.path.exists(source):
                shutil.copy(source, os.path.join(out, name))
        net.stop()
        topo.run(["ip", "link", "del", topo.MGMT_MAIN_INTERFACE], check=False)
        # Give the files back to the user who ran sudo, so they can be edited and committed.
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid:
            for folder, _, files in os.walk(out):
                os.chown(folder, int(uid), int(gid))
                for f in files:
                    os.chown(os.path.join(folder, f), int(uid), int(gid))
        say("saved in " + out)


if __name__ == "__main__":
    main()
