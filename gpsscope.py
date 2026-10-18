#!/usr/bin/env python3
"""
GPS Scope - live u-blox M8 / M10 interference diagnostics with a web UI.

Built to answer: "is my ELRS receiver (or VTX, camera, ...) degrading my GPS?"

  * per-satellite / per-constellation C/N0, elevation, azimuth, used-in-fix
  * per-signal detail (NAV-SIG) on gen-9/10 receivers
  * RF front-end health: jamming indicator, AGC, noise, antenna status
  * RF spectrum (MON-SPAN) on gen-9/10 receivers (M10, M9, F9...)
  * satellite-loss / C/N0-dip / jamming / fix-change event detection
  * labelled test runs logged to CSV, with per-run summary for A/B comparison

All receiver configuration is written to RAM only - power-cycling the GPS
restores whatever the flight controller (or you) saved to flash.

Usage:
  python3 gpsscope.py --port /dev/serial0          # Pi UART (GPIO14/15)
  python3 gpsscope.py --port /dev/ttyUSB0          # USB-UART adapter
  python3 gpsscope.py --sim                        # no hardware, demo data
Then browse to http://<pi-ip>:8080
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import math
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
import zipfile

import analysis
import analyse_run
import share
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

try:
    import serial  # pyserial
    from pyubx2 import POLL, SET, UBX_PROTOCOL, UBXMessage, UBXReader
except ImportError:  # sim mode still works without them
    serial = None

HERE = os.path.dirname(os.path.abspath(__file__))
GPSSCOPE_VERSION = "2026-10-18.12"

GNSS = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC"}
GNSS_PREFIX = {0: "G", 1: "S", 2: "E", 3: "B", 4: "I", 5: "Q", 6: "R", 7: "N"}

# (gnssId, sigId) -> (signal name, band)
SIGNALS = {
    (0, 0): ("L1 C/A", "L1"), (0, 3): ("L2 CL", "L2"), (0, 4): ("L2 CM", "L2"),
    (0, 6): ("L5 I", "L5"), (0, 7): ("L5 Q", "L5"),
    (1, 0): ("L1 C/A", "L1"),
    (2, 0): ("E1 C", "L1"), (2, 1): ("E1 B", "L1"), (2, 3): ("E5a I", "L5"),
    (2, 4): ("E5a Q", "L5"), (2, 5): ("E5b I", "L2"), (2, 6): ("E5b Q", "L2"),
    (3, 0): ("B1I D1", "L1"), (3, 1): ("B1I D2", "L1"), (3, 2): ("B2I D1", "L2"),
    (3, 3): ("B2I D2", "L2"), (3, 5): ("B1C", "L1"), (3, 7): ("B2a", "L5"),
    (5, 0): ("L1 C/A", "L1"), (5, 1): ("L1S", "L1"), (5, 4): ("L2 CM", "L2"),
    (5, 5): ("L2 CL", "L2"), (5, 8): ("L5 I", "L5"), (5, 9): ("L5 Q", "L5"),
    (6, 0): ("L1 OF", "L1"), (6, 2): ("L2 OF", "L2"),
    (7, 0): ("L5 A", "L5"),
}
QUALITY = {0: "no signal", 1: "searching", 2: "acquired", 3: "unusable",
           4: "code lock", 5: "code+carrier", 6: "code+carrier", 7: "code+carrier"}
FIX = {0: "No fix", 1: "DR only", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "Time only"}
JAM = {0: "unknown", 1: "OK", 2: "warning", 3: "critical"}
ANT = {0: "init", 1: "unknown", 2: "OK", 3: "short", 4: "open"}

# Reference frequencies drawn on the spectrum (MHz)
BAND_MARKERS = [
    {"name": "GPS L1 / Galileo E1", "short": "L1/E1", "f": 1575.42, "bw": 2.046},
    {"name": "BeiDou B1I", "short": "B1I", "f": 1561.098, "bw": 4.092},
    {"name": "GLONASS L1", "short": "G1", "f": 1602.0, "bw": 8.0},
]

# Event thresholds
DROP_ELEV_MIN = 20       # deg - losing a sat above this is "unexpected"
DROP_CNO_MIN = 28        # dB-Hz - ...if it was this strong
DIP_DB = 8               # dB single-epoch C/N0 drop that counts as a dip
MASS_DROP_N = 3          # sats lost in one epoch => RF event, not geometry
HISTORY_SECONDS = 3600
SPAN_DB_PER_LSB = 0.25   # UBX-MON-SPAN spectrum bytes are quarter-dB (u-blox: dB = value / 4)
PGA_DB_PER_STEP = 0.5    # spectrum dB moved per PGA dB (measured ~0.52 on a real M10: 5.75 dB / 11 PGA)
SPUR_DB = 5.0            # narrowband peak this far above baseline (gain-corrected) = spur event
DROP_USED_MIN_S = 5      # a sat must have been in the fix this long...
DROP_GONE_S = 3          # ...and out of it this long, before we call it a drop (no flapping)
CNO_STEP_DB = 4          # strongest sats falling this much together = broadband/front-end event
GAIN_CUT_ALERT_DB = 3    # PGA this far below reference = strong in-band energy
AGC_DROP_ALERT_PCT = 10  # M8 (no PGA): AGC this far below reference
GAIN_REF_WINDOW = 600    # s - auto reference = highest gain seen in this window


def now() -> float:
    return time.time()


def top_mean(vals, n):
    vals = sorted((v for v in vals if v > 0), reverse=True)[:n]
    return round(sum(vals) / len(vals), 2) if vals else None


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------
class State:
    """Everything about ONE receiver."""
    def __init__(self, log_dir: str, rid: str = "gps1", port: str = ""):
        self.lock = threading.RLock()
        self.log_dir = log_dir
        self.rid, self.port = rid, port
        self.meta = {"name": rid, "role": "test", "product": "", "antenna": "", "location": "",
                     "distance_cm": "", "notes": ""}
        self.hub = None
        os.makedirs(log_dir, exist_ok=True)
        self.info = {"pgaK": PGA_DB_PER_STEP, "status": "starting", "model": None, "fw": None, "protver": None,
                     "port": None, "baud": None, "spectrum": False, "signals": False,
                     "sim": False, "msg_counts": {}}
        self.pvt: dict = {}
        self.sats: dict = {}          # key "G12" -> dict
        self.sigs: list = []
        self.rf: list = []
        self.spec: dict | None = None  # current spectrum block 0
        self.spec_max: list | None = None
        self.spec_baseline: dict | None = None
        self.history = collections.deque(maxlen=HISTORY_SECONDS)
        self.events = collections.deque(maxlen=500)
        self.event_seq = 0
        self.run: Run | None = None
        self.clog = ContinuousLog(log_dir, rid)
        self.last_jam = None
        self.last_fix = None
        self.sim_interference = False
        self.gain_hist = collections.deque(maxlen=GAIN_REF_WINDOW)  # (t, pga, agcPct)
        self.gain_alert = False
        self.gain = {}
        self.sat_track: dict = {}
        self.recent_drops = collections.deque(maxlen=50)
        self._last_mass = 0
        self._last_step = 0
        self._spur_last: dict = {}
        self._spec_recent = collections.deque(maxlen=5)
        self._dense_frames = 0
        self._comb_gaps = collections.Counter()
        self._spike_count = None
        self.spec_hist = collections.deque(maxlen=600)   # waterfall on page load + phase baselines (10 min)
        self.phase_name, self.phase_start = None, now()
        self.spec_max_label = "(since start)"
        self._prev_sats: dict = {}
        self.pos_hist = collections.deque(maxlen=6000)  # (t, lat, lon, alt, fixType) - 10 min at 10 Hz
        self.trail = collections.deque(maxlen=900)      # (t, dE, dN, dU)
        self.ref = None
        self.drift_thresh = 3.0
        self.drifting = False
        self._load_ref()
        self._last_sample = 0

    # -- events -----------------------------------------------------------
    def event(self, kind, msg, severity="info", data=None):
        with self.lock:
            self.event_seq += 1
            ev = {"id": self.event_seq, "t": now(), "kind": kind, "msg": msg,
                  "severity": severity, "data": data or {},
                  "run": self.run.label if self.run else None}
            self.events.append(ev)
            self.clog.event(ev)
            if self.run:
                self.run.on_event(ev)

    # -- message handlers -------------------------------------------------
    def on_nav_sat(self, msg):
        sats = {}
        for i in range(1, msg.numSvs + 1):
            g = getattr(msg, f"gnssId_{i:02d}")
            sv = getattr(msg, f"svId_{i:02d}")
            key = f"{GNSS_PREFIX.get(g, '?')}{sv:02d}"
            sats[key] = {
                "key": key, "gnss": GNSS.get(g, str(g)), "gnssId": g, "sv": sv,
                "cno": getattr(msg, f"cno_{i:02d}"),
                "elev": getattr(msg, f"elev_{i:02d}"),
                "azim": getattr(msg, f"azim_{i:02d}"),
                "prRes": round(getattr(msg, f"prRes_{i:02d}"), 1),
                "quality": getattr(msg, f"qualityInd_{i:02d}"),
                "used": bool(getattr(msg, f"svUsed_{i:02d}")),
                "health": getattr(msg, f"health_{i:02d}"),
            }
        self.update_sats(sats)

    def update_sats(self, sats: dict):
        with self.lock:
            self._detect_sat_events(self._prev_sats, sats)
            self._prev_sats = sats
            self.sats = sats
            if self.run:
                self.run.log_sats(sats)
            self._sample()

    def on_nav_sig(self, msg):
        sigs = []
        for i in range(1, msg.numSigs + 1):
            g = getattr(msg, f"gnssId_{i:02d}")
            sid = getattr(msg, f"sigId_{i:02d}")
            name, band = SIGNALS.get((g, sid), (f"sig{sid}", "?"))
            sigs.append({
                "key": f"{GNSS_PREFIX.get(g, '?')}{getattr(msg, f'svId_{i:02d}'):02d}",
                "gnss": GNSS.get(g, str(g)), "sig": name, "band": band,
                "cno": getattr(msg, f"cno_{i:02d}"),
                "quality": getattr(msg, f"qualityInd_{i:02d}"),
                "prUsed": bool(getattr(msg, f"prUsed_{i:02d}")),
                "freqId": getattr(msg, f"freqId_{i:02d}"),
            })
        with self.lock:
            self.sigs = sigs

    def on_nav_pvt(self, msg):
        pvt = {"fixType": msg.fixType, "fix": FIX.get(msg.fixType, "?"),
               "fixOk": bool(msg.gnssFixOk), "numSV": msg.numSV,
               "lat": round(msg.lat, 7), "lon": round(msg.lon, 7),
               "alt": round(msg.hMSL / 1000, 1), "hAcc": round(msg.hAcc / 1000, 2),
               "vAcc": round(msg.vAcc / 1000, 2), "pDOP": round(msg.pDOP, 2),
               "speed": round(msg.gSpeed / 1000, 2),
               "utc": f"{msg.hour:02d}:{msg.min:02d}:{msg.second:02d}"}
        self.update_pvt(pvt)

    # -- fixed reference position / drift ----------------------------------
    def _ref_path(self):
        d = os.path.join(self.log_dir, "_receivers")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{self.rid}_reference.json")

    def _load_ref(self):
        try:
            with open(self._ref_path()) as f:
                d = json.load(f)
            self.ref, self.drift_thresh = d.get("ref"), d.get("thresh", 3.0)
        except (OSError, ValueError):
            pass

    def set_ref(self, ref, thresh=None):
        with self.lock:
            self.ref = ref
            if thresh:
                self.drift_thresh = float(thresh)
            self.trail.clear()
            self.drifting = False
            with open(self._ref_path(), "w") as f:
                json.dump({"ref": self.ref, "thresh": self.drift_thresh}, f)
            if ref:
                self.event("ref", f"Reference position set ({ref['source']}): "
                           f"{ref['lat']:.7f}, {ref['lon']:.7f} - alert above {self.drift_thresh} m")

    def avg_position(self, seconds=60, since=None):
        with self.lock:
            t = now()
            t0 = since if since is not None else t - seconds
            pts = [p for p in self.pos_hist if p[0] >= t0 and p[4] in (3, 4)]
            if len(pts) < 5:
                return None
            return {"lat": round(statistics.mean(p[1] for p in pts), 8),
                    "lon": round(statistics.mean(p[2] for p in pts), 8),
                    "alt": round(statistics.mean(p[3] for p in pts), 2),
                    "n": len(pts), "source": f"average of {len(pts)} fixes"}

    def _apply_ref(self, pvt):
        if not self.ref or pvt["fixType"] not in (2, 3, 4):
            pvt["err"] = None
            return
        lat0, lon0 = self.ref["lat"], self.ref["lon"]
        dN = (pvt["lat"] - lat0) * 111_320.0
        dE = (pvt["lon"] - lon0) * 111_320.0 * math.cos(math.radians(lat0))
        dU = pvt["alt"] - self.ref["alt"] if self.ref.get("alt") is not None else None
        err = math.hypot(dN, dE)
        pvt.update(dN=round(dN, 2), dE=round(dE, 2), dU=round(dU, 2) if dU is not None else None,
                   err=round(err, 2),
                   bearing=round(math.degrees(math.atan2(dE, dN)) % 360) if err > 0 else 0)
        self.trail.append((round(now(), 1), pvt["dE"], pvt["dN"], pvt["dU"]))
        # hysteresis: alert when above threshold, clear when back under 70 %
        if not self.drifting and err > self.drift_thresh:
            self.drifting = True
            self.event("drift", f"Position error {err:.1f} m re reference "
                       f"(bearing {pvt['bearing']}°, hAcc {pvt['hAcc']} m), static receiver",
                       "alert", {"err": round(err, 2), "dE": pvt["dE"], "dN": pvt["dN"]})
        elif self.drifting and err < self.drift_thresh * 0.7:
            self.drifting = False
            self.event("drift", f"Position error back within {self.drift_thresh} m of reference "
                       f"({err:.1f} m)", "good", {"err": round(err, 2)})
        pvt["drifting"] = self.drifting

    def update_pvt(self, pvt):
        with self.lock:
            self.pos_hist.append((now(), pvt["lat"], pvt["lon"], pvt["alt"], pvt["fixType"]))
            self._apply_ref(pvt)
            if self.last_fix is not None and pvt["fixType"] != self.last_fix:
                worse = pvt["fixType"] < self.last_fix or pvt["fixType"] not in (2, 3, 4)
                self.event("fix", f"Fix {FIX.get(self.last_fix)} → {pvt['fix']}",
                           "alert" if worse else "good")
            self.last_fix = pvt["fixType"]
            self.pvt = pvt
            if self.run:
                self.run.log_pvt(pvt)

    def on_mon_rf(self, msg):
        blocks = []
        for i in range(1, msg.nBlocks + 1):
            s = lambda f: getattr(msg, f"{f}_{i:02d}")  # noqa: E731
            blocks.append({"block": s("blockId"), "jamState": s("jammingState"),
                           "jamInd": s("jamInd"), "agcCnt": s("agcCnt"),
                           "noise": s("noisePerMS"), "antStatus": s("antStatus"),
                           "antPower": s("antPower"), "magI": s("magI"), "magQ": s("magQ"),
                           "ofsI": s("ofsI"), "ofsQ": s("ofsQ")})
        self.update_rf(blocks)

    def on_mon_hw(self, msg):  # M8 and older
        self.update_rf([{"block": 0, "jamState": msg.jammingState, "jamInd": msg.jamInd,
                         "agcCnt": msg.agcCnt, "noise": msg.noisePerMS,
                         "antStatus": msg.aStatus, "antPower": msg.aPower}])

    def update_rf(self, blocks):
        with self.lock:
            for b in blocks:
                b["agcPct"] = round(b["agcCnt"] / 8191 * 100, 1)
                b["jam"] = JAM.get(b["jamState"], "?")
                b["ant"] = ANT.get(b["antStatus"], "?")
            js = max((b["jamState"] for b in blocks), default=0)
            if self.last_jam is not None and js != self.last_jam and js != 0:
                sev = {1: "good", 2: "warn", 3: "alert"}.get(js, "info")
                self.event("jam", f"Receiver jamming flag: {JAM.get(js)}", sev,
                           {"jamInd": blocks[0]["jamInd"]})
            self.last_jam = js
            self.rf = blocks
            if self.run:
                self.run.log_rf(blocks)

    def on_mon_span(self, msg):
        for i in range(1, msg.numRfBlocks + 1):
            raw = getattr(msg, f"spectrum_{i:02d}")
            self.update_spectrum(i - 1, list(raw), getattr(msg, f"span_{i:02d}"),
                                 getattr(msg, f"res_{i:02d}"), getattr(msg, f"center_{i:02d}"),
                                 getattr(msg, f"pga_{i:02d}"))

    def update_spectrum(self, block, raw, span, res, center, pga):
        if block != 0:  # multi-band receivers: show L1 block only
            return
        # Raw values are relative dB *after* the receiver's PGA. When a strong
        # signal appears the AGC lowers PGA gain, which pulls the whole trace down,
        # so the PGA value is shown and logged alongside (see README).
        db = [v * SPAN_DB_PER_LSB for v in raw]   # quarter-dB bytes -> dB, as u-center shows them
        n = len(db)
        with self.lock:
            same = self.spec and self.spec["center"] == center and self.spec["span"] == span
            self.spec = {"t": now(), "db": db, "pga": pga, "span": span, "res": res,
                         "center": center,
                         "f0": (center - span / 2) / 1e6, "df": span / n / 1e6}
            if not same or self.spec_max is None:
                self.spec_max = list(db)
            else:
                self.spec_max = [max(a, b) for a, b in zip(self.spec_max, db)]
            if self.run:
                self.run.log_spec(self.spec)
            self.spec_hist.append((round(self.spec["t"], 2), db))
            self._detect_spurs()

    def comb_spacing(self, df):
        """Repeating spike spacing (tentative). Pair counts are normalised by how many
        pairs fit at each gap, then the best pitch must stand clear of the rest."""
        g = self._comb_gaps
        if sum(g.values()) < 400:
            return None
        n = len(self.spec["db"]) if self.spec else 256
        def score(b):
            return sum(g[m * b + e] / (n - (m * b + e)) for m in range(1, 5) for e in (-1, 0, 1) if m * b + e < n)
        cand = list(range(max(2, math.ceil(0.6 / df)), int(7.5 / df) + 1))  # 0.6 - 7.5 MHz
        sc = {b: score(b) for b in cand}
        med = statistics.median(sc.values())
        mad = statistics.median(abs(v - med) for v in sc.values()) or 1e-9
        best = max(sc, key=sc.get)
        for d in (2, 3):  # prefer the fundamental over a harmonic of it
            if best % d == 0 and best // d in sc and sc[best // d] >= 0.8 * sc[best]:
                best //= d
        return round(best * df, 2) if (sc[best] - med) / mad > 6 else None

    def _comb_decay(self):
        for k in list(self._comb_gaps):
            self._comb_gaps[k] = int(self._comb_gaps[k] * 0.8)
            if not self._comb_gaps[k]:
                del self._comb_gaps[k]

    def _detect_spurs(self):
        """Narrowband peaks relative to their own neighbourhood, compared with the
        same in the baseline - independent of the receiver's gain changes."""
        sp = self.spec
        a = sp["db"]
        n0 = len(a)
        res = [a[i] - statistics.median(a[max(0, i - 8):i + 9]) for i in range(n0)]
        self._spike_count = sum(1 for i in range(1, n0 - 1) if res[i] >= SPUR_DB and res[i] >= res[i - 1] and res[i] >= res[i + 1])
        self._spec_recent.append(sp["db"])
        b = self.spec_baseline
        if not b or len(b["db"]) != len(sp["db"]) or len(self._spec_recent) < 3:
            return
        def peaks(a):
            return [a[i] - statistics.median(a[max(0, i - 8):i + 9]) for i in range(len(a))]
        bp = peaks(b["db"])
        frames = [[x - y for x, y in zip(peaks(f), bp)] for f in list(self._spec_recent)[-3:]]
        n = len(bp)
        # Many spikes that move frame to frame = hopping / pulsed wideband source (one event, not dozens)
        cur = frames[-1]
        spikes = [i for i in range(1, n - 1) if cur[i] >= SPUR_DB and cur[i] >= cur[i - 1] and cur[i] >= cur[i + 1]]
        if len(spikes) >= 8:
            self._dense_frames += 1
            for x in range(len(spikes)):                # pairwise spacing histogram -> comb pitch
                for y in range(x + 1, len(spikes)):
                    self._comb_gaps[spikes[y] - spikes[x]] += 1
            comb = self.comb_spacing(sp["df"])
            if self._dense_frames >= 2 and sp["t"] - self._spur_last.get("dense", 0) >= 60:
                self._spur_last["dense"] = sp["t"]
                lo, hi = sp["f0"] + spikes[0] * sp["df"], sp["f0"] + spikes[-1] * sp["df"]
                top = max(cur[i] for i in spikes)
                ctxt = (f", comb pitch ~{comb:.2f} MHz (tentative; typical of an SMPS or spread-spectrum clock)") if comb else ""
                self.event("spur", f"Wideband spur field: {len(spikes)} spurs {lo:.1f}-{hi:.1f} MHz, up to {top:.1f} dB "
                           f"above floor{ctxt}; non-stationary (hopping/pulsed). Covers L1/E1/B1I.",
                           "alert", {"spikes": len(spikes), "lo": round(lo, 2), "hi": round(hi, 2), "comb": comb})
            return
        self._dense_frames = 0
        self._comb_decay()
        hit = [all(fr[i] >= SPUR_DB for fr in frames) for i in range(n)]
        ex = [statistics.median(fr[i] for fr in frames) for i in range(n)]
        i = 0
        while i < n:                       # one report per cluster of adjacent bins
            if not hit[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and hit[j + 1]:
                j += 1
            k = max(range(i, j + 1), key=lambda q: ex[q])
            i = j + 1
            f = sp["f0"] + k * sp["df"]
            key = round(f)
            if sp["t"] - self._spur_last.get(key, 0) < 60:
                continue
            self._spur_last[key] = sp["t"]
            near = min(BAND_MARKERS, key=lambda m: abs(m["f"] - f))
            inband = abs(f - 1575.42) < 6 or abs(f - 1561.1) < 4
            self.event("spur", f"Narrowband spur {f:.2f} MHz, +{ex[k]:.1f} dB re local floor "
                       f"({f - near['f']:+.2f} MHz from {near['name']}), absent in baseline",
                       "alert" if inband else "warn", {"f": round(f, 3), "db": round(ex[k], 1)})

    # -- derived timeline sample (1 Hz, driven by NAV-SAT) ----------------
    def _sample(self):
        t = now()
        if t - self._last_sample < 0.5:
            return
        self._last_sample = t
        by = collections.defaultdict(list)
        for s in self.sats.values():
            by[s["gnss"]].append(s["cno"])
        rf0 = self.rf[0] if self.rf else {}
        gain = self._gain_status(t, rf0)
        spec_l1 = None
        if self.spec:
            sp = self.spec
            lo, hi = 1574.0, 1577.0
            vals = [v for k, v in enumerate(sp["db"])
                    if lo <= sp["f0"] + k * sp["df"] <= hi]
            spec_l1 = round(statistics.mean(vals), 2) if vals else None
        spec_l1c = None
        if spec_l1 is not None and gain.get("pgaRef") is not None:
            # undo the receiver's gain reduction
            spec_l1c = round(spec_l1 + PGA_DB_PER_STEP * (gain["pgaRef"] - self.spec["pga"]), 2)
        sample = {
            "t": round(t, 2),
            "cnoTop": top_mean([s["cno"] for s in self.sats.values()], 6),
            "cnoBy": {g: top_mean(v, 4) for g, v in by.items()},
            "used": sum(1 for s in self.sats.values() if s["used"]),
            "tracked": sum(1 for s in self.sats.values() if s["cno"] > 0),
            "jamInd": rf0.get("jamInd"), "agcPct": rf0.get("agcPct"),
            "noise": rf0.get("noise"), "hAcc": self.pvt.get("hAcc") if self.pvt.get("fixType") in (2, 3, 4) else None,
            "fixType": self.pvt.get("fixType"), "specL1": spec_l1,
            "pga": self.spec["pga"] if self.spec else None,
            "gainCut": gain.get("gainCut"), "agcDrop": gain.get("agcDrop"),
            "specL1c": spec_l1c,
            "spikes": self._spike_count if self.spec and t - self.spec["t"] < 3 else None,
            "pDOP": self.pvt.get("pDOP") if self.pvt.get("fixType") in (2, 3, 4) else None,
            "runErr": self.run.run_err(self.pvt) if self.run and self.pvt else None,
            "phase": self.run.phase if self.run else None,
            "posErr": self.pvt.get("err"),
        }
        self.history.append(sample)
        self._detect_cno_step(t)
        self.clog.sample(sample, self.run.label if self.run else None)
        if self.run:
            self.run.on_sample(sample)

    # -- gain reduction: the real broadband-interference signature --------
    def _gain_status(self, t, rf0):
        pga = self.spec["pga"] if self.spec else None
        agc = rf0.get("agcPct")
        self.gain_hist.append((t, pga, agc))
        g = {}
        if pga is not None:
            if self.spec_baseline:
                ref, src = self.spec_baseline["pga"], "baseline"
            else:
                ref, src = max(p for _, p, _ in self.gain_hist if p is not None), "10-min max"
            g.update(pga=pga, pgaRef=ref, gainCut=max(0, ref - pga), refSrc=src)
        if agc is not None:
            aref = max(a for _, _, a in self.gain_hist if a is not None)
            g.update(agcRef=aref, agcDrop=round(max(0.0, aref - agc), 1))
        if "gainCut" in g:
            bad = g["gainCut"] >= GAIN_CUT_ALERT_DB
            clear = g["gainCut"] < GAIN_CUT_ALERT_DB - 1
            txt = f"−{g['gainCut']} dB (PGA {pga} dB, ref {g['pgaRef']} dB, {g['refSrc']})"
        elif "agcDrop" in g:
            bad = g["agcDrop"] >= AGC_DROP_ALERT_PCT
            clear = g["agcDrop"] < AGC_DROP_ALERT_PCT * 0.6
            txt = f"AGC −{g['agcDrop']} % (ref {g['agcRef']} %)"
        else:
            bad = clear = False
            txt = ""
        if bad and not self.gain_alert:
            self.gain_alert = True
            self.event("gain", f"PGA reduction: {txt} - in-band RFI. (CW jamming indicator is post-PGA and may fall.)",
                       "alert", dict(g))
        elif clear and self.gain_alert:
            self.gain_alert = False
            self.event("gain", f"PGA nominal: {txt}", "good", dict(g))
        g["alert"] = self.gain_alert
        self.gain = g
        return g

    # -- event detection --------------------------------------------------
    def _detect_sat_events(self, prev, cur):
        """Debounced: receivers at 5-10 Hz flick satellites in and out of the fix,
        so only report a sat that was used for DROP_USED_MIN_S and then stayed out
        for DROP_GONE_S. Reported once per drop."""
        t = now()
        top = top_mean([x["cno"] for x in cur.values()], 6) or 0
        for k, c in cur.items():
            tr = self.sat_track.setdefault(k, {"used_since": None, "unused_since": None, "dropped": False,
                                               "hist": collections.deque(maxlen=200)})
            tr["hist"].append((t, c["cno"], c["elev"], c["used"]))
            tr["last"] = c
            if c["used"]:
                tr["unused_since"] = None
                if tr["used_since"] is None:
                    tr["used_since"] = t
                if tr["dropped"] and t - tr["used_since"] >= DROP_USED_MIN_S:
                    tr["dropped"] = False
            else:
                if tr["unused_since"] is None:
                    tr["unused_since"] = t
                if (not tr["dropped"] and tr["used_since"] is not None
                        and tr["unused_since"] - tr["used_since"] >= DROP_USED_MIN_S
                        and t - tr["unused_since"] >= DROP_GONE_S):
                    tr["dropped"] = True
                    tr["used_since"] = None
                    self._report_drop(k, tr, c, top, t)
                elif tr["used_since"] is not None and tr["unused_since"] - tr["used_since"] < DROP_USED_MIN_S \
                        and t - tr["unused_since"] >= DROP_GONE_S:
                    tr["used_since"] = None   # never settled in the fix - not a drop
        for k, tr in self.sat_track.items():  # vanished from NAV-SAT entirely
            if k not in cur and tr.get("used_since") and not tr["dropped"] and t - tr["used_since"] >= DROP_USED_MIN_S:
                tr["dropped"] = True
                tr["used_since"] = None
                self._report_drop(k, tr, None, top, t)

    def _report_drop(self, k, tr, c, top, t):
        before = [h for h in tr["hist"] if t - DROP_GONE_S - 5 <= h[0] <= t - DROP_GONE_S]
        cno0 = statistics.median(h[1] for h in before) if before else tr["last"]["cno"]
        elev = (before[-1][2] if before else tr["last"]["elev"])
        last = tr["last"]
        gn = last["gnss"]
        why = "loss of lock" if c is None or c["cno"] == 0 else f"now {c['cno']} dB-Hz, excluded from fix"
        strong = cno0 >= max(20, top - 12)
        data = {"sat": k, "gnss": gn, "elev": elev, "azim": last["azim"], "cno": cno0}
        self.recent_drops.append((t, k, gn))
        if elev >= DROP_ELEV_MIN and strong:
            self.event("sat_drop", f"{k} ({gn}) dropped from fix at {elev}° elev, {cno0:.0f} dB-Hz prior - {why}",
                       "warn", data)
        else:
            self.event("sat_drop", f"{k} ({gn}) dropped at {elev}° elev, {cno0:.0f} dB-Hz "
                       f"(low elevation/weak - expected) - {why}", "info", data)
        burst = [d for d in self.recent_drops if t - d[0] <= 6]
        if len(burst) >= MASS_DROP_N and t - self._last_mass > 15:
            self._last_mass = t
            consts = sorted({d[2] for d in burst})
            self.event("mass_drop", f"{len(burst)} SVs dropped from fix within 6 s "
                       f"({', '.join(d[1] for d in burst)}) across {', '.join(consts)} - common-mode, consistent with RFI",
                       "alert", {"sats": [d[1] for d in burst], "constellations": consts})

    def _detect_cno_step(self, t):
        """Strongest satellites all losing C/N0 together = front-end desensitisation."""
        h = [x for x in self.history if x.get("cnoTop") is not None]
        now_ = [x["cnoTop"] for x in h if t - x["t"] <= 2]
        ref = [x for x in h if 4 <= t - x["t"] <= 14]
        if len(now_) < 2 or len(ref) < 5 or t - self._last_step < 15:
            return
        r = statistics.median(x["cnoTop"] for x in ref)
        d = r - statistics.median(now_)
        if d >= CNO_STEP_DB:
            self._last_step = t
            last = h[-1]["cnoBy"]
            refby = {g: statistics.median([x["cnoBy"].get(g) for x in ref if x["cnoBy"].get(g)] or [0])
                     for g in last}
            per = ", ".join(f"{g} {last[g] - refby[g]:+.0f}" for g in last if refby.get(g) and last.get(g))
            self.event("mass_dip", f"C/N0 step: top-6 down {d:.1f} dB ({r:.1f} → {r - d:.1f} dB-Hz; {per}) "
                       "- common-mode desensitisation", "alert", {"drop": round(d, 1)})

    # -- spectrum baseline / loudest, tied to phases --------------------------
    def capture_spec_baseline(self, label, since=None):
        """Typical (median) and loudest per frequency from frames since `since` (default: last 30 s)."""
        with self.lock:
            if not self.spec:
                return False
            t0 = since if since is not None else now() - 30
            n = len(self.spec["db"])
            fr = [d for t, d in self.spec_hist if t >= t0 and len(d) == n] or list(self._spec_recent) or [self.spec["db"]]
            self.spec_baseline = {"db": [statistics.median(c) for c in zip(*fr)], "max": [max(c) for c in zip(*fr)],
                                  "frames": len(fr), "pga": self.spec["pga"], "t": now(), "label": label}
            self.event("baseline", f"Spectrum baseline captured ({label}, {len(fr)} sweeps): median and peak per bin")
            return True

    def reset_max(self, label):
        with self.lock:
            self.spec_max = list(self.spec["db"]) if self.spec else None
            self.spec_max_label = label

    def on_phase(self, name):
        """New phase: leaving 'Baseline' records the spectrum baseline from that whole phase;
        every phase starts its own 'loudest' trace."""
        prev, start = self.phase_name, self.phase_start
        if prev and prev.lower().startswith("base") and not name.lower().startswith("base"):
            self.capture_spec_baseline(prev, since=start)
            # Position datum = mean of this Baseline phase's 3D fixes (no hand-picked point needed)
            r = self.avg_position(since=start)
            if r:
                r["source"] = f"Baseline phase average, {r['n']} fixes"
                self.set_ref(r)
        self.phase_name, self.phase_start = name, now()
        self.reset_max(f"({name})")

    # -- snapshot for the UI ---------------------------------------------
    def snapshot(self, since: float, ev_since: int):
        with self.lock:
            spec = None
            if self.spec:
                spec = dict(self.spec, max=self.spec_max, maxLabel=self.spec_max_label,
                            baseline=self.spec_baseline, markers=BAND_MARKERS,
                            comb=self.comb_spacing(self.spec["df"]) if self._dense_frames else None)
            return {
                "server_t": now(), "info": self.info, "pvt": self.pvt,
                "sats": sorted(self.sats.values(), key=lambda s: (s["gnssId"], s["sv"])),
                "sigs": self.sigs, "rf": self.rf, "spectrum": spec,
                "history": [h for h in self.history if h["t"] > since],
                "events": [e for e in self.events if e["id"] > ev_since],
                "run": self.run.status() if self.run else None,
                "ref": self.ref, "driftThresh": self.drift_thresh, "gain": self.gain,
                "trail": list(self.trail)[-300:],
                "rx": {"id": self.rid, "port": self.port, "meta": self.meta},
                "sim_interference": self.sim_interference,
            }


# --------------------------------------------------------------------------
# Always-on log (daily files) - so nothing is lost if no run was started
# --------------------------------------------------------------------------
class ContinuousLog:
    TL = ["t", "iso", "run", "cnoTop", "used", "tracked", "fixType", "hAcc", "posErr", "pga", "gainCut",
          "agcPct", "agcDrop", "noise", "jamInd", "specL1", "specL1c", "spikes", "pDOP", "runErr", "phase", "cnoBy"]
    EV = ["t", "iso", "run", "kind", "severity", "msg"]

    def __init__(self, log_dir, rid="gps1"):
        self.dir = os.path.join(log_dir, "_continuous", rid)
        os.makedirs(self.dir, exist_ok=True)
        self.day, self.f, self.n = None, {}, 0

    def _open(self):
        day = time.strftime("%Y%m%d")
        if day == self.day:
            return
        for f in self.f.values():
            f.close()
        self.day, self.f, self.w = day, {}, {}
        for name, hdr in (("timeline", self.TL), ("events", self.EV)):
            path = os.path.join(self.dir, f"{day}_{name}.csv")
            new = not os.path.exists(path)
            self.f[name] = open(path, "a", newline="")
            self.w[name] = csv.writer(self.f[name])
            if new:
                self.w[name].writerow(hdr)

    def sample(self, s, run):
        try:
            self._open()
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s["t"]))
            self.w["timeline"].writerow([s["t"], iso, run or ""] + [s.get(k) for k in self.TL[3:-1]] +
                                        [json.dumps(s["cnoBy"])])
            self.n += 1
            if self.n % 10 == 0:
                for f in self.f.values():
                    f.flush()
        except OSError as e:  # e.g. SD card full - never stop the UI for logging
            print(f"[log] continuous log error: {e}", flush=True)

    def event(self, ev):
        try:
            self._open()
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ev["t"]))
            self.w["events"].writerow([round(ev["t"], 2), iso, ev["run"] or "", ev["kind"], ev["severity"], ev["msg"]])
            self.f["events"].flush()
        except OSError as e:
            print(f"[log] continuous log error: {e}", flush=True)


