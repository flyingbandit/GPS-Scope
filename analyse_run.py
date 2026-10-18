#!/usr/bin/env python3
"""
Re-analyse a recorded GPS Scope run (works on runs made with older versions too).

  python3 analyse_run.py runs/20261017-152821_Run-2
  python3 analyse_run.py runs/<run> --json            # machine-readable

Phases come from 'phase' events. Runs recorded before phase buttons existed use
their markers as phase boundaries instead (each marker starts a phase named after it).
"""
import argparse
import csv
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analysis  # noqa: E402


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def spikes_per_frame(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        rows = list(csv.reader(f))
    for r in rows[1:]:
        a = [float(v) for v in r[5:]]
        n = len(a)
        res = [a[i] - statistics.median(a[max(0, i - 8):i + 9]) for i in range(n)]
        out[float(r[0])] = sum(1 for i in range(1, n - 1) if res[i] >= 20 and res[i] >= res[i - 1] and res[i] >= res[i + 1])
    return out


def load(run):
    rd = lambda n: list(csv.DictReader(open(os.path.join(run, n)))) if os.path.exists(os.path.join(run, n)) else []  # noqa: E731
    tl = rd("timeline.csv")
    pv = rd("pvt.csv")
    ev = rd("events.csv")
    sp = spikes_per_frame(os.path.join(run, "spectrum.csv"))
    spt = sorted(sp)
    timeline = []
    for r in tl:
        s = {k: fnum(v) for k, v in r.items() if k != "cnoBy"}
        if "spikes" not in r and spt:
            t = s["t"]
            near = min(spt, key=lambda x: abs(x - t))
            s["spikes"] = sp[near] if abs(near - t) < 1.5 else None
        timeline.append(s)
    # pDOP lives in pvt.csv; attach nearest
    if pv and "lat" not in pv[0]:
        # shareable copy: no coordinates, only metres from the baseline average. Feed them in as
        # small offsets from (0, 0) - the analysis only ever uses distances between fixes.
        pv = [r for r in pv if r.get("dE_m") not in (None, "")]
        pvt = [(float(r["t"]), float(r["dN_m"]) / 111_320.0, float(r["dE_m"]) / 111_320.0, float(r["hAcc"]),
                int(r["fixType"]), fnum(r.get("pDOP"))) for r in pv]
    else:
        pvt = [(float(r["t"]), float(r["lat"]), float(r["lon"]), float(r["hAcc"]), int(r["fixType"]),
                fnum(r.get("pDOP"))) for r in pv]
    j = 0
    for s in timeline:
        while j + 1 < len(pvt) and pvt[j + 1][0] <= s["t"]:
            j += 1
        if pvt and s.get("pDOP") is None:
            s["pDOP"] = pvt[j][5]
    sats = []
    with open(os.path.join(run, "sats.csv")) as f:
        last = {}
        for r in csv.DictReader(f):
            t = float(r["t"])
            if t - last.get(r["sat"], 0) < 0.9:   # 1 Hz per satellite is plenty
                continue
            last[r["sat"]] = t
            sats.append((t, r["sat"], r["gnss"], int(r["cno"]), int(r["elev"]), r["used"] == "1"))
    marks = [(float(e["t"]), e["msg"]) for e in ev if e["kind"] == "phase"]
    source = "phase buttons"
    if not marks:
        marks = [(float(e["t"]), e["msg"]) for e in ev if e["kind"] == "marker"]
        source = "markers (run recorded before phase buttons existed)"
    starts = [float(e["t"]) for e in ev if e["kind"] == "run" and "started" in e["msg"]]
    t0 = starts[0] if starts else timeline[0]["t"]
    t1 = max(timeline[-1]["t"], pvt[-1][0] if pvt else 0)
    return timeline, sats, pvt, marks, t0, t1, source


def fmt(v, d=1):
    return "–" if v is None else f"{v:.{d}f}"


def report(a, source, t0):
    print(f"Phases from {source}. Baseline phase: '{a['baselinePhase']}'.\n")
    meta = {m["key"]: m for m in a["metricsMeta"]}
    meta["posErr"] = {"label": "Position error vs baseline mean", "unit": "m", "dec": 2}
    for p in a["phases"]:
        flag = "  (short - low confidence)" if p["lowConfidence"] else ""
        print(f"== {p['name']}  +{p['t'] - t0:.0f}s, {p['dur']:.0f}s, {p['n']} samples{flag}")
        for k, m in p["metrics"].items():
            if m.get("med") is None:
                continue
            mm = meta.get(k, {"label": k, "unit": "", "dec": 2})
            d = p["delta"].get(k)
            ds = f"   Δ {d:+.{mm['dec']}f}" if d is not None else ""
            extra = f"  p95 {m['p95']}  max {m['max']}" if k == "posErr" and "p95" in m else ""
            print(f"   {mm['label']:30} {m['med']:>8} {mm['unit']:5} (IQR {m['p25']}–{m['p75']}, n={m['n']}){extra}{ds}")
        if p.get("overconfidentPct") is not None:
            print(f"   {'Error > 2× claimed accuracy':30} {p['overconfidentPct']:>7}% of fixes "
                  f"(p95 error/hAcc = {p['errOverHaccP95']})")
        if "paired" in p:
            pr = p["paired"]
            bc = ", ".join(f"{g} {v['med']:+.1f} (n={v['n']})" for g, v in pr["byConst"].items())
            print(f"   {'Paired C/N0 change':30} {fmt(pr['med']):>8} dB    (IQR {fmt(pr['p25'])}–{fmt(pr['p75'])}, "
                  f"{pr['n']} sats tracked in both) {bc}")
            if pr["lost"]:
                print(f"   {'Baseline sats lost':30} {len(pr['lost']):>8}       ({', '.join(pr['lost'])})")
        print()
    for r in a["recovery"]:
        print(f"Recovery after '{r['phase']}': " +
              (f"{r['seconds']} s" if r["recovered"] else f"NOT recovered within the {r['observed']:.0f} s recorded"))
    print(f"  criteria: {a['recoveryRules']}")
    if a["detected"]:
        print("Detected from the data (compare with your marks): " +
              "; ".join(f"{d['signal']} {d['what']} at +{d['t'] - t0:.1f}s" for d in a["detected"]))
    for n in a["notes"]:
        print("NOTE: " + n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    timeline, sats, pvt, marks, t0, t1, source = load(a.run)
    res = analysis.analyse(timeline, sats, pvt, marks, t0, t1)
    res["phaseSource"] = source
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        report(res, source, t0)
    with open(os.path.join(a.run, "analysis.json"), "w") as f:
        json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()
