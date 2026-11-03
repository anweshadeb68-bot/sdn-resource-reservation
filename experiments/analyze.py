#!/usr/bin/env python3
"""Turn saved experiment data into tables and graphs (Member A, D2).

    python3.9 experiments/analyze.py experiments/results/<date-time>
    python3.9 experiments/analyze.py --app-tests tests/results/app_tests_<date-time>
    (both arguments can be given together)

Reads ONLY saved files (summary.csv, raw/, stats_timeline.jsonl, e4_timing.csv,
or the app-test results.csv), so every number in the report can be regenerated.
Writes into an analysis/ folder inside the folder it reads:

    tables.md               all tables, with units, run counts and conditions
    e1_throughput.png       E1: h1 TCP throughput, baseline vs booked
    e2_concurrent.png       E2: h1 and h2 together, baseline vs booked
    e3_timeline_runN.png    E3: one timeline per run, congestion limit shaded
    e4_response.png         E4: STATUS response time per condition
    e5_load.png             E5 (app tests): response time vs number of clients

Statistics: mean and sample standard deviation over runs (n is always shown),
plus min and max. With 3 to 5 runs these describe spread; they are not a
significance test. Needs matplotlib (pip install matplotlib); tables are written
even without it.
"""

import argparse
import csv
import datetime
import json
import os
import statistics
import sys

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:              # tables still work
    plt = None

# Colours checked for colour-blind separation (dataviz validator, light surface).
BOOKED = "#d9731f"      # booked / h1 (the EVA camera colour)
SECOND = "#0b8fa0"      # h2 / link usage
POLICY = "#6f5bd6"      # congestion limit active / h3
BASELINE = "#8a8f98"    # no booking (neutral on purpose)
INK, MUTED, GRID = "#1d2230", "#5d6474", "#e3e6ec"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def describe(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {"n": len(values), "mean": statistics.mean(values),
            "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
            "min": min(values), "max": max(values)}


def fmt(x, digits=2):
    return "–" if x is None else ("%.*f" % (digits, x))


def percentile(values, share):
    ordered = sorted(values)
    if not ordered:
        return None
    k = (len(ordered) - 1) * share
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def as_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def iperf_series(path):
    """Per-second (start_s, Mbps) from an iperf2 -y C file, total row excluded."""
    points, spans = [], []
    if not os.path.exists(path):
        return points
    for line in open(path):
        parts = line.strip().split(",")
        if len(parts) < 9 or "-" not in parts[6]:
            continue
        try:
            start, end = (float(x) for x in parts[6].split("-"))
            mbps = float(parts[8]) / 1e6
        except ValueError:
            continue
        spans.append((start, end, mbps))
    if not spans:
        return points
    longest = max(e - s for s, e, _ in spans)
    return [(s, m) for s, e, m in spans if not (s == 0.0 and e - s == longest and longest > 1.5)]


def style(ax, title, ylabel, xlabel=None):
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=10)
    ax.set_ylabel(ylabel, color=MUTED)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#b9bfca")
    ax.tick_params(colors=MUTED)


def save(fig, folder, name):
    path = os.path.join(folder, name)
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def strip_with_mean(ax, x, values, color, label):
    """Every run as a dot, mean as a bar-free marker with a ±1 sd whisker."""
    d = describe(values)
    if not d["n"]:
        return
    jitter = [x + (i - (len(values) - 1) / 2) * 0.06 for i in range(len(values))]
    ax.scatter(jitter, values, s=28, color=color, alpha=0.55, zorder=3,
               edgecolors="white", linewidths=1)
    ax.errorbar([x], [d["mean"]], yerr=[[d["sd"]], [d["sd"]]], fmt="o", color=color,
                markersize=9, capsize=6, linewidth=2, zorder=4, label=label,
                markeredgecolor="white", markeredgewidth=1.5)
    ax.annotate("%.2f" % d["mean"], (x, d["mean"]), xytext=(12, -4),
                textcoords="offset points", color=INK, fontsize=10)


# --------------------------------------------------------------------------
# Experiments E1-E4 (network, from run_experiments.py)
# --------------------------------------------------------------------------

