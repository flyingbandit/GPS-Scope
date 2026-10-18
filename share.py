#!/usr/bin/env python3
"""
Make a shareable copy of a GPS Scope run with no position or personal data.

  python3 share.py runs/<run folder>            -> runs/<run folder>_shareable.zip
  (or the Share button in the run table)

Removed or changed:
  - latitude, longitude, altitude: removed. Position is kept only as an offset in
    metres from that receiver's own average position during the Baseline phase.
  - clock times: every time becomes seconds from the start of the run; no date or time of day.
  - satellite identities: each satellite gets a random per-share name (GPS-07, BeiDou-03...),
    azimuth is removed and elevation is rounded to 10 degrees. Sky geometry plus time could
    otherwise give the location away.
  - the drift reference point, the chip serial number (SEC-UNIQID), and the spec fields
    "location" and "notes".
Kept: GPS model, firmware, product/antenna/distance/role, all RF, C/N0, spectrum,
gain, accuracy and analysis figures.
"""
import csv
import io
import json
import math
import os
import random
import re
import sys
import zipfile

ABS_T = 1.0e9                       # anything above this is a Unix timestamp
DROP_KEYS = {"lat", "lon", "alt", "ref", "uniqid", "location", "notes", "iso", "trail",
             "bearing", "refSrc"}
CONST = {"G": "GPS", "S": "SBAS", "E": "Galileo", "B": "BeiDou", "C": "BeiDou", "R": "GLONASS",
         "J": "QZSS", "Q": "QZSS", "I": "NavIC", "N": "NavIC"}
SAT_RE = re.compile(r"\b([GSEBCRJQIN])(\d{2,3})\b")    # u-blox style ids: G05, S136 (not E1, C/N0)
ELEV_RE = re.compile(r"(\d+(?:\.\d+)?)\s*°(\s*elev)")
COORD_RE = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")
AZ_RE = re.compile(r",?\s*\baz(?:imuth)?\s*[:=]?\s*\d+(?:\.\d+)?\s*°?", re.I)
BEARING_RE = re.compile(r",?\s*\(?\s*bearing\s*\d+°?\s*,?", re.I)