# --------------------------------------------------------------------------
# Labelled test runs
# --------------------------------------------------------------------------
def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:40] or "run"


class Run:
    def __init__(self, log_dir, label, notes="", run_id=None, sub=None, start=None, meta=None, info=None):
        self.label = label
        self.notes = notes
        self.start = start or now()
        self.id = run_id or (time.strftime("%Y%m%d-%H%M%S") + "_" + slug(label))
        self.dir = os.path.join(log_dir, self.id, sub) if sub else os.path.join(log_dir, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.meta, self.info = dict(meta or {}), dict(info or {})
        with open(os.path.join(self.dir, "receiver.json"), "w") as f:   # spec stamped into the run
            json.dump({"meta": self.meta, "info": {k: v for k, v in self.info.items() if k != "msg_counts"}}, f, indent=2)
        self.raw = None
        try:                         # receiver's raw UBX stream: opens natively in u-center (File > Open)
            self.raw = open(os.path.join(self.dir, "raw.ubx"), "ab", buffering=65536)
        except OSError:
            pass
        self.files, self.w = {}, {}
        for name, hdr in {
            "sats": ["t", "sat", "gnss", "cno", "elev", "azim", "used", "quality", "prRes", "health"],
            "rf": ["t", "block", "jamState", "jamInd", "agcCnt", "agcPct", "noise", "antStatus", "antPower"],
            "pvt": ["t", "fixType", "numSV", "lat", "lon", "alt", "hAcc", "vAcc", "pDOP",
                    "dE_m", "dN_m", "dU_m", "err_m"],
            "timeline": ["t", "cnoTop", "used", "tracked", "jamInd", "agcPct", "noise", "hAcc",
                         "fixType", "specL1", "pga", "posErr", "gainCut", "agcDrop", "specL1c",
                         "spikes", "pDOP", "runErr", "phase", "cnoBy"],
            "events": ["t", "kind", "severity", "msg"],
            "spectrum": None,
        }.items():
            f = open(os.path.join(self.dir, f"{name}.csv"), "w", newline="")
            self.files[name] = f
            self.w[name] = csv.writer(f)
            if hdr:
                self.w[name].writerow(hdr)
        self.spec_hdr = False
        self.samples, self.n_drop, self.n_unexp, self.n_alert = [], 0, 0, 0
        self.lost_by_const = collections.Counter()
        self.n_drifts = 0
        self.marks = []            # (t, phase name) - operator's phase buttons
        self.mem_sats, self._sat_last = [], {}
        self.mem_pvt, self._pvt_last = [], 0
        self.base_fix = []         # 3D fixes during the baseline phase -> run's own position reference
        self.base_ref = None
        self.phase = "Baseline"
        self._ana, self._ana_t = None, 0

    def mark_phase(self, name):
        t = now()
        self.marks.append((t, name))
        if self.phase.lower().startswith("base") and not name.lower().startswith("base") and self.base_fix:
            self.base_ref = (statistics.mean(f[0] for f in self.base_fix), statistics.mean(f[1] for f in self.base_fix))
        self.phase = name

    def run_err(self, p):
        """Distance from this run's own baseline-phase mean position (None until baseline has data)."""
        if p.get("fixType") not in (2, 3, 4):
            return None
        ref = self.base_ref
        if ref is None and self.phase.lower().startswith("base") and len(self.base_fix) >= 10:
            ref = (statistics.mean(f[0] for f in self.base_fix), statistics.mean(f[1] for f in self.base_fix))
        if ref is None:
            return None
        de, dn = analysis.enu(p["lat"], p["lon"], *ref)
        return round(math.hypot(de, dn), 2)

    def analysis(self, force=False):
        if force or now() - self._ana_t > 3:
            self._ana = analysis.analyse(self.samples, self.mem_sats, self.mem_pvt, self.marks, self.start, now())
            self._ana["phaseSource"] = "phase buttons" if self.marks else "no phases marked yet - whole run is 'Baseline'"
            self._ana_t = now()
        return self._ana

    def log_sats(self, sats):
        t = round(now(), 2)
        for s in sats.values():
            if t - self._sat_last.get(s["key"], 0) >= 0.9:
                self._sat_last[s["key"]] = t
                self.mem_sats.append((t, s["key"], s["gnss"], s["cno"], s["elev"], s["used"]))
            self.w["sats"].writerow([t, s["key"], s["gnss"], s["cno"], s["elev"], s["azim"],
                                     int(s["used"]), s["quality"], s["prRes"], s["health"]])

    def log_rf(self, blocks):
        t = round(now(), 2)
        for b in blocks:
            self.w["rf"].writerow([t, b["block"], b["jamState"], b["jamInd"], b["agcCnt"],
                                   b["agcPct"], b["noise"], b["antStatus"], b["antPower"]])

    def log_pvt(self, p):
        t = now()
        if t - self._pvt_last >= 0.2:
            self._pvt_last = t
            self.mem_pvt.append((t, p["lat"], p["lon"], p["hAcc"], p["fixType"], p.get("pDOP")))
            if self.phase.lower().startswith("base") and p["fixType"] in (3, 4):
                self.base_fix.append((p["lat"], p["lon"]))
        self.w["pvt"].writerow([round(now(), 2), p["fixType"], p["numSV"], p["lat"], p["lon"],
                                p["alt"], p["hAcc"], p["vAcc"], p["pDOP"], p.get("dE"), p.get("dN"),
                                p.get("dU"), p.get("err")])

    def log_spec(self, sp):
        if not self.spec_hdr:
            self.w["spectrum"].writerow(["t", "pga", "center_hz", "span_hz", "res_hz"] +
                                        [f"{sp['f0'] + i * sp['df']:.3f}" for i in range(len(sp["db"]))])
            self.spec_hdr = True
        self.w["spectrum"].writerow([round(sp["t"], 2), sp["pga"], sp["center"], sp["span"],
                                     sp["res"]] + sp["db"])

    def on_sample(self, s):
        self.samples.append(s)
        self.w["timeline"].writerow([s["t"], s["cnoTop"], s["used"], s["tracked"], s["jamInd"],
                                     s["agcPct"], s["noise"], s["hAcc"], s["fixType"], s["specL1"],
                                     s["pga"], s["posErr"], s["gainCut"], s["agcDrop"], s["specL1c"],
                                     s["spikes"], s["pDOP"], s["runErr"], s["phase"], json.dumps(s["cnoBy"])])
        if len(self.samples) % 10 == 0:
            for f in self.files.values():
                f.flush()

    def on_event(self, ev):
        self.w["events"].writerow([round(ev["t"], 2), ev["kind"], ev["severity"], ev["msg"]])
        if ev["kind"] == "sat_drop":
            self.n_drop += 1
            if ev["severity"] == "warn":
                self.n_unexp += 1
                self.lost_by_const[ev["msg"].split("(")[1].split(")")[0]] += 1
        if ev["kind"] == "mass_drop":
            self.n_drop += len(ev["data"]["sats"])
            self.n_unexp += len(ev["data"]["sats"])
            for c in ev["data"]["constellations"]:
                self.lost_by_const[c] += 1
        if ev["kind"] == "drift" and ev["severity"] == "alert":
            self.n_drifts += 1
        if ev["severity"] == "alert":
            self.n_alert += 1

    def summary(self):
        def m(k):
            v = [s[k] for s in self.samples if s.get(k) is not None]
            return round(statistics.mean(v), 2) if v else None

        def mn(k):
            v = [s[k] for s in self.samples if s.get(k) is not None]
            return round(min(v), 2) if v else None

        def mx(k):
            v = [s[k] for s in self.samples if s.get(k) is not None]
            return round(max(v), 2) if v else None
        by = collections.defaultdict(list)
        for s in self.samples:
            for g, v in s["cnoBy"].items():
                if v:
                    by[g].append(v)
        fix3 = [s for s in self.samples if s.get("fixType") in (3, 4)]
        errs = sorted(s["posErr"] for s in self.samples if s.get("posErr") is not None)
        return {
            "id": self.id, "label": self.label, "notes": self.notes,
            "start": self.start, "duration": round(now() - self.start, 1),
            "samples": len(self.samples),
            "cnoTop": m("cnoTop"), "cnoTopMin": mn("cnoTop"),
            "cnoBy": {g: round(statistics.mean(v), 2) for g, v in by.items()},
            "used": m("used"), "usedMin": mn("used"), "tracked": m("tracked"),
            "jamInd": m("jamInd"), "jamIndMax": mx("jamInd"),
            "agcPct": m("agcPct"), "agcPctMin": mn("agcPct"),
            "noise": m("noise"), "hAcc": m("hAcc"), "specL1": m("specL1"),
            "pga": m("pga"), "pgaMin": mn("pga"),
            "gainCut": m("gainCut"), "gainCutMax": mx("gainCut"), "agcDrop": m("agcDrop"),
            "specL1c": m("specL1c"),
            "posErr": round(statistics.mean(errs), 2) if errs else None,
            "posErrP95": errs[int(0.95 * (len(errs) - 1))] if errs else None,
            "posErrMax": errs[-1] if errs else None,
            "driftEvents": self.n_drifts,
            "fix3Pct": round(100 * len(fix3) / len(self.samples), 1) if self.samples else None,
            "drops": self.n_drop, "unexpectedDrops": self.n_unexp, "alerts": self.n_alert,
            "lostByConstellation": dict(self.lost_by_const),
        }

    def status(self):
        return {"id": self.id, "label": self.label, "elapsed": round(now() - self.start, 1), "phase": self.phase,
                "summary": self.summary()}

    def close(self):
        s = self.summary()
        a = self.analysis(force=True)
        with open(os.path.join(self.dir, "analysis.json"), "w") as f:
            json.dump(a, f, indent=2)
        others = [p for p in a["phases"] if not p["baseline"] and p.get("paired", {}).get("med") is not None]
        if others:
            worst = min(others, key=lambda p: p["paired"]["med"])
            s["pairedWorst"] = {"phase": worst["name"], "med": worst["paired"]["med"], "n": worst["paired"]["n"],
                                "lost": len(worst["paired"]["lost"])}
        s["recovery"] = a["recovery"]
        s["phases"] = [p["name"] for p in a["phases"]]
        s["meta"] = self.meta
        s["model"] = self.info.get("model")
        with open(os.path.join(self.dir, "summary.json"), "w") as f:
            json.dump(s, f, indent=2)
        for f in self.files.values():
            f.close()
        raw, self.raw = self.raw, None
        if raw:
            try:
                raw.close()
            except OSError:
                pass
        return s


# --------------------------------------------------------------------------
# Receiver I/O
# --------------------------------------------------------------------------
class UbxRelay(threading.Thread):
    """Raw UBX byte stream of one receiver on a TCP port, for u-center (Receiver > Connection >
    Network connection > tcp://<pi>:<port>).

    By default u-center may only *ask* the GPS things: UBX polls (MON-VER, NAV-*, CFG polls) and
    CFG-VALGET are passed to the receiver so u-center can identify it and fill its panes. Anything
    that would change the GPS (CFG-VALSET, CFG-CFG, resets, log erase, firmware...) and any NMEA is
    discarded. With allow_write everything u-center sends goes to the GPS."""

    POLL_CLASSES = {0x01, 0x02, 0x06, 0x0A, 0x0B, 0x0D, 0x10, 0x27}   # NAV RXM CFG MON AID TIM ESF SEC
    QUEUE_MAX = 4096                                                   # frames buffered per client (~10 s)

    def __init__(self, port, rid, writer=None, allow_write=False, local_only=False):
        super().__init__(daemon=True, name=f"ubxtcp-{rid}")
        self.port, self.rid, self.writer, self.allow_write = port, rid, writer, allow_write
        self.clients, self.lock = {}, threading.Lock()      # socket -> deque of pending bytes
        if local_only:                         # web UI is localhost-only, so keep the relay local too
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("127.0.0.1", port))
            self.srv.listen(4)
            return
        try:                                   # IPv4 + IPv6: Windows apps often try a .local name's IPv6 first
            self.srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            self.srv.bind(("::", port))
        except OSError:
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("0.0.0.0", port))
        self.srv.listen(4)

    def run(self):
        while True:
            try:
                c, addr = self.srv.accept()
            except OSError:
                time.sleep(1)
                continue
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.settimeout(None)
            q, ev = collections.deque(), threading.Event()
            with self.lock:
                self.clients[c] = (q, ev)
            who = addr[0][7:] if addr[0].startswith("::ffff:") else addr[0]
            print(f"[{self.rid}] u-center connected from {who} (tcp :{self.port}, "
                  f"{'read/write' if self.allow_write else 'read-only, polls passed'})", flush=True)
            threading.Thread(target=self._to_client, args=(c, q, ev, addr[0]), daemon=True).start()
            threading.Thread(target=self._from_client, args=(c,), daemon=True).start()

    def _is_query(self, f):
        cls, mid, ln = f[2], f[3], f[4] | (f[5] << 8)
        if cls == 0x06 and mid == 0x8B:          # CFG-VALGET: reads config, changes nothing
            return True
        return ln == 0 and cls in self.POLL_CLASSES

    def _from_client(self, c):
        buf, logged = bytearray(), 0
        while True:
            try:
                b = c.recv(4096)
            except OSError:
                b = b""
            if not b:
                self._drop(c, "disconnected")
                return
            if not self.writer:
                continue
            if self.allow_write:
                try:
                    self.writer(b)
                except Exception:  # noqa: BLE001
                    pass
                continue
            buf += b
            while True:                            # pull whole UBX frames out, skip anything else
                i = buf.find(b"\xb5\x62")
                if i < 0:
                    if buf and logged < 40:
                        logged += 1
                        print(f"[{self.rid}] u-center sent {len(buf)} non-UBX bytes (dropped): {bytes(buf[:40])!r}", flush=True)
                    buf.clear()
                    break
                if i and logged < 40:
                    logged += 1
                    print(f"[{self.rid}] u-center sent {i} non-UBX bytes (dropped): {bytes(buf[:min(i, 40)])!r}", flush=True)
                del buf[:i]
                if len(buf) < 6:
                    break
                n = 8 + (buf[4] | (buf[5] << 8))
                if n > 4096:
                    del buf[:2]
                    continue
                if len(buf) < n:
                    break
                f = bytes(buf[:n])
                del buf[:n]
                ok = self._is_query(f)
                if logged < 40:
                    logged += 1
                    print(f"[{self.rid}] u-center sent UBX {f[2]:02X}-{f[3]:02X} len {n - 8}: "
                          f"{'passed' if ok else 'blocked'}", flush=True)
                if ok:
                    try:
                        self.writer(f)
                    except Exception:  # noqa: BLE001
                        pass

    def _to_client(self, c, q, ev, who):
        while True:
            ev.wait(1.0)
            ev.clear()
            with self.lock:
                if c not in self.clients:
                    return
                data = b"".join(q)
                q.clear()
            if not data:
                continue
            try:
                c.sendall(data)
            except OSError:
                self._drop(c, "send failed")
                return

    def _drop(self, c, why):
        with self.lock:
            had = self.clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass
        if had:
            print(f"[{self.rid}] u-center {why} (tcp :{self.port})", flush=True)

    def send(self, raw):
        if not self.clients:
            return
        with self.lock:
            for c, (q, ev) in list(self.clients.items()):
                if len(q) >= self.QUEUE_MAX:       # client far behind: skip frames, never stall the GPS
                    continue
                q.append(raw)
                ev.set()

    def count(self):
        return len(self.clients)