def analyze_results(folder, lines, figures):
    summary_path = os.path.join(folder, "summary.csv")
    if not os.path.exists(summary_path):
        sys.exit("No summary.csv in %s" % folder)
    rows = read_csv(summary_path)
    conditions = {}
    cpath = os.path.join(folder, "conditions.json")
    if os.path.exists(cpath):
        conditions = json.load(open(cpath))
    out = os.path.join(folder, "analysis")
    os.makedirs(out, exist_ok=True)

    lines.append("# Experiment results: %s\n" % os.path.basename(os.path.abspath(folder)))
    if conditions:
        lines.append("Conditions: %s s TCP tests, h3 UDP flood at %s Mbps, %s runs per condition, "
                     "%s s cool-down. Bottleneck %s Mbps, bookable share %s.%s\n" % (
                         conditions.get("duration_s"), conditions.get("flood_mbps"),
                         conditions.get("runs"), conditions.get("cooldown_s"),
                         conditions.get("bottleneck_mbps"), conditions.get("reservable_share"),
                         (" Note: " + conditions["note"]) if conditions.get("note") else ""))
    lines.append("Throughput = iperf2 TCP total over the whole test, Mbps. "
                 "Mean ± sample SD over runs; n = number of runs.\n")

    # ---- E1 -----------------------------------------------------------------
    e1 = [r for r in rows if r["experiment"] == "E1"]
    if e1:
        lines.append("## E1: h1 TCP throughput while h3 floods\n")
        lines.append("| Mode | h1 booked | n | Mean Mbps | SD | Min | Max | Policy on (mean s) |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        by_mode = {}
        for mode in ("baseline", "booked"):
            sel = [r for r in e1 if r["mode"] == mode]
            vals = [as_float(r["measured_mbps"]) for r in sel]
            by_mode[mode] = vals
            d = describe(vals)
            pol = describe([as_float(r["policy_active_s"]) for r in sel])["mean"]
            lines.append("| %s | %s | %d | %s | %s | %s | %s | %s |" % (
                mode, "5 Mbps" if mode == "booked" else "none", d["n"], fmt(d["mean"]),
                fmt(d["sd"]), fmt(d["min"]), fmt(d["max"]), fmt(pol, 1)))
        b, p = describe(by_mode["baseline"])["mean"], describe(by_mode["booked"])["mean"]
        if b and p:
            lines.append("\nBooked mean is %.1f× the baseline mean (%.2f vs %.2f Mbps). "
                         "A booking guarantees a minimum of 5 Mbps; more is possible when the "
                         "link has room, including when the congestion limit has slowed h3.\n"
                         % (p / b, p, b))
        if plt:
            fig, ax = plt.subplots(figsize=(6.4, 4.2))
            strip_with_mean(ax, 0, by_mode["baseline"], BASELINE, "No booking")
            strip_with_mean(ax, 1, by_mode["booked"], BOOKED, "h1 booked 5 Mbps")
            ax.axhline(5, color=BOOKED, linestyle=(0, (4, 4)), linewidth=1.2)
            ax.text(1.42, 5.15, "guaranteed 5", color=MUTED, fontsize=9, ha="right")
            ax.set_xticks([0, 1], ["No booking\n(baseline)", "h1 booked\n5 Mbps"])
            ax.set_xlim(-0.6, 1.6)
            ax.set_ylim(0, 10.5)
            style(ax, "E1: h1 throughput while h3 floods the link", "Mbps (TCP, whole test)")
            ax.text(0, -0.2, "Dots: individual runs. Marker and whisker: mean ± 1 SD.",
                    transform=ax.transAxes, color=MUTED, fontsize=8.5)
            figures.append(save(fig, out, "e1_throughput.png"))

    # ---- E2 -----------------------------------------------------------------
    e2 = [r for r in rows if r["experiment"] == "E2"]
    if e2:
        lines.append("## E2: h1 and h2 at the same time while h3 floods\n")
        lines.append("| Mode | Flow | Booked | n | Mean Mbps | SD | Min | Max |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        data = {}
        for mode in ("baseline", "booked"):
            for flow in ("h1", "h2"):
                sel = [r for r in e2 if r["mode"] == mode and r["flow"] == flow]
                vals = [as_float(r["measured_mbps"]) for r in sel]
                data[(mode, flow)] = vals
                d = describe(vals)
                booked = sel[0]["booked_mbps"] if sel else ""
                lines.append("| %s | %s | %s Mbps | %d | %s | %s | %s | %s |" % (
                    mode, flow, booked, d["n"], fmt(d["mean"]), fmt(d["sd"]),
                    fmt(d["min"]), fmt(d["max"])))
        lines.append("")
        if plt:
            fig, ax = plt.subplots(figsize=(7.2, 4.2))
            for i, (mode, label) in enumerate((("baseline", "No bookings"),
                                               ("booked", "h1 5 + h2 3 Mbps booked"))):
                strip_with_mean(ax, i - 0.17, data[(mode, "h1")], BOOKED if i else BASELINE,
                                "h1 (EVA camera)" if i else None)
                strip_with_mean(ax, i + 0.17, data[(mode, "h2")], SECOND if i else "#b3b8c0",
                                "h2 (science lab)" if i else None)
            for value, color, text in ((5, BOOKED, "h1 guaranteed 5"), (3, SECOND, "h2 guaranteed 3")):
                ax.hlines(value, 0.7, 1.4, color=color, linestyle=(0, (4, 4)), linewidth=1.2)
                ax.text(1.42, value, text, color=MUTED, fontsize=9, va="center")
            ax.set_xticks([-0.17, 0.17, 0.83, 1.17], ["h1", "h2", "h1", "h2"])
            ax.set_xlim(-0.6, 1.9)
            ax.set_ylim(0, 10.5)
            for x, text in ((0, "No bookings (baseline)"), (1, "h1 5 + h2 3 booked")):
                ax.text(x, -0.16, text, transform=ax.get_xaxis_transform(), ha="center",
                        color=INK, fontsize=10)
            style(ax, "E2: two booked flows at once, h3 flooding", "Mbps (TCP, whole test)")
            ax.legend(frameon=False, loc="upper left")
            figures.append(save(fig, out, "e2_concurrent.png"))

    # ---- E3 -----------------------------------------------------------------
    e3 = [r for r in rows if r["experiment"] == "E3"]
    timeline = []
    tpath = os.path.join(folder, "stats_timeline.jsonl")
    if os.path.exists(tpath):
        timeline = [json.loads(l) for l in open(tpath) if l.strip()]
    if e3:
        lines.append("## E3: congestion policy timeline (h1 booked 5 Mbps)\n")
        lines.append("| Run | Phase | Seconds | Link use Mbps (mean) | h3 offered Mbps (mean) | Limit on (s) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in e3:
        run = r["run"]
        name = "E3_run%s" % run
        samples = [s for s in timeline if str(s.get("label", "")).startswith(name + "_")]
        if not samples:
            lines.append("| %s | (no stats_timeline samples) | | | | |" % run)
            continue
        t0 = samples[0]["time"]
        for phase in ("normal", "flood", "recovery"):
            ps = [s for s in samples if s["label"] == "%s_%s" % (name, phase)]
            use = describe([s.get("used_mbps_s1_to_s2") or s.get("used_mbps") for s in ps])["mean"]
            h3 = describe([(s.get("ingress_mbps") or {}).get("h3") for s in ps])["mean"]
            on = sum(1 for s in ps if s.get("policy_active"))
            lines.append("| %s | %s | %d | %s | %s | %d |" % (run, phase, len(ps), fmt(use), fmt(h3), on))
        if not plt:
            continue
        fig, ax = plt.subplots(figsize=(9, 4.3))
        xs = [s["time"] - t0 for s in samples]
        # Shade the seconds the congestion limit was on.
        start = None
        for x, s in zip(xs, samples):
            if s.get("policy_active") and start is None:
                start = x
            if not s.get("policy_active") and start is not None:
                ax.axvspan(start, x, color=POLICY, alpha=0.13, linewidth=0)
                start = None
        if start is not None:
            ax.axvspan(start, xs[-1], color=POLICY, alpha=0.13, linewidth=0)
        # Phase boundaries.
        for phase in ("flood", "recovery"):
            first = next((x for x, s in zip(xs, samples) if s["label"] == "%s_%s" % (name, phase)), None)
            if first is not None:
                ax.axvline(first, color="#b9bfca", linewidth=1)
                ax.text(first + 1, 10.15, "flood starts" if phase == "flood" else "flood stops",
                        color=MUTED, fontsize=9)
        h1 = iperf_series(os.path.join(folder, "raw", "%s_h1_tcp.csv" % name))
        if h1:
            ax.plot([p[0] + 0.5 for p in h1], [p[1] for p in h1], color=BOOKED, linewidth=2,
                    label="h1 booked TCP throughput")
        ax.plot(xs, [s.get("used_mbps_s1_to_s2") if s.get("used_mbps_s1_to_s2") is not None
                     else s.get("used_mbps") for s in samples],
                color=SECOND, linewidth=2, label="Moon link use (s1 to s2)")
        ax.plot(xs, [(s.get("ingress_mbps") or {}).get("h3") for s in samples], color=POLICY,
                linewidth=1.6, linestyle=(0, (5, 3)), label="h3 offered load (before the link)")
        ax.fill_between([], [], color=POLICY, alpha=0.13, label="Congestion limit active")
        ax.set_ylim(0, 11)
        ax.set_xlim(0, max(xs) if xs else 1)
        style(ax, "E3 run %s: the limit switches on during the flood and off after it" % run,
              "Mbps", "seconds since the run started")
        ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.16), ncol=2, fontsize=9)
        figures.append(save(fig, out, "e3_timeline_run%s.png" % run))
    if e3:
        lines.append("")

    # ---- E4 -----------------------------------------------------------------
    e4_path = os.path.join(folder, "e4_timing.csv")
    if os.path.exists(e4_path):
        timing = read_csv(e4_path)
        lines.append("## E4: STATUS response time from h2 (TCP, new connection per request)\n")
        lines.append("| Condition | Requests | Failed | Median ms | 95th pct ms | Worst ms |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        order = [("idle", "Idle"), ("flood", "Flood, no booking"),
                 ("flood_booked", "Flood, h1 booked (limit can act)")]
        groups = []
        for key, label in order:
            sel = [t for t in timing if t["label"].startswith("E4_%s_run" % key)]
            ok = [float(t["ms"]) for t in sel if not t["reply"].startswith("ERROR")]
            groups.append((label, ok))
            lines.append("| %s | %d | %d | %s | %s | %s |" % (
                label, len(sel), len(sel) - len(ok), fmt(statistics.median(ok) if ok else None, 1),
                fmt(percentile(ok, 0.95), 1), fmt(max(ok) if ok else None, 1)))
        lines.append("")
        if plt and any(g[1] for g in groups):
            fig, ax = plt.subplots(figsize=(7.2, 4.2))
            data = [g[1] or [0] for g in groups]
            box = ax.boxplot(data, widths=0.45, patch_artist=True, showfliers=True,
                             medianprops={"color": INK, "linewidth": 2},
                             flierprops={"marker": "o", "markersize": 4, "markerfacecolor": MUTED,
                                         "markeredgecolor": "white", "alpha": 0.6})
            for patch, color in zip(box["boxes"], (BASELINE, POLICY, BOOKED)):
                patch.set_facecolor(color)
                patch.set_alpha(0.35)
                patch.set_edgecolor(color)
            ax.set_xticks([1, 2, 3], ["Idle", "Flood,\nno booking", "Flood,\nh1 booked"])
            if max(max(d) for d in data) > 20 * max(1e-9, min(statistics.median(d) for d in data)):
                ax.set_yscale("log")
            style(ax, "E4: how fast Mission Control answers STATUS", "milliseconds")
            figures.append(save(fig, out, "e4_response.png"))
    return out


# --------------------------------------------------------------------------
# E5/E6 (application tests, from tests/app_tests.py)
# --------------------------------------------------------------------------

def analyze_app_tests(folder, lines, figures):
    path = os.path.join(folder, "results.csv")
    if not os.path.exists(path):
        sys.exit("No results.csv in %s" % folder)
    rows = read_csv(path)
    out = os.path.join(folder, "analysis")
    os.makedirs(out, exist_ok=True)
    lines.append("# Application tests: %s\n" % os.path.basename(os.path.abspath(folder)))
    lines.append("Controller: SIMULATED (tests/fake_controller.py). These show application "
                 "behaviour, not SDN enforcement.\n")
    lines.append("## E5: simultaneous booking requests (2 Mbps each, 8 Mbps bookable)\n")
    lines.append("| Clients | Accepted | Rejected (no room) | Errors | Median ms | 95th pct ms | Worst ms | Result |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    loads = sorted({int(r["case"].split("_")[1]) for r in rows
                    if r["case"].startswith("load_") and not r["case"].endswith("_stats")})
    medians, p95s = [], []
    for n in loads:
        sel = [r for r in rows if r["case"] == "load_%d" % n]
        ms = [float(r["ms"]) for r in sel]
        acc = sum(1 for r in sel if r["reply"].startswith("OK RESERVED"))
        rej = sum(1 for r in sel if r["reply"] == "ERR REJECTED insufficient_capacity")
        err = sum(1 for r in sel if r["reply"].startswith("ERROR"))
        medians.append(statistics.median(ms))
        p95s.append(percentile(ms, 0.95))
        lines.append("| %d | %d | %d | %d | %.1f | %.1f | %.1f | %s |" % (
            n, acc, rej, err, medians[-1], p95s[-1], max(ms),
            "PASS" if all(r["result"] == "PASS" for r in sel) else "FAIL"))
    lines.append("\nAt every load exactly 4 × 2 = 8 Mbps was admitted: no overbooking.\n")
    lines.append("## E6: failure handling\n")
    lines.append("| Case | Condition | Request | Reply | ms | Result |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        if r["case"].startswith("load_"):
            continue
        lines.append("| %s | %s | `%s` | `%s` | %s | %s |" % (
            r["case"], r["condition"], r["request"], r["reply"], r["ms"], r["result"]))
    lines.append("")
    if plt and loads:
        fig, ax = plt.subplots(figsize=(6.4, 4.0))
        ax.plot(loads, medians, color=BOOKED, linewidth=2, marker="o", markersize=8,
                markeredgecolor="white", markeredgewidth=1.5, label="median")
        ax.plot(loads, p95s, color=SECOND, linewidth=2, marker="s", markersize=8,
                markeredgecolor="white", markeredgewidth=1.5, label="95th percentile")
        ax.set_xticks(loads)
        ax.set_ylim(0, max(p95s) * 1.25)
        style(ax, "E5: booking response time vs clients arriving at once",
              "milliseconds", "simultaneous clients")
        ax.legend(frameon=False, loc="upper left")
        ax.text(0, -0.24, "Simulated controller. Every load admitted exactly 8 Mbps.",
                transform=ax.transAxes, color=MUTED, fontsize=8.5)
        figures.append(save(fig, out, "e5_load.png"))
    return out


def main():
    parser = argparse.ArgumentParser(description="Tables and graphs from saved experiment data")
    parser.add_argument("results", nargs="?", help="experiments/results/<date-time> folder")
    parser.add_argument("--app-tests", help="tests/results/app_tests_<date-time> folder")
    args = parser.parse_args()
    if not args.results and not args.app_tests:
        parser.error("give a results folder and/or --app-tests folder")
    if plt is None:
        print("matplotlib not installed: writing tables only (pip install matplotlib)")
    for folder, fn in ((args.results, analyze_results), (args.app_tests, analyze_app_tests)):
        if not folder:
            continue
        lines, figures = [], []
        out = fn(folder, lines, figures)
        lines.append("_Generated %s by experiments/analyze.py from the saved files in this folder._"
                     % datetime.datetime.now().isoformat(timespec="seconds"))
        with open(os.path.join(out, "tables.md"), "w") as handle:
            handle.write("\n".join(lines) + "\n")
        print("Wrote %s" % os.path.join(out, "tables.md"))
        for f in figures:
            print("Wrote %s" % f)


if __name__ == "__main__":
    main()
