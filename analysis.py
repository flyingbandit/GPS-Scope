"""
Run analysis for GPS Scope - shared by the live UI and `analyse_run.py`.

Design rules (to keep the numbers honest):
  * Phases come from the operator (phase buttons / markers), never from the data.
    Automatically *detected* onset/offset is reported separately, for comparison.
  * Every statistic is a median with its inter-quartile range and sample count.
    Nothing is dropped or trimmed. Phases under MIN_CONFIDENT_S are flagged.
  * Satellite C/N0 change is *paired*: each satellite is compared with itself,
    only for satellites tracked in both phases, so satellites rising/setting
    can't bias it. Baseline satellites that vanish are counted, not hidden.
  * Position error is measured from the mean of the baseline phase's own 3D fixes.
  * Recovery uses fixed criteria (below). If they're never met, it says so
    ("not recovered within N s") instead of reporting a time.
"""
from __future__ import annotations

import math
import statistics

MIN_CONFIDENT_S = 60          # phases shorter than this are flagged
MIN_SAMPLES = 5               # below this a statistic is not reported
RECOVERY_HOLD_S = 10          # criteria must hold continuously this long
PAIR_MIN = 5                  # samples a sat needs in each phase to be paired
OVERCONFIDENT_K = 2.0         # error > K x hAcc = receiver over-confident

# Recovery criteria, relative to the baseline phase medians:
#   sats used  >= baseline - 1
#   top-6 C/N0 >= baseline - 1 dB
#   PGA        >= baseline - 1 dB          (M10 only)
#   position error <= max(1.0 m, 2 x baseline p95 error)   (GPS wanders slowly even when clean)
RECOVERY_RULES = "SVs used ≥ baseline−1, C/N0 top-6 ≥ baseline−1 dB, PGA ≥ baseline−1 dB (M10), " \
                 "H. error ≤ max(1 m, 2× baseline p95), held 10 s"

METRICS = [  # key, label, unit, decimals, which direction is better
    ("cnoTop", "C/N0 top-6", "dB-Hz", 1, "up"),
    ("used", "SVs used", "", 1, "up"),
    ("tracked", "SVs tracked", "", 1, "up"),
    ("pga", "PGA gain", "dB", 1, "up"),
    ("agcPct", "AGC", "%", 1, "up"),
    ("noise", "Noise level (post-PGA)", "/ms", 0, None),
    ("jamInd", "CW jamming ind. (post-PGA)", "", 1, None),
    ("spikes", "Spurs per sweep", "", 0, "down"),
    ("specL1c", "L1 in-band power (comp.)", "dB", 1, "down"),
    ("hAcc", "hAcc", "m", 2, "down"),
    ("pDOP", "PDOP", "", 2, "down"),
]


def q(vals, p):
    """Percentile with linear interpolation (p in 0..1)."""
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def stat(vals, dec=2):
    vals = [x for x in vals if x is not None]
    if len(vals) < MIN_SAMPLES:
        return {"n": len(vals), "med": None}
    return {"n": len(vals), "med": round(q(vals, .5), dec), "p25": round(q(vals, .25), dec),
            "p75": round(q(vals, .75), dec)}


def phase_of(t, phases):
    cur = phases[0]
    for p in phases:
        if t >= p["t"]:
            cur = p
    return cur


def build_phases(marks, t_start, t_end):
    """marks: [(t, name)] in time order. Run start opens the first phase."""
    ps = [{"name": "Baseline", "t": t_start}] if not marks or marks[0][0] - t_start > 2 else []
    for t, name in marks:
        if ps and t - ps[-1]["t"] < 0.5:      # two marks at once: keep the later name
            ps[-1]["name"] = name
            continue
        if ps and ps[-1]["name"] == name:     # same button pressed again: the phase just continues
            continue
        ps.append({"name": name, "t": t})
    for i, p in enumerate(ps):
        p["end"] = ps[i + 1]["t"] if i + 1 < len(ps) else t_end
        p["dur"] = round(p["end"] - p["t"], 1)
    return ps


def baseline_index(ps):
    for i, p in enumerate(ps):
        if "base" in p["name"].lower():
            return i
    return 0