class Receiver(threading.Thread):
    BAUDS = [115200, 38400, 9600, 57600, 230400, 460800]

    def __init__(self, st: State, port, baud, target_baud, keep_nmea, decimate=1):
        super().__init__(daemon=True, name=f"rx-{st.rid}")
        self.st, self.port, self.baud = st, port, baud
        self.decimate = max(1, int(decimate))   # NAV-SAT/NAV-SIG every Nth epoch (FC passthrough)
        self.via_fc = False                     # GPS behind a flight controller: re-enter passthrough on reconnect
        self.fc_auto = False                    # ...and re-find the FC's port if it changes (--fc auto)
        self.target_baud, self.keep_nmea = target_baud, keep_nmea
        self.ser = None
        self.relay = None            # UbxRelay, set by the Hub
        self.wlock = threading.Lock()

    def log(self, m):
        print(f"[{self.st.rid}] {m}", flush=True)
        with self.st.lock:
            self.st.info["status"] = m

    def run(self):
        while True:
            try:
                self.connect()
                self.loop()
            except Exception as e:  # noqa: BLE001
                self.log(f"disconnected: {e}")
                try:
                    self.ser and self.ser.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2)

    def _probe(self, baud):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, baud, 0.3
        s.dtr = s.rts = False
        s.open()
        s.reset_input_buffer()
        # nudge a quiet receiver into replying
        s.write(UBXMessage("MON", "MON-VER", POLL).serialize())
        end, buf = time.time() + 1.6, b""
        while time.time() < end:
            buf += s.read(512)
        ok = buf.count(b"\xb5\x62") >= 1 or len(re.findall(rb"\$G[NPLAB][A-Z]{3},", buf)) >= 2
        if not ok:
            s.close()
            return None
        return s

    def connect(self):
        if self.via_fc:
            # An FC that was unplugged or reset has rebooted out of passthrough: switch it back first.
            if self.fc_auto and IS_WIN and self.port.upper() not in {p.device.upper() for p in list_serial_ports()}:
                new = find_fc_port()
                if new and new.upper() != self.port.upper():
                    self.log(f"flight controller moved {self.port} -> {new}")
                    self.port = new
            self.log(f"waiting for flight controller on {self.port}")
            fc_passthrough(self.port, log=lambda m: self.log(m.replace("[fc] ", "")))
            time.sleep(0.5)
        self.log(f"opening {self.port}")
        bauds = ([self.baud] if self.baud else []) + [b for b in self.BAUDS if b != self.baud]
        for b in bauds:
            self.log(f"probing {self.port} @ {b}")
            s = self._probe(b)
            if s:
                self.ser, cur = s, b
                break
        else:
            raise IOError(f"no u-blox data on {self.port} at any baud - check TX/RX wiring & power")
        self.log(f"found receiver @ {cur}")
        ver = self.poll_version()
        uid = self.poll_uniqid()
        if uid:
            ver["uniqid"] = uid
        pv = ver.get("protver") or 0
        gen9 = pv >= 27
        with self.st.lock:
            self.st.info.update(ver, port=self.port, baud=cur, spectrum=gen9, signals=gen9)
        # optional baud bump (RAM only)
        if self.target_baud and cur != self.target_baud:
            self.log(f"switching receiver to {self.target_baud} baud (RAM only)")
            if gen9:
                self.send(UBXMessage.config_set(1, 0, [("CFG_UART1_BAUDRATE", self.target_baud)]))
            else:
                self.send(UBXMessage("CFG", "CFG-PRT", SET, portID=1, charLen=3, parity=4,
                                     nStopBits=0, baudRate=self.target_baud, inUBX=1, inNMEA=1,
                                     outUBX=1, outNMEA=0 if not self.keep_nmea else 1))
            time.sleep(0.3)
            self.ser.baudrate = self.target_baud
            time.sleep(0.3)
            self.ser.reset_input_buffer()
            with self.st.lock:
                self.st.info["baud"] = self.target_baud
        self.configure(gen9)
        self.log("streaming")
        if self.st.hub:
            self.st.hub.on_identified(self.st)

    def send(self, msg):
        self.write_raw(msg.serialize())
        time.sleep(0.05)

    def write_raw(self, b):
        with self.wlock:
            self.ser.write(b)
            self.ser.flush()

    def poll_version(self):
        self.ser.reset_input_buffer()
        self.send(UBXMessage("MON", "MON-VER", POLL))
        rdr = UBXReader(self.ser, protfilter=UBX_PROTOCOL, quitonerror=0)
        end = time.time() + 3
        while time.time() < end:
            try:
                _, msg = rdr.read()
            except Exception:  # noqa: BLE001
                continue
            if msg and msg.identity == "MON-VER":
                exts = []
                i = 1
                while hasattr(msg, f"extension_{i:02d}"):
                    exts.append(getattr(msg, f"extension_{i:02d}").decode(errors="ignore").strip("\x00 "))
                    i += 1
                info = {"fw": msg.swVersion.decode(errors="ignore").strip("\x00 "),
                        "hw": msg.hwVersion.decode(errors="ignore").strip("\x00 "),
                        "extensions": exts}
                for e in exts:
                    if e.startswith("PROTVER="):
                        info["protver"] = float(e.split("=")[1])
                    if e.startswith("MOD="):
                        info["model"] = e.split("=")[1]
                    if e.startswith("FWVER="):
                        info["fw"] = e.split("=")[1]
                if not info.get("model"):
                    hw = info["hw"]
                    info["model"] = {"00080000": "u-blox M8", "00190000": "u-blox F9",
                                     "000A0000": "u-blox M10"}.get(hw, f"u-blox hw {hw}")
                if not info.get("protver"):
                    info["protver"] = 18.0
                self.log(f"{info['model']} fw {info['fw']} protver {info['protver']}")
                return info
        self.log("no MON-VER reply - assuming M8-class receiver")
        return {"model": "unknown (assumed M8)", "protver": 18.0}

    def poll_uniqid(self):
        """Chip's unique ID (UBX-SEC-UNIQID) - lets us notice a different GPS on the same port."""
        try:
            self.ser.reset_input_buffer()
            self.send(UBXMessage("SEC", "SEC-UNIQID", POLL))
            rdr = UBXReader(self.ser, protfilter=UBX_PROTOCOL, quitonerror=0)
            end = time.time() + 1.5
            while time.time() < end:
                _, msg = rdr.read()
                if msg and msg.identity == "SEC-UNIQID":
                    return f"{msg.uniqueId:010X}"
        except Exception:  # noqa: BLE001
            pass
        return None

    def configure(self, gen9):
        msgs = ["NAV_SAT", "NAV_PVT", "MON_RF", "MON_SPAN", "NAV_SIG"] if gen9 else []
        if gen9:
            big = {"NAV_SAT", "NAV_SIG"}
            cfg = [(f"CFG_MSGOUT_UBX_{m}_UART1", self.decimate if m in big else 1) for m in msgs]
            if not self.keep_nmea:
                cfg.append(("CFG_UART1OUTPROT_NMEA", 0))
            cfg.append(("CFG_UART1OUTPROT_UBX", 1))
            self.send(UBXMessage.config_set(1, 0, cfg))
        else:
            for cls, mid in [(0x01, 0x35), (0x01, 0x07), (0x0A, 0x09)]:  # NAV-SAT, NAV-PVT, MON-HW
                r = self.decimate if (cls, mid) == (0x01, 0x35) else 1
                self.send(UBXMessage("CFG", "CFG-MSG", SET, msgClass=cls, msgID=mid,
                                     rateDDC=0, rateUART1=r, rateUART2=0, rateUSB=1, rateSPI=0))
            if not self.keep_nmea:
                for mid in range(0x00, 0x0E):  # standard NMEA sentences off on UART1
                    self.send(UBXMessage("CFG", "CFG-MSG", SET, msgClass=0xF0, msgID=mid,
                                         rateDDC=0, rateUART1=0, rateUART2=0, rateUSB=0, rateSPI=0))

    def loop(self):
        rdr = UBXReader(self.ser, protfilter=UBX_PROTOCOL, quitonerror=0)
        handlers = {"NAV-SAT": self.st.on_nav_sat, "NAV-SIG": self.st.on_nav_sig,
                    "NAV-PVT": self.st.on_nav_pvt, "MON-RF": self.st.on_mon_rf,
                    "MON-HW": self.st.on_mon_hw, "MON-SPAN": self.st.on_mon_span}
        last_data = time.time()
        while True:
            raw, msg = rdr.read()
            if raw:
                if self.relay:
                    self.relay.send(raw)
                run = self.st.run
                if run is not None and run.raw is not None:
                    try:
                        run.raw.write(raw)
                    except (OSError, ValueError):
                        pass
            if msg is None:
                if time.time() - last_data > 5:
                    raise IOError("no data for 5 s")
                continue
            last_data = time.time()
            ident = msg.identity
            with self.st.lock:
                c = self.st.info["msg_counts"]
                c[ident] = c.get(ident, 0) + 1
            h = handlers.get(ident)
            if h:
                try:
                    h(msg)
                except Exception as e:  # noqa: BLE001
                    print(f"[{self.st.rid}] error handling {ident}: {e}", flush=True)