class Scrubber:
    def __init__(self, t0, fixes=None):
        self.t0 = t0
        self.fixes = fixes or {}
        self.names = {}
        self.pools = {}

    def sat(self, sid):
        """Random but consistent name for a satellite within this share."""
        if not sid:
            return sid
        if sid not in self.names:
            c = CONST.get(sid[0], "SV")
            pool = self.pools.get(c)
            if not pool:
                base = 100 * (len(self.names) // 99 + 1) if c in self.pools else 1
                pool = self.pools[c] = random.sample(range(base, base + 99), 99)
            self.names[sid] = f"{c}-{pool.pop():02d}"
        return self.names[sid]

    def t(self, v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return v
        return round(x - self.t0, 2) if x > ABS_T else v

    def text(self, s):
        if not isinstance(s, str):
            return s
        s = COORD_RE.sub("[position removed]", s)
        s = AZ_RE.sub("", s)
        s = BEARING_RE.sub(" (", s).replace("( (", "(").replace(" ()", "")
        s = SAT_RE.sub(lambda m: self.sat(m.group(0)), s)
        s = ELEV_RE.sub(lambda m: f"{int(round(float(m.group(1)) / 10.0) * 10)}°{m.group(2)}", s)
        return s

    def json(self, o, key=""):
        if isinstance(o, dict):
            return {k: self.json(v, k) for k, v in o.items() if k not in DROP_KEYS}
        if isinstance(o, list):
            return [self.json(v, key) for v in o]
        if isinstance(o, (int, float)) and not isinstance(o, bool) and o > ABS_T:
            return round(o - self.t0, 2)
        if isinstance(o, str):
            return self.text(o)
        return o


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _read(path):
    with open(path, newline="", errors="replace") as f:
        return list(csv.reader(line.replace("\0", "") for line in f))


def _baseline_window(events_rows, t_start):
    """(start, end) of the first Baseline phase from events.csv; first 60 s if none."""
    ph = [(float(r[0]), r[3]) for r in events_rows[1:] if len(r) >= 4 and r[1] == "phase" and _f(r[0])]
    b0 = b1 = None
    for t, name in ph:
        if name.lower().startswith("base"):
            if b0 is None:
                b0 = t
        elif b0 is not None:
            b1 = t
            break
    if b0 is None:
        b0 = t_start
    return b0, (b1 if b1 is not None else b0 + 60)


def _csv_bytes(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


def scrub_receiver(d, sc):
    """Return {filename: bytes} for one receiver folder."""
    out = {}
    ev = _read(os.path.join(d, "events.csv")) if os.path.exists(os.path.join(d, "events.csv")) else [[]]
    pv_path = os.path.join(d, "pvt.csv")
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        if fn.endswith(".json"):
            try:
                with open(p, errors="replace") as f:
                    txt = f.read().replace("\0", "")
                out[fn] = json.dumps(sc.json(json.loads(txt)) if txt.strip() else {}, indent=1).encode()
            except ValueError:
                pass
            continue
        if not fn.endswith(".csv"):
            continue
        rows = _read(p)
        if not rows:
            continue
        hdr, body = rows[0], [r for r in rows[1:] if r and len(r) == len(rows[0])]
        ix = {h: i for i, h in enumerate(hdr)}
        if fn == "pvt.csv":
            fixes = [r for r in body if _f(r[ix["lat"]]) is not None]
            b0, b1 = _baseline_window(ev, _f(fixes[0][0]) if fixes else 0)
            base = [r for r in fixes if b0 <= float(r[0]) < b1 and r[ix["fixType"]] in ("3", "4")] or fixes[:50]
            if base:
                la0 = sum(float(r[ix["lat"]]) for r in base) / len(base)
                lo0 = sum(float(r[ix["lon"]]) for r in base) / len(base)
                alts = [float(r[ix["alt"]]) for r in base if _f(r[ix["alt"]]) is not None]
                al0 = sum(alts) / len(alts) if alts else None
            keep = ["t", "fixType", "numSV", "hAcc", "vAcc", "pDOP"]
            new = [keep + ["dE_m", "dN_m", "dU_m", "err_m"]]
            for r in body:
                row = [sc.t(r[0])] + [r[ix[k]] for k in keep[1:] if k in ix]
                la, lo, al = _f(r[ix["lat"]]), _f(r[ix["lon"]]), _f(r[ix["alt"]])
                if base and la is not None and r[ix["fixType"]] in ("2", "3", "4"):
                    dn = (la - la0) * 111_320.0
                    de = (lo - lo0) * 111_320.0 * math.cos(math.radians(la0))
                    du = (al - al0) if (al is not None and al0 is not None) else ""
                    row += [round(de, 2), round(dn, 2), round(du, 2) if du != "" else "", round(math.hypot(de, dn), 2)]
                else:
                    row += ["", "", "", ""]
                new.append(row)
            out[fn] = _csv_bytes(new)
            continue
        if fn == "sats.csv":
            keep = [h for h in hdr if h not in ("azim",)]
            new = [keep]
            for r in body:
                if "cno" in ix and (_f(r[ix["cno"]]) or 0) <= 0:
                    continue        # listed but not received: says which satellites exist overhead, nothing about RF
                row = []
                for h in keep:
                    v = r[ix[h]]
                    if h == "t":
                        v = sc.t(v)
                    elif h == "sat":
                        v = sc.sat(v)
                    elif h == "elev" and _f(v) is not None:
                        v = int(round(float(v) / 10.0) * 10)
                    row.append(v)
                new.append(row)
            out[fn] = _csv_bytes(new)
            continue
        if fn == "events.csv":
            new = [hdr]
            for r in body:
                if r[ix["kind"]] in ("ref", "drift"):     # drift = distance from the map reference point
                    continue
                row = [sc.t(r[0])] + [sc.text(v) for v in r[1:]]
                if r[ix["kind"]] in ("marker", "phase"):
                    row[ix["msg"]] = tidy(row[ix["msg"]], sc.fixes)
                new.append(row)
            out[fn] = _csv_bytes(new)
            continue
        # timeline / rf / spectrum and anything else: drop position-ish columns, relative time
        drop = {"posErr", "iso", "lat", "lon", "alt", "bearing", "dE", "dN", "dU"}
        keep = [h for h in hdr if h not in drop]
        new = [keep]
        for r in body:
            new.append([sc.t(r[ix[h]]) if h in ("t", "ts") else sc.text(r[ix[h]]) for h in keep])
        out[fn] = _csv_bytes(new)
    return out


def _run_start(run_dir):
    for p in (os.path.join(run_dir, "run.json"), os.path.join(run_dir, "summary.json")):
        try:
            with open(p) as f:
                v = json.load(f).get("start")
            if v:
                return float(v)
        except (OSError, ValueError, TypeError):
            pass
    ts = []
    for root, _, files in os.walk(run_dir):
        if "timeline.csv" in files:
            rows = _read(os.path.join(root, "timeline.csv"))
            if len(rows) > 1 and _f(rows[1][0]):
                ts.append(float(rows[1][0]))
    return min(ts) if ts else 0.0


def _label(run_dir):
    base = os.path.basename(os.path.normpath(run_dir))
    m = re.match(r"\d{8}-\d{6}_(.*)", base)
    return (m.group(1) if m else base) or "run"


def tidy(msg, fixes=None):
    """Tidy an operator note for reading: RX/TX in capitals, X-band, first letter capital."""
    for a, b in (fixes or {}).items():
        msg = msg.replace(a, b)
    msg = re.sub(r"\b(rx|tx)\b", lambda m: m.group(1).upper(), msg, flags=re.I)
    msg = re.sub(r"\bx[ -]?band\b", "X-band", msg, flags=re.I)
    msg = re.sub(r"\b868\b(?! ?MHz)", "868 MHz", msg)
    return msg[:1].upper() + msg[1:]


def notes_text(files, label, fixes=None):
    """Plain-text run notes for Notepad (CRLF line endings)."""
    first = sorted({k.split("/")[0] for k in files if k.endswith("receiver.json")} or [""])[0]
    ev = files.get(f"{first}/events.csv" if first else "events.csv", b"").decode(errors="replace")
    m = re.search(r"Run started: ([^\r\n\"]+)", ev)
    title = m.group(1).strip() if m else label.replace("-", " ")
    L = [f"GPS Scope test run: {title}", "=" * 60, "",
         "Shareable copy. Times are seconds from the start of the run.", ""]
    rxs = sorted({k.split("/")[0] for k in files if k.endswith("receiver.json")})
    if rxs:
        L += ["RECEIVERS", "-" * 9]
        for r in rxs:
            try:
                d = json.loads(files[f"{r}/receiver.json"])
            except ValueError:
                continue
            m, i = d.get("meta", {}), d.get("info", {})
            bits = [m.get("product") or i.get("model"), i.get("fw") and f"fw {i['fw']}",
                    m.get("antenna") and f"antenna {m['antenna']}", m.get("distance_cm") and f"{m['distance_cm']} from RX",
                    m.get("role") and f"role: {m['role']}"]
            L.append(f"  {r}: " + ", ".join(b for b in bits if b))
        L.append("")
    first = rxs[0] if rxs else ""
    ev_key = f"{first}/events.csv" if first else "events.csv"
    if ev_key in files:
        rows = list(csv.reader(io.StringIO(files[ev_key].decode())))[1:]
        marks = [(float(r[0]), r[1], r[3]) for r in rows if len(r) >= 4 and r[1] in ("phase", "marker")]
        if marks:
            L += ["YOUR MARKERS", "-" * 12]
            last = None
            for t, kind, msg in marks:
                if kind == "phase" and msg == last:
                    continue
                last = msg if kind == "phase" else last
                L.append(f"  {max(t, 0):7.1f} s   {'[phase] ' if kind == 'phase' else ''}{tidy(msg, fixes)}")
            L.append("")
    for r in rxs:
        try:
            a = json.loads(files.get(f"{r}/analysis.json", b"{}") or b"{}")
        except ValueError:
            a = {}
        if not a.get("phases"):
            continue
        try:
            prod = json.loads(files[f"{r}/receiver.json"]).get("meta", {}).get("product") or r
        except (KeyError, ValueError):
            prod = r
        L += [f"RESULTS: {prod} ({r})", "-" * 60,
              "  Medians; change from the baseline in brackets. Error = metres from the baseline average."]
        if a.get("phaseSource"):
            L.append("  Phases: " + str(a["phaseSource"]))
        L.append("")
        L.append(f"  {'Phase':26}{'Length':>7}{'PGA dB':>12}{'C/N0 top-6':>15}{'Sats used':>12}{'Error m':>17}")
        for p in a["phases"]:
            m, dl = p.get("metrics", {}), p.get("delta", {})
            def cell(k, dec=1):
                v = (m.get(k) or {}).get("med")
                if v is None:
                    return "-"
                d = dl.get(k)
                return f"{v:.{dec}f}" + (f" ({d:+.{dec}f})" if d is not None else "")
            L.append(f"  {tidy(p['name'], fixes)[:26]:26}{p.get('dur', 0):>6.0f}s{cell('pga', 0):>12}{cell('cnoTop'):>15}"
                     f"{cell('used', 0):>12}{cell('posErr', 1):>17}")
        L.append("")
    L += ["WHAT WAS REMOVED FROM THIS COPY", "-" * 31,
          "  - Latitude, longitude and altitude. Position is kept only as metres from each",
          "    receiver's own average position during the Baseline phase.",
          "  - Dates and clock times. Every time is seconds from the start of the run.",
          "  - Satellite identities. Each satellite has a random name for this copy, azimuth",
          "    is removed, elevation is rounded to 10 degrees, and satellites that were listed",
          "    but not received are left out.",
          "  - The map reference point and its drift alerts, the chip serial number, and the",
          "    spec fields 'location' and 'notes'.",
          "", "Kept: GPS model, firmware, antenna, distance and role, and every RF, C/N0,",
          "spectrum, gain, accuracy and analysis figure. analyse_run.py works on this copy.", ""]
    return "\r\n".join(L)


def make_share_zip(run_dir, fixes=None):
    """Bytes of a zip holding the scrubbed run."""
    sc = Scrubber(_run_start(run_dir), fixes)
    name = _label(run_dir) + "_shareable"
    files = {}
    subs = [s for s in sorted(os.listdir(run_dir)) if os.path.isdir(os.path.join(run_dir, s))]
    if subs:
        for s in subs:
            for fn, b in scrub_receiver(os.path.join(run_dir, s), sc).items():
                files[f"{s}/{fn}"] = b
        for fn in sorted(os.listdir(run_dir)):
            p = os.path.join(run_dir, fn)
            if os.path.isfile(p) and fn.endswith(".json"):
                try:
                    with open(p) as f:
                        txt = f.read().replace("\0", "")
                    d = sc.json(json.loads(txt)) if txt.strip() else {}
                except ValueError:
                    continue
                if isinstance(d, dict):
                    d["id"] = name
                files[fn] = json.dumps(d, indent=1).encode()
    else:
        files.update(scrub_receiver(run_dir, sc))
    files["NOTES.txt"] = notes_text(files, _label(run_dir), fixes).encode("utf-8-sig")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn, b in files.items():
            zi = zipfile.ZipInfo(f"{name}/{fn}", date_time=(1980, 1, 1, 0, 0, 0))   # no export date/time
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b)
    return name, buf.getvalue()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    run = sys.argv[1].rstrip("/\\")
    fixes = dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a)   # optional typo fixes: "vhange=change"
    name, data = make_share_zip(run, fixes)
    out = os.path.join(os.path.dirname(os.path.abspath(run)), name + ".zip")
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