def enu(lat, lon, lat0, lon0):
    dn = (lat - lat0) * 111_320.0
    de = (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))
    return de, dn


def analyse(timeline, sats, pvt, marks, t_start, t_end):
    """
    timeline: [{t, cnoTop, used, ...}]   (~1-2 Hz)
    sats:     [(t, key, gnss, cno, elev, used)]
    pvt:      [(t, lat, lon, hAcc, fixType, pDOP)]
    marks:    [(t, phase name)]
    """
    ps = build_phases(sorted(marks), t_start, t_end)
    bi = baseline_index(ps)
    base = ps[bi]

    # --- position reference: mean of the baseline phase's 3D fixes -----------
    bfix = [r for r in pvt if base["t"] <= r[0] < base["end"] and r[4] in (3, 4)]
    ref = None
    if len(bfix) >= MIN_SAMPLES:
        ref = (statistics.mean(r[1] for r in bfix), statistics.mean(r[2] for r in bfix))
    perr = []  # (t, err, hAcc, de, dn)
    if ref:
        for t, lat, lon, hacc, fx, _ in pvt:
            if fx in (2, 3, 4):
                de, dn = enu(lat, lon, *ref)
                perr.append((t, math.hypot(de, dn), hacc, de, dn))

    # --- per-sat series for pairing ------------------------------------------
    by_phase_sat: dict = {}
    for t, key, gn, cno, elev, used in sats:
        p = phase_of(t, ps)
        if not (p["t"] <= t < p["end"]):
            continue
        d = by_phase_sat.setdefault(p["t"], {}).setdefault(key, {"gnss": gn, "cno": [], "n": 0, "elev": elev})
        d["n"] += 1
        if cno > 0:
            d["cno"].append(cno)

    bsats = by_phase_sat.get(base["t"], {})
    out_phases = []
    for i, p in enumerate(ps):
        samp = [s for s in timeline if p["t"] <= s["t"] < p["end"]]
        o = {"name": p["name"], "t": p["t"], "end": p["end"], "dur": p["dur"], "n": len(samp),
             "baseline": i == bi, "lowConfidence": p["dur"] < MIN_CONFIDENT_S, "metrics": {}}
        for key, _, _, dec, _ in METRICS:
            o["metrics"][key] = stat([s.get(key) for s in samp], dec)
        pe = [e for e in perr if p["t"] <= e[0] < p["end"]]
        o["metrics"]["posErr"] = stat([e[1] for e in pe], 2)
        if len(pe) >= MIN_SAMPLES:
            o["metrics"]["posErr"]["p95"] = round(q([e[1] for e in pe], .95), 2)
            o["metrics"]["posErr"]["max"] = round(max(e[1] for e in pe), 2)
            ratio = [e[1] / e[2] for e in pe if e[2] > 0]
            o["overconfidentPct"] = round(100 * sum(1 for r in ratio if r > OVERCONFIDENT_K) / len(ratio), 1) if ratio else None
            o["errOverHaccP95"] = round(q(ratio, .95), 2) if ratio else None
        # paired C/N0 vs baseline
        if i != bi:
            cur = by_phase_sat.get(p["t"], {})
            deltas, lost, per_const = [], [], {}
            for k, b in bsats.items():
                if len(b["cno"]) < PAIR_MIN:
                    continue
                c = cur.get(k)
                if c is None or c["n"] < PAIR_MIN:
                    continue                                   # not observed long enough - can't say
                if len(c["cno"]) < 0.2 * c["n"]:
                    lost.append(k)                             # observed, but mostly not tracked
                    continue
                if len(c["cno"]) >= PAIR_MIN:
                    d = statistics.median(c["cno"]) - statistics.median(b["cno"])
                    deltas.append(d)
                    per_const.setdefault(b["gnss"], []).append(d)
            o["paired"] = {"n": len(deltas), "lost": lost,
                           "med": round(q(deltas, .5), 1) if len(deltas) >= 3 else None,
                           "p25": round(q(deltas, .25), 1) if len(deltas) >= 3 else None,
                           "p75": round(q(deltas, .75), 1) if len(deltas) >= 3 else None,
                           "byConst": {g: {"n": len(v), "med": round(q(v, .5), 1)} for g, v in per_const.items()}}
        out_phases.append(o)

    # --- deltas vs baseline (medians) ----------------------------------------
    bm = out_phases[bi]["metrics"]
    for o in out_phases:
        o["delta"] = {}
        if o["baseline"]:
            continue
        for k, v in o["metrics"].items():
            b = bm.get(k, {})
            if v.get("med") is not None and b.get("med") is not None:
                o["delta"][k] = round(v["med"] - b["med"], 2)

    # --- recovery after each "off" phase -------------------------------------
    rec = []
    bmed = {k: bm.get(k, {}).get("med") for k in ("used", "cnoTop", "pga")}
    bp95 = bm.get("posErr", {}).get("p95")
    err_lim = max(1.0, 2 * bp95) if bp95 is not None else None
    for i, p in enumerate(ps):
        if i == bi or "off" not in p["name"].lower():
            continue
        samp = [s for s in timeline if p["t"] <= s["t"] < p["end"]]
        held_since, got = None, None
        for s in samp:
            ok = True
            if bmed["used"] is not None and (s.get("used") is None or s["used"] < bmed["used"] - 1):
                ok = False
            if bmed["cnoTop"] is not None and (s.get("cnoTop") is None or s["cnoTop"] < bmed["cnoTop"] - 1):
                ok = False
            if bmed["pga"] is not None and (s.get("pga") is None or s["pga"] < bmed["pga"] - 1):
                ok = False
            if err_lim is not None:
                near = [e[1] for e in perr if abs(e[0] - s["t"]) <= 0.6]
                if not near or max(near) > err_lim:
                    ok = False
            if ok:
                held_since = held_since or s["t"]
                if s["t"] - held_since >= RECOVERY_HOLD_S:
                    got = held_since - p["t"]
                    break
            else:
                held_since = None
        rec.append({"phase": p["name"], "seconds": round(got, 1) if got is not None else None,
                    "observed": p["dur"], "recovered": got is not None})

    # --- detected onset/offset (for comparison with the operator's marks) ----
    det = []
    bsp = bm.get("spikes", {}).get("med")
    signals = []
    if bmed["pga"] is not None:
        signals.append(("PGA reduction ≥3 dB", lambda s: s.get("pga") is not None and s["pga"] <= bmed["pga"] - 3))
    if bsp is not None:
        signals.append(("spur burst", lambda s: s.get("spikes") is not None and s["spikes"] >= max(8, 4 * (bsp + 1))))
    for name, test in signals:      # a change must persist 3 samples (no flapping)
        state, run_, first = False, 0, None
        for s in timeline:
            if not (t_start <= s["t"] <= t_end):
                continue
            if test(s) != state:
                run_ += 1
                first = first if run_ > 1 else s["t"]
                if run_ >= 3:
                    state = not state
                    det.append({"t": first, "signal": name, "what": ("started" if state else "ended")})
                    run_ = 0
            else:
                run_ = 0
    det.sort(key=lambda d: d["t"])
    # warn where the data changed state well before/after the nearest phase boundary
    notes = []
    bounds = [p for p in ps[1:]]
    for d in det:
        if not bounds:
            break
        nb = min(bounds, key=lambda p: abs(p["t"] - d["t"]))
        lag = nb["t"] - d["t"]
        if 1.0 < lag < 8:       # data changed BEFORE the mark: the previous phase is contaminated
            prev = phase_of(d["t"], ps)["name"]
            notes.append(f"{d['signal']} {d['what']} {lag:.1f} s before the '{nb['name']}' mark: the last "
                         f"{lag:.1f} s of '{prev}' are contaminated.")
        elif -8 < lag < -2.5:   # data changed well AFTER the mark (receiver lag is normally 1-2 s)
            notes.append(f"{d['signal']} {d['what']} {-lag:.1f} s after the '{nb['name']}' mark: the first "
                         f"{-lag:.1f} s of '{nb['name']}' precede the change.")

    return {"phases": out_phases, "recovery": rec, "recoveryRules": RECOVERY_RULES, "detected": det, "notes": notes,
            "positionRef": "mean of baseline-phase 3D fixes" if ref else None,
            "baselinePhase": base["name"], "metricsMeta": [{"key": k, "label": l, "unit": u, "dec": d, "better": b}
                                                         for k, l, u, d, b in METRICS]}