# --------------------------------------------------------------------------
# Simulator (no hardware) - lets you try the UI and toggle "interference"
# --------------------------------------------------------------------------
class Simulator(threading.Thread):
    def __init__(self, st: State, gen="M10", scale=1.0, hub=None):
        super().__init__(daemon=True)
        self.st, self.gen, self.scale, self.hub = st, gen, scale, hub
        rnd = random.Random(7)
        self.sats = []
        for g, n in [(0, 10), (2, 8), (3, 9), (6, 7), (1, 2)]:
            for k in range(n):
                self.sats.append({"g": g, "sv": rnd.randint(1, 36) if g != 1 else 120 + k * 3,
                                  "az0": rnd.uniform(0, 360), "el0": rnd.uniform(-15, 85),
                                  "rate": rnd.uniform(-0.02, 0.02), "base": rnd.uniform(36, 47),
                                  "used_ok": g != 1 or rnd.random() < .5})
        seen = set()
        self.sats = [s for s in self.sats if not ((s["g"], s["sv"]) in seen or seen.add((s["g"], s["sv"])))]
        self.t0 = time.time()

    def run(self):
        st = self.st
        with st.lock:
            st.info.update(status="streaming (simulated)", model=f"SIMULATED {self.gen}",
                           fw="SIM 1.0", protver=34.1 if self.gen == "M10" else 18.0,
                           port=st.port or "sim", baud=0, uniqid=f"SIM{abs(hash(st.rid)) % 10**7:07d}", spectrum=self.gen == "M10",
                           signals=self.gen == "M10", sim=True)
        rnd = random.Random()
        wE = wN = 0.0  # random-walk position error (m)
        while True:
            t = time.time() - self.t0
            on = self.hub.sim_interference if self.hub else st.sim_interference
            jam = on and self.scale > 0.05
            loss = 7.5 * self.scale if on else 0.0
            sats = {}
            for s in self.sats:
                el = s["el0"] + 10 * math.sin(t * s["rate"] * 0.5)
                if el < 0:
                    continue
                cno = s["base"] - (12 * (1 - el / 90) ** 2) + rnd.gauss(0, 0.8)
                cno -= loss * (0.8 + 0.4 * rnd.random())
                cno = max(0, int(round(cno)))
                if cno < 17:
                    cno = 0 if rnd.random() < .5 else cno
                used = cno >= 22 and el > 8 and s["used_ok"]
                key = f"{GNSS_PREFIX[s['g']]}{s['sv']:02d}"
                sats[key] = {"key": key, "gnss": GNSS[s["g"]], "gnssId": s["g"], "sv": s["sv"],
                             "cno": cno, "elev": int(el), "azim": int(s["az0"] + t * s["rate"]) % 360,
                             "prRes": round(rnd.gauss(0, 2 + loss / 2), 1),
                             "quality": 7 if used else (4 if cno > 20 else (1 if cno == 0 else 3)),
                             "used": used, "health": 1}
            st.update_sats(sats)
            if self.gen == "M10":
                with st.lock:
                    st.sigs = [{"key": k, "gnss": s["gnss"],
                                "sig": SIGNALS[(s["gnssId"], 0)][0], "band": "L1", "cno": s["cno"],
                                "quality": s["quality"], "prUsed": s["used"], "freqId": 0}
                               for k, s in sats.items()]
            used = sum(1 for s in sats.values() if s["used"])
            fix = 3 if used >= 5 else (2 if used >= 3 else 0)
            k = 0.12 + 0.33 * self.scale if on else 0.12
            wE = wE * 0.97 + rnd.gauss(0, k)
            wN = wN * 0.97 + rnd.gauss(0, k)
            st.update_pvt({"fixType": fix, "fix": FIX[fix], "fixOk": fix >= 2, "numSV": used,
                           "lat": 51.5007 + wN / 111320, "lon": -0.1246 + wE / (111320 * math.cos(math.radians(51.5007))),
                           "alt": 32.0, "hAcc": round(1.1 + loss * 0.5 + abs(rnd.gauss(0, .2)), 2),
                           "vAcc": round(1.8 + loss, 2), "pDOP": round(1.2 + loss / 10, 2), "speed": 0,
                           "utc": time.strftime("%H:%M:%S", time.gmtime())})
            jamind = int(max(0, min(255, (14 - 9 * self.scale if on else 14) + rnd.gauss(0, 3))))  # CW ind. falls
            agc = int((5600 - 2400 * self.scale if on else 5600) + rnd.gauss(0, 60))
            st.update_rf([{"block": 0, "jamState": 1, "jamInd": jamind,
                           "agcCnt": agc, "noise": int((88 - 30 * self.scale if on else 88) + rnd.gauss(0, 3)),  # real M10: falls (post-gain)
                           "antStatus": 2, "antPower": 1}])
            if self.gen == "M10":
                span, n, center = 32_000_000, 256, 1_566_500_000   # what a real M10 reports
                pga = int(54 - 16 * self.scale) if on else 54
                f0 = (center - span / 2) / 1e6
                comb_off = rnd.uniform(0, 2.75)
                df = span / n / 1e6
                spec = []
                for i in range(n):
                    f = f0 + i * df
                    # receiver SAW filter passband shape
                    shape = 100 + 4 * math.sin((f - 1550) / 5) - 0.02 * (f - 1568) ** 2
                    v = shape + rnd.gauss(0, 0.8)
                    if jam:  # raised floor + a dithering comb: what a noisy RX/regulator looks like
                        v += 6 + 5 * math.exp(-((f - 1572) / 9) ** 2)
                        ph = (f - comb_off) % 2.75
                        if min(ph, 2.75 - ph) < 0.07 or abs(((f - comb_off - 1.125) % 2.75)) < 0.07:
                            v += rnd.uniform(15, 45) * self.scale
                    v -= PGA_DB_PER_STEP / SPAN_DB_PER_LSB * (54 - pga)   # sim works in raw quarter-dB bytes
                    spec.append(max(0, min(255, int(v))))
                st.update_spectrum(0, spec, span, span // n, center, pga)
            time.sleep(1)


# --------------------------------------------------------------------------
# Hub: many receivers, one run / one set of phase marks across all of them
# --------------------------------------------------------------------------
SCAN_GLOBS = ["/dev/serial0", "/dev/ttyAMA*", "/dev/ttyUSB*", "/dev/ttyACM*"]
IS_WIN = os.name == "nt"


def realp(dev):
    """Canonical name for a port. On Windows COM ports aren't files, so realpath would mangle them."""
    return dev.upper() if IS_WIN else os.path.realpath(dev)


def list_serial_ports():
    """All serial ports, as pyserial sees them (used on Windows, where there are no /dev globs)."""
    try:
        from serial.tools import list_ports
        return list(list_ports.comports())
    except Exception:  # noqa: BLE001
        return []


# USB VID of flight-controller virtual COM ports: STM32, Artery AT32, GigaDevice GD32, some APM boards
FC_VIDS = {0x0483, 0x2E3C, 0x28E9, 0x1209, 0x2DAE, 0x3162}


def find_fc_port():
    """Best guess at a Betaflight/INAV/ArduPilot flight controller's USB port."""
    cands = []
    for p in list_serial_ports():
        txt = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        score = (2 if p.vid in FC_VIDS else 0) + (1 if any(w in txt for w in
                 ("stm32", "betaflight", "inav", "ardupilot", "virtual com", "at32", "flight")) else 0)
        if score:
            cands.append((score, p.device))
    return max(cands)[1] if cands else None


def fc_passthrough(port, log=print):
    """Put a Betaflight/INAV flight controller into GPS passthrough on its USB port.
    Afterwards the port carries the GPS's own byte stream until the FC is power-cycled."""
    s = serial.Serial(port, 115200, timeout=0.3)
    try:
        s.reset_input_buffer()
        end, buf = time.time() + 1.0, b""
        while time.time() < end:                 # already passing through? (GPS data arriving unasked)
            buf += s.read(4096)
        if buf.count(b"\xb5\x62") >= 2 or len(re.findall(rb"\$G[NPLAB][A-Z]{3},", buf)) >= 2:
            log(f"[fc] {port} is already in GPS passthrough")
            return True
        s.write(b"#")                              # enter the CLI
        end, buf = time.time() + 3, b""
        while time.time() < end and b"CLI" not in buf and not buf.rstrip().endswith(b"#"):
            buf += s.read(256)
        if b"CLI" not in buf and b"#" not in buf:
            raise IOError(f"no Betaflight/INAV CLI on {port} (is it the flight controller, and is "
                          "Betaflight Configurator closed?). ArduPilot: set SERIAL_PASS1/2 instead.")
        s.write(b"gpspassthrough\r\n")
        end, buf = time.time() + 3, b""
        while time.time() < end:
            buf += s.read(4096)
            if buf.count(b"\xb5\x62") >= 2 or len(re.findall(rb"\$G[NPLAB][A-Z]{3},", buf)) >= 2:
                log(f"[fc] {port}: GPS passthrough on, GPS data flowing")
                return True
        txt = " ".join(buf.decode(errors="ignore").split())
        if "error" in txt.lower() or "unknown" in txt.lower():
            raise IOError(f"FC refused passthrough: {txt[:200]} - is a GPS set up on one of its UARTs?")
        log(f"[fc] {port}: passthrough requested but no GPS data yet ({txt[:80]}); waiting for the GPS")
        return True
    finally:
        s.close()


def never_scan():
    """Ports we must not touch: the Bluetooth UART on Pi 3/4 (/dev/serial1) and user exclusions."""
    out = set(realp(p) for p in os.environ.get("GPS_SCAN_EXCLUDE", "").split(",") if p)
    if os.path.exists("/dev/serial1"):
        out.add(os.path.realpath("/dev/serial1"))
    return out


def port_key(dev):
    """Stable name for a physical port: /dev/serial/by-path for USB sockets, else the device."""
    if IS_WIN:
        return dev.upper()
    real = os.path.realpath(dev)
    bp = "/dev/serial/by-path"
    if os.path.isdir(bp):
        for n in sorted(os.listdir(bp)):
            if os.path.realpath(os.path.join(bp, n)) == real:
                return n
    return os.path.basename(real)


# Pi hardware UART base address -> (name, GPIO pins). Pi 4 (BCM2711) and Pi 5 (RP1).
UART_ADDR = {"fe201000": ("UART0", "GPIO14/15"), "7e201000": ("UART0", "GPIO14/15"),
             "fe201400": ("UART2", "GPIO0/1"), "fe201600": ("UART3", "GPIO4/5"),
             "fe201800": ("UART4", "GPIO8/9"), "fe201a00": ("UART5", "GPIO12/13"),
             "1f00030000": ("UART0", "GPIO14/15"), "1f00034000": ("UART1", "GPIO0/1"),
             "1f00038000": ("UART2", "GPIO4/5"), "1f0003c000": ("UART3", "GPIO8/9"),
             "1f00040000": ("UART4", "GPIO12/13"), "107d001000": ("debug UART", "3-pin header")}


def port_label(key, dev):
    if IS_WIN:
        for p in list_serial_ports():
            if p.device.upper() == dev.upper():
                return f"{p.device} ({p.description})" if p.description and p.description != p.device else p.device
        return dev
    m = re.search(r"usb-0:([\d.]+):", key)
    if m:
        return f"USB port {m.group(1)}"
    b = os.path.basename(os.path.realpath(dev))
    if b == "ttyS0":
        return "GPIO14/15 (mini UART)"
    try:
        node = os.path.basename(os.path.realpath(f"/sys/class/tty/{b}/device"))   # e.g. fe201600.serial
        addr = node.split(".")[0].lower()
        if addr in UART_ADDR:
            n, pins = UART_ADDR[addr]
            return f"{pins} ({n})"
    except OSError:
        pass
    return b


class Hub:
    def __init__(self, log_dir, keep_nmea=False, set_baud=115200):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.rx: "collections.OrderedDict[str, State]" = collections.OrderedDict()
        self.cfg_path = os.path.join(log_dir, "_receivers", "receivers.json")
        os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
        try:
            with open(self.cfg_path) as f:
                self.cfg = json.load(f)
        except (OSError, ValueError):
            self.cfg = {}
        self.keep_nmea, self.set_baud = keep_nmea, set_baud
        self.ubx_tcp_base, self.ubx_tcp_write = 0, False
        self.local_only = False
        self.run = None              # {"id","label","start","notes"}
        self.sim_interference = False
        self.busy_ports = set()

    # -- receivers ---------------------------------------------------------
    def _save_cfg(self):
        with open(self.cfg_path, "w") as f:
            json.dump(self.cfg, f, indent=2)

    def add(self, dev, key=None, label=None):
        key = key or port_key(dev)
        with self.lock:
            for st in self.rx.values():
                if st.port_key == key:
                    return st
            n = len(self.rx) + 1
            c = self.cfg.setdefault(key, {})
            rid = c.get("rid") or f"gps{n}"
            while rid in self.rx:
                n += 1
                rid = f"gps{n}"
            c["rid"] = rid
            st = State(self.log_dir, rid, dev)
            st.hub, st.port_key = self, key
            st.port_label = label or port_label(key, dev)
            st.meta.update({k: v for k, v in c.get("meta", {}).items()})
            if not c.get("meta", {}).get("name"):
                st.meta["name"] = f"{rid.upper()} · {st.port_label}"
            self.rx[rid] = st
            self._save_cfg()
            return st

    def get(self, rid):
        with self.lock:
            if rid and rid in self.rx:
                return self.rx[rid]
            return next(iter(self.rx.values()), None)

    def set_meta(self, rid, meta):
        st = self.get(rid)
        if not st:
            return None
        allowed = {"name", "role", "product", "antenna", "location", "distance_cm", "notes"}
        with st.lock:
            for k, v in meta.items():
                if k in allowed:
                    st.meta[k] = str(v)[:200]
            if st.meta.get("role") not in ("test", "control", "ignore"):
                st.meta["role"] = "test"
            st.meta.pop("chip_changed", None)
        c = self.cfg.setdefault(st.port_key, {})
        c["meta"] = {k: v for k, v in st.meta.items() if k in allowed}
        c["uniqid"] = st.info.get("uniqid") or c.get("uniqid")
        self._save_cfg()
        st.event("spec", f"Spec updated: {st.meta.get('product') or '-'} · role {st.meta['role']}")
        return st.meta

    def on_identified(self, st):
        """Called once a receiver answers. Warn if a different chip sits on this port than last time."""
        c = self.cfg.setdefault(st.port_key, {})
        uid = st.info.get("uniqid")
        if uid and c.get("uniqid") and c["uniqid"] != uid:
            st.meta["chip_changed"] = True
            st.event("spec", f"A different GPS chip is on {st.port_label} than last time "
                     f"(was {c['uniqid']}, now {uid}) - check this port's spec", "warn")
        if uid:
            c["uniqid"] = uid
        c["last_model"] = st.info.get("model")
        self._save_cfg()

    # -- discovery (listen-only) -------------------------------------------
    @staticmethod
    def listen_for_gps(dev, bauds=(115200, 38400, 9600, 57600, 230400)):
        """Passive: open and LISTEN. Never writes - a flight controller or ELRS module on USB is left alone."""
        for b in bauds:
            try:
                s = serial.Serial()
                s.port, s.baudrate, s.timeout = dev, b, 0.25
                s.dtr = s.rts = False          # don't pulse reset lines (ESP32/Arduino boards reset on DTR/RTS)
                s.open()
                try:
                    s.reset_input_buffer()
                    end, buf = time.time() + 1.3, b""
                    while time.time() < end and len(buf) < 4000:
                        buf += s.read(512)
                finally:
                    s.close()
            except (OSError, serial.SerialException):
                return None
            if buf.count(b"\xb5\x62") >= 2 or len(re.findall(rb"\$G[NPLABQ][A-Z]{3},", buf)) >= 2:
                return b
        return None

    def scan_loop(self, every=8):
        import glob
        while True:
            try:
                seen = set(never_scan())
                groups = [[p.device for p in list_serial_ports()]] if IS_WIN else [sorted(glob.glob(g)) for g in SCAN_GLOBS]
                for devs in groups:
                    for dev in devs:
                        real = realp(dev)
                        if real in seen or real in self.busy_ports:
                            continue
                        seen.add(real)
                        key = port_key(dev)
                        same = [st for st in self.rx.values() if st.port_key == key]
                        if same:   # same physical socket re-enumerated under a new name (e.g. ttyUSB0 -> ttyUSB2)
                            st = same[0]
                            if realp(st.port) != real and "stream" not in (st.info.get("status") or ""):
                                print(f"[scan] {st.rid} moved {st.port} -> {dev}", flush=True)
                                st.port = dev
                                if getattr(st, "rxthread", None):
                                    st.rxthread.port = dev
                                self.busy_ports.add(real)
                            continue
                        b = self.listen_for_gps(dev)
                        if b:
                            print(f"[scan] u-blox heard on {dev} @ {b}", flush=True)
                            self.start_receiver(dev, baud=b)
            except Exception as e:  # noqa: BLE001
                print(f"[scan] {e}", flush=True)
            time.sleep(every)

    def start_receiver(self, dev, baud=None, via_fc=False):
        st = self.add(dev)
        self.busy_ports.add(realp(dev))
        if self.run:
            self._open_run_for(st)
        # Through an FC the GPS<->FC UART baud is fixed by the FC, so never change the GPS's baud there,
        # and thin out the big per-epoch messages so they fit a 57600/115200 link.
        st.rxthread = Receiver(st, dev, baud, None if via_fc else (self.set_baud or None), self.keep_nmea,
                               decimate=5 if via_fc else 1)
        if via_fc:
            st.rxthread.via_fc = True
            st.rxthread.fc_auto = via_fc == "auto"
            with st.lock:
                st.info["via_fc"] = True
        if self.ubx_tcp_base:
            m = re.search(r"(\d+)$", st.rid)
            port = self.ubx_tcp_base + (int(m.group(1)) - 1 if m else len(self.rx) - 1)
            try:
                st.rxthread.relay = UbxRelay(port, st.rid, st.rxthread.write_raw, self.ubx_tcp_write,
                                             local_only=self.local_only)
                st.rxthread.relay.start()
                with st.lock:
                    st.info["ubx_tcp"] = port
                print(f"[{st.rid}] u-center: tcp://<this computer>:{port}", flush=True)
            except OSError as e:
                print(f"[{st.rid}] u-center relay on :{port} unavailable: {e}", flush=True)
        st.rxthread.start()
        return st

    # -- runs ----------------------------------------------------------------
    def active(self):
        return [st for st in self.rx.values() if st.meta.get("role") != "ignore"]

    def _open_run_for(self, st):
        with st.lock:
            st.run = Run(self.log_dir, self.run["label"], self.run["notes"], run_id=self.run["id"], sub=st.rid,
                         start=self.run["start"], meta=st.meta, info=st.info)
            for t, name in self.run["marks"]:
                st.run.marks.append((t, name))
                st.run.phase = name
            st.event("run", f"Run started: {self.run['label']}", "info")
            st.on_phase(st.run.phase)
            st.event("phase", st.run.phase, "marker")

    def start_run(self, label, notes=""):
        with self.lock:
            if self.run:
                self.stop_run()
            rid = time.strftime("%Y%m%d-%H%M%S") + "_" + slug(label)
            self.run = {"id": rid, "label": label, "notes": notes, "start": now(), "marks": []}
            os.makedirs(os.path.join(self.log_dir, rid), exist_ok=True)
            for st in self.active():
                self._open_run_for(st)
            return self.run_status()

    def stop_run(self):
        with self.lock:
            if not self.run:
                return None
            out = {"id": self.run["id"], "label": self.run["label"], "notes": self.run["notes"],
                   "start": self.run["start"], "duration": round(now() - self.run["start"], 1),
                   "multi": True, "phases": [m[1] for m in self.run["marks"]], "receivers": {}}
            ana = {}
            for st in self.rx.values():
                with st.lock:
                    if st.run and st.run.id == self.run["id"]:
                        st.event("run", f"Run stopped: {st.run.label}", "info")
                        sm = st.run.close()
                        ana[st.rid] = (st.meta, json.load(open(os.path.join(st.run.dir, "analysis.json"))))
                        out["receivers"][st.rid] = {"meta": dict(st.meta), "model": st.info.get("model"),
                                                    "port": st.port_label, "summary": sm}
                        st.run = None
            out["compare"] = analysis.compare(ana) if ana else None
            with open(os.path.join(self.log_dir, out["id"], "run.json"), "w") as f:
                json.dump(out, f, indent=2)
            self.run = None
            return out

    def phase(self, name):
        with self.lock:
            if self.run:
                self.run["marks"].append((now(), name))
            for st in self.rx.values():
                with st.lock:
                    if st.run:
                        st.run.mark_phase(name)
                    st.on_phase(name)
                    st.event("phase", name, "marker")

    def marker(self, text):
        for st in self.rx.values():
            st.event("marker", text, "marker")

    def run_status(self):
        if not self.run:
            return None
        ph = self.run["marks"][-1][1] if self.run["marks"] else "Baseline"
        return {"id": self.run["id"], "label": self.run["label"], "elapsed": round(now() - self.run["start"], 1),
                "phase": ph, "receivers": [st.rid for st in self.rx.values() if st.run]}

    def live_compare(self):
        ana = {}
        for st in self.rx.values():
            with st.lock:
                if st.run:
                    ana[st.rid] = (st.meta, st.run.analysis())
        return analysis.compare(ana) if ana else None

    def overview(self, since):
        out = []
        for st in self.rx.values():
            with st.lock:
                h = st.history[-1] if st.history else {}
                out.append({
                    "id": st.rid, "port": st.port_label, "dev": st.port, "meta": st.meta,
                    "info": {k: st.info.get(k) for k in ("status", "model", "fw", "protver", "uniqid", "spectrum", "sim", "baud", "ubx_tcp", "via_fc")},
                    "fix": st.pvt.get("fix"), "fixType": st.pvt.get("fixType"),
                    "now": {k: h.get(k) for k in ("cnoTop", "used", "tracked", "gainCut", "agcDrop", "spikes",
                                                  "runErr", "hAcc", "pga", "jamInd")},
                    "gainAlert": st.gain.get("alert"),
                    "history": [{k: x.get(k) for k in ("t", "cnoTop", "used", "gainCut", "spikes", "runErr", "hAcc")}
                                for x in st.history if x["t"] > since],
                    "inRun": bool(st.run)})
        return out


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------
def make_app(hub: Hub) -> Flask:
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"))

    def rx_state():
        rid = request.args.get("rx") or (request.get_json(silent=True) or {}).get("rx")
        st = hub.get(rid)
        if st is None:
            abort(503, "no receivers yet")
        return st

    @app.get("/")
    def index():
        r = send_from_directory(app.static_folder, "index.html", max_age=0)
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- receivers / overview ----
    @app.get("/api/version")
    def version():
        try:
            import getpass
            user = getpass.getuser()
        except Exception:
            user = "pi"
        return jsonify({"version": GPSSCOPE_VERSION, "ssh": f"{user}@{socket.gethostname()}.local"})

    @app.get("/api/receivers")
    def receivers():
        return jsonify({"receivers": hub.overview(time.time() + 1), "run": hub.run_status(),
                        "sim_interference": hub.sim_interference})

    @app.get("/api/overview")
    def overview():
        return jsonify({"receivers": hub.overview(float(request.args.get("since", 0))), "run": hub.run_status(),
                        "sim_interference": hub.sim_interference, "server_t": now(), "version": GPSSCOPE_VERSION})

    @app.post("/api/receivers/<rid>")
    def set_meta(rid):
        m = hub.set_meta(rid, request.get_json(force=True, silent=True) or {})
        return jsonify({"ok": m is not None, "meta": m})

    # ---- per-receiver live state ----
    @app.get("/api/state")
    def api_state():
        st = hub.get(request.args.get("rx"))
        if st is None:
            return jsonify({"waiting": True, "info": {"status": "no receivers found yet - scanning ports"},
                            "run": hub.run_status(), "receivers": []})
        snap = st.snapshot(float(request.args.get("since", 0)), int(request.args.get("ev", 0)))
        snap["run"] = (dict(st.run.status(), hubPhase=hub.run_status()["phase"]) if st.run else None)
        snap["hubRun"] = hub.run_status()
        snap["sim_interference"] = hub.sim_interference
        snap["version"] = GPSSCOPE_VERSION
        snap["receivers"] = [{"id": r.rid, "name": r.meta.get("name"), "role": r.meta.get("role"),
                              "status": r.info.get("status"), "port": r.port_label} for r in hub.rx.values()]
        return jsonify(snap)

    @app.get("/api/spectrum/history")
    def spec_history():
        st = rx_state()
        with st.lock:
            sp = st.spec
            return jsonify({"f0": sp["f0"] if sp else None, "df": sp["df"] if sp else None,
                            "frames": [{"t": t, "db": d} for t, d in st.spec_hist]})

    # ---- runs (hub-wide) ----
    @app.post("/api/run/start")
    def run_start():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(hub.start_run((body.get("label") or "run").strip(), body.get("notes", "")))

    @app.post("/api/run/stop")
    def run_stop():
        out = hub.stop_run()
        return jsonify(out or {"ok": False})

    @app.post("/api/phase")
    def phase():
        b = request.get_json(force=True, silent=True) or {}
        name = (b.get("name") or "").strip()[:40]
        if not name:
            return jsonify({"ok": False}), 400
        hub.phase(name)
        return jsonify({"ok": True})

    @app.post("/api/marker")
    def marker():
        body = request.get_json(force=True, silent=True) or {}
        hub.marker(body.get("text") or "Marker")
        return jsonify({"ok": True})

    @app.get("/api/run/analysis")
    def run_analysis():
        st = rx_state()
        with st.lock:
            if not st.run:
                return jsonify(None)
            a = st.run.analysis()
            return jsonify(dict(a, label=st.run.label, start=st.run.start, live=True, rx=st.rid, rxName=st.meta.get("name")))

    @app.get("/api/run/compare")
    def run_compare():
        c = hub.live_compare()
        return jsonify(dict(c, label=hub.run["label"], live=True) if c and hub.run else None)

    def run_dirs(rid):
        d = os.path.join(hub.log_dir, os.path.basename(rid))
        if not os.path.isdir(d):
            abort(404)
        return d

    def analyse_dir(d):
        p = os.path.join(d, "analysis.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        tl, sats, pvt, marks, t0, t1, src = analyse_run.load(d)
        a = analysis.analyse(tl, sats, pvt, marks, t0, t1)
        a["phaseSource"] = src
        with open(p, "w") as f:
            json.dump(a, f, indent=2)
        return a

    @app.get("/api/runs/<rid>/analysis")
    def finished_analysis(rid):
        d = run_dirs(rid)
        sub = request.args.get("rx")
        if os.path.exists(os.path.join(d, "run.json")):          # multi-receiver run
            with open(os.path.join(d, "run.json")) as f:
                rj = json.load(f)
            subs = list(rj["receivers"])
            sub = sub if sub in subs else (subs[0] if subs else None)
            if not sub:
                abort(404)
            a = analyse_dir(os.path.join(d, sub))
            a.update(label=rj["label"], start=rj["start"], live=False, rx=sub,
                     rxName=rj["receivers"][sub]["meta"].get("name"))
            return jsonify(a)
        a = analyse_dir(d)                                       # single-receiver run (older)
        sp = os.path.join(d, "summary.json")
        if os.path.exists(sp):
            with open(sp) as f:
                sm = json.load(f)
            a["label"], a["start"] = sm.get("label", rid), sm.get("start")
        a["live"] = False
        return jsonify(a)

    @app.get("/api/runs/<rid>/compare")
    def finished_compare(rid):
        d = run_dirs(rid)
        if not os.path.exists(os.path.join(d, "run.json")):
            return jsonify(None)
        with open(os.path.join(d, "run.json")) as f:
            rj = json.load(f)
        ana = {r: (v["meta"], analyse_dir(os.path.join(d, r))) for r, v in rj["receivers"].items()}
        c = analysis.compare(ana)
        return jsonify(dict(c, label=rj["label"], live=False))

    @app.get("/api/runs")
    def runs():
        out = []
        for d in sorted(os.listdir(hub.log_dir), reverse=True):
            if d.startswith("_"):
                continue
            rp, sp = os.path.join(hub.log_dir, d, "run.json"), os.path.join(hub.log_dir, d, "summary.json")
            if os.path.exists(rp):
                with open(rp) as f:
                    rj = json.load(f)
                rj.pop("compare", None)
                out.append(rj)
            elif os.path.exists(sp):
                with open(sp) as f:
                    s1 = json.load(f)
                out.append({"id": s1["id"], "label": s1["label"], "start": s1["start"], "duration": s1["duration"],
                            "multi": False, "receivers": {"gps1": {"meta": s1.get("meta", {}), "summary": s1}}})
        return jsonify(out)

    @app.get("/api/runs/<rid>.zip")
    def run_zip(rid):
        d = run_dirs(rid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.join(rid, os.path.relpath(full, d)))
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{rid}.zip")

    @app.get("/api/runs/<rid>/share.zip")
    def run_share(rid):
        name, data = share.make_share_zip(run_dirs(rid))
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                         download_name=f"{name}.zip")

    @app.get("/api/continuous.zip")
    def cont_zip():
        d = os.path.join(hub.log_dir, "_continuous")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(d):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    z.write(full, os.path.join("continuous", os.path.relpath(full, d)))
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"gpsscope-continuous-{time.strftime('%Y%m%d')}.zip")

    @app.delete("/api/runs/<rid>")
    def run_del(rid):
        import shutil
        d = run_dirs(rid)
        if not hub.run or hub.run["id"] != rid:
            shutil.rmtree(d)
        return jsonify({"ok": True})

    # ---- per-receiver controls ----
    @app.post("/api/spectrum/baseline")
    def baseline():
        body = request.get_json(force=True, silent=True) or {}
        targets = list(hub.rx.values()) if body.get("all") else [rx_state()]
        for st in targets:
            with st.lock:
                if body.get("clear"):
                    st.spec_baseline = None
                elif st.spec:
                    st.capture_spec_baseline(body.get("label") or "baseline")   # last 30 s
                    st.reset_max("(since baseline)")      # "loudest" restarts: compares after vs before
        return jsonify({"ok": True})

    @app.post("/api/ref")
    def set_ref():
        st = rx_state()
        b = request.get_json(force=True, silent=True) or {}
        if b.get("clear"):
            st.set_ref(None)
            return jsonify({"ok": True})
        if b.get("mode") == "average":
            r = st.avg_position(int(b.get("seconds", 60)))
            if not r:
                return jsonify({"ok": False, "error": "need at least 5 3D fixes in that window"}), 400
        else:
            try:
                r = {"lat": float(b["lat"]), "lon": float(b["lon"]),
                     "alt": float(b["alt"]) if b.get("alt") not in (None, "") else None,
                     "source": b.get("source", "manual")}
            except (KeyError, ValueError):
                return jsonify({"ok": False, "error": "lat/lon required"}), 400
            if not (-90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180):
                return jsonify({"ok": False, "error": "lat/lon out of range"}), 400
            if r["alt"] is None:
                a = st.avg_position(60)
                r["alt"] = a["alt"] if a else None
        st.set_ref(r, b.get("thresh"))
        return jsonify({"ok": True, "ref": r})

    @app.post("/api/ref/thresh")
    def set_thresh():
        st = rx_state()
        b = request.get_json(force=True, silent=True) or {}
        with st.lock:
            st.drift_thresh = max(0.2, float(b.get("thresh", 3)))
            with open(st._ref_path(), "w") as f:
                json.dump({"ref": st.ref, "thresh": st.drift_thresh}, f)
        return jsonify({"ok": True})

    @app.post("/api/spectrum/resetmax")
    def resetmax():
        st = rx_state()
        st.reset_max("(since reset)")
        return jsonify({"ok": True})

    @app.post("/api/sim/interference")
    def sim_int():
        hub.sim_interference = not hub.sim_interference
        hub.marker("Sim: RFI " + ("on" if hub.sim_interference else "off"))
        return jsonify({"on": hub.sim_interference})

    return app