# --------------------------------------------------------------------------
# Multi-receiver comparison (same run, same phase marks, same sky)
# --------------------------------------------------------------------------
COMPARE_KEYS = [  # key, label, unit, decimals, better
    ("paired", "ΔC/N0 paired", "dB", 1, "up"),
    ("dCno", "ΔC/N0 top-6", "dB", 1, "up"),
    ("dUsed", "ΔSVs used", "", 1, "up"),
    ("lost", "SVs lost", "", 0, "down"),
    ("dPga", "ΔPGA", "dB", 1, "up"),
    ("spikes", "Spurs per sweep", "", 0, "down"),
    ("dMove", "ΔH. error (median)", "m", 2, "down"),
    ("moveP95", "H. error p95", "m", 2, "down"),
    ("overconf", "Error > 2×hAcc", "%", 1, "down"),
]
ADJUSTABLE = {"paired", "dCno", "dUsed", "dPga", "dMove"}


def compare(ana):
    """
    ana: {rid: (meta, analysis)}. Phases are matched by position (every receiver got
    the same marks). Control-adjusted value = receiver's change - mean change of the
    control receivers in the same phase: what's left is the effect at that receiver's
    position, with sky / satellite geometry / weather changes removed.
    """
    rids = list(ana)
    if not rids:
        return None
    ref = max(rids, key=lambda r: len(ana[r][1]["phases"]))
    names = [p["name"] for p in ana[ref][1]["phases"]]
    controls = [r for r in rids if ana[r][0].get("role") == "control"]
    tests = [r for r in rids if ana[r][0].get("role") != "ignore"]
    phases = []
    for i, name in enumerate(names):
        rows = {}
        for r in tests:
            ps = ana[r][1]["phases"]
            if i >= len(ps):
                continue
            p = ps[i]
            if p.get("baseline"):
                continue
            pr = p.get("paired") or {}
            m = p["metrics"]
            rec = next((x for x in ana[r][1].get("recovery", []) if x["phase"] == name), None)
            rows[r] = {"paired": pr.get("med"), "pairedN": pr.get("n"), "lost": len(pr.get("lost", [])) if pr else None,
                       "dCno": p["delta"].get("cnoTop"), "dUsed": p["delta"].get("used"),
                       "dPga": p["delta"].get("pga"), "spikes": m.get("spikes", {}).get("med"),
                       "dMove": p["delta"].get("posErr"), "moveP95": m.get("posErr", {}).get("p95"),
                       "overconf": p.get("overconfidentPct"), "recovery": rec, "short": p.get("lowConfidence")}
        if not rows:
            continue
        adj = {}
        cvals = {k: [rows[c][k] for c in controls if c in rows and rows[c].get(k) is not None] for k in ADJUSTABLE}
        for r, row in rows.items():
            if r in controls:
                continue
            adj[r] = {k: (round(row[k] - statistics.mean(cvals[k]), 2) if row.get(k) is not None and cvals[k] else None)
                      for k in ADJUSTABLE}
        phases.append({"name": name, "rows": rows, "adjusted": adj})
    return {"receivers": {r: {"name": ana[r][0].get("name") or r, "role": ana[r][0].get("role"),
                              "product": ana[r][0].get("product"), "distance_cm": ana[r][0].get("distance_cm"),
                              "antenna": ana[r][0].get("antenna"), "location": ana[r][0].get("location")}
                          for r in tests},
            "controls": controls, "phases": phases,
            "keys": [{"key": k, "label": l, "unit": u, "dec": d, "better": b, "adjustable": k in ADJUSTABLE}
                     for k, l, u, d, b in COMPARE_KEYS],
            "note": None if controls else "No control receiver assigned - values are not corrected for common-mode "
                                          "sky effects. Set one receiver, away from the source, to Control."}