def main():
    if os.name == "nt" and not sys.flags.utf8_mode:
        # Windows defaults to cp1252 for files and the console; logs and messages use Δ, →, · ...
        import subprocess
        raise SystemExit(subprocess.call([sys.executable, "-X", "utf8"] + sys.argv))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", action="append", default=[],
                    help="serial port with a GPS; repeat for several. Probed actively. "
                         "Without --port (or with --scan) all serial ports are scanned listen-only.")
    ap.add_argument("--fc", metavar="PORT|auto",
                    help="flight controller on USB (Betaflight/INAV): switch it to GPS passthrough and monitor its GPS. "
                         "'auto' picks the FC's COM port. Implies --no-scan unless --scan is given")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--scan", action="store_true", help="also scan for receivers when --port is given")
    ap.add_argument("--no-scan", action="store_true", help="never scan, only use --port")
    ap.add_argument("--exclude", action="append", default=[],
                    help="port the scanner must never open (e.g. a flight controller); repeatable")
    ap.add_argument("--baud", type=int, default=0, help="current receiver baud (default: auto-detect)")
    ap.add_argument("--set-baud", type=int, default=115200,
                    help="switch receivers to this baud in RAM (0 = leave as is). Default 115200")
    ap.add_argument("--keep-nmea", action="store_true", help="leave NMEA output on")
    ap.add_argument("--ubx-tcp", type=int, default=2101,
                    help="first TCP port for the u-center relay (gps1=2101, gps2=2102, ...); 0 turns it off")
    ap.add_argument("--ubx-tcp-write", action="store_true",
                    help="let u-center send commands to the GPS through the relay (default: read-only)")
    ap.add_argument("--http", default="0.0.0.0:8080", help="web UI bind address")
    ap.add_argument("--logdir", default=os.path.join(HERE, "runs"))
    ap.add_argument("--pga-db-per-step", type=float, default=PGA_DB_PER_STEP,
                    help="spectrum dB per PGA dB, for gain correction (default %(default)s)")
    ap.add_argument("--sim", nargs="?", const="M10", choices=["M10", "M8"],
                    help="simulate receivers instead of using hardware")
    ap.add_argument("--sim-count", type=int, default=1, help="number of simulated receivers (first is a control)")
    a = ap.parse_args()

    if a.list_ports:
        for p in list_serial_ports():
            print(f"{p.device:12} vid={p.vid and hex(p.vid)} {p.description}")
        return
    globals()["PGA_DB_PER_STEP"] = a.pga_db_per_step
    if a.exclude:
        os.environ["GPS_SCAN_EXCLUDE"] = ",".join(a.exclude + [os.environ.get("GPS_SCAN_EXCLUDE", "")])
    hub = Hub(a.logdir, a.keep_nmea, a.set_baud)
    hub.ubx_tcp_base, hub.ubx_tcp_write = a.ubx_tcp, a.ubx_tcp_write
    hub.local_only = a.http.rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
    if a.sim:
        n = max(1, a.sim_count)
        for i in range(n):
            st = hub.add(f"sim{i + 1}", key=f"sim{i + 1}", label=f"simulated #{i + 1}")
            if n > 1 and not hub.cfg.get(f"sim{i + 1}", {}).get("meta"):
                st.meta.update(role="control" if i == 0 else "test",
                               distance_cm="200" if i == 0 else str(2 + 6 * (i - 1)),
                               product="Simulated M10", name=f"{'Control' if i == 0 else 'Test ' + str(i)} · sim #{i + 1}")
            scale = 0.0 if (n > 1 and i == 0) else (1.0 if n == 1 else max(0.25, 1.0 - 0.3 * (i - 1)))
            Simulator(st, a.sim, scale=scale, hub=hub).start()
    else:
        if serial is None:
            raise SystemExit("pyserial / pyubx2 not installed - run: pip install pyubx2 pyserial flask")
        if a.fc:
            fc = find_fc_port() if a.fc.lower() == "auto" else a.fc
            if not fc:
                raise SystemExit("No flight controller found on USB. Plug it in, close Betaflight Configurator, "
                                 "or name the port: --fc COM5  (python gpsscope.py --list-ports shows them)")
            hub.start_receiver(fc, baud=115200, via_fc="auto" if a.fc.lower() == "auto" else True)
        for dev in a.port:
            hub.start_receiver(dev, baud=a.baud or None)
        if (not (a.port or a.fc) or a.scan) and not a.no_scan and not (IS_WIN and not a.scan):
            threading.Thread(target=hub.scan_loop, daemon=True, name="scan").start()

    host, port = a.http.rsplit(":", 1)
    print(f"[web] GPS Scope {GPSSCOPE_VERSION} on http://{host}:{port}", flush=True)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    make_app(hub).run(host=host, port=int(port), threaded=True)


if __name__ == "__main__":
    main()
