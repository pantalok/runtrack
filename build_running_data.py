#!/usr/bin/env python3
"""Extract running data from an Apple Health export.

Usage: python3 build_running_data.py [export_dir]
export_dir is the unzipped apple_health_export folder (defaults to $RUNTRACK_EXPORT,
then the current directory). Re-run whenever you drop a fresh export into that folder.

Writes, inside export_dir (so your data stays next to the private export, not in this repo):
  data/summary.json          every run's summary + daily VO2max / resting HR / HRV trends
  data/streams/<run id>.json 10-second time series and a per-mile table for each run
"""
import bisect
import json
import math
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RUNTRACK_EXPORT") or ".").expanduser().resolve()
EXPORT = ROOT / "export.xml"
OUT = ROOT / "data"

RUN = "HKWorkoutActivityTypeRunning"
Q = "HKQuantityTypeIdentifier"
STAT_KEYS = {
    f"{Q}DistanceWalkingRunning": ("dist", "sum"),
    f"{Q}ActiveEnergyBurned": ("kcal", "sum"),
    f"{Q}StepCount": ("steps", "sum"),
    f"{Q}HeartRate": ("hr", "average"),
    f"{Q}RunningPower": ("power", "average"),
    f"{Q}RunningGroundContactTime": ("gct", "average"),
    f"{Q}RunningStrideLength": ("stride", "average"),
    f"{Q}RunningVerticalOscillation": ("vo", "average"),
}
DAILY_RECORDS = {
    f"{Q}VO2Max": "vo2",
    f"{Q}RestingHeartRate": "rhr",
    f"{Q}HeartRateVariabilitySDNN": "hrv",
    f"{Q}HeartRateRecoveryOneMinute": "hrr",
}
# Per-sample records used to build in-run streams (Watch only, to avoid iPhone double counts).
STREAM_RECORDS = {
    f"{Q}HeartRate": "hr",
    f"{Q}RunningPower": "power",
    f"{Q}RunningSpeed": "speed",
    f"{Q}RunningGroundContactTime": "gct",
    f"{Q}RunningStrideLength": "stride",
    f"{Q}RunningVerticalOscillation": "vo",
    f"{Q}StepCount": "steps",
}
MI = 1609.344
STEP = 10  # seconds per stream sample
BEST_EFFORTS = {"1mi": 1.0, "5k": 3.10686, "10k": 6.21371, "half": 13.1094}


def ts(s):
    return datetime.fromisoformat(s.replace(" -", "-").replace(" +", "+")).timestamp()


def num(s):
    try:
        return float(s.split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def rnd(v, n=1):
    return None if v is None else round(v, n)


def parse_workout(el):
    start = datetime.fromisoformat(el.get("startDate").replace(" -", "-").replace(" +", "+"))
    w = {
        "start": start.isoformat(),
        "dur": round(float(el.get("duration")) * (60 if el.get("durationUnit") == "min" else 1)),
        "src": el.get("sourceName"),
        "_t0": start.timestamp(),
        "_t1": ts(el.get("endDate")),
    }
    for st in el.iter("WorkoutStatistics"):
        key = STAT_KEYS.get(st.get("type"))
        if not key:
            continue
        v = num(st.get(key[1]))
        if v is None:
            continue
        if key[0] == "dist" and st.get("unit") == "km":
            v /= 1.609344
        w[key[0]] = round(v, 3)
        if key[0] == "hr" and st.get("maximum"):
            w["hrMax"] = round(float(st.get("maximum")))
    for md in el.iter("MetadataEntry"):
        k, v = md.get("key"), md.get("value")
        if k == "HKIndoorWorkout":
            w["indoor"] = v == "1"
        elif k == "HKWeatherTemperature":
            w["tempF"] = round(num(v), 1)
        elif k == "HKWeatherHumidity":
            w["humid"] = round(num(v) / 100)  # Apple stores % x100
        elif k == "HKElevationAscended":
            w["elevFt"] = round(num(v) / 30.48)  # value is in cm
    zones, bounds = [], []
    for zg in el.iter("WorkoutZoneGroup"):
        if zg.get("type", "").endswith("HeartRate"):
            for z in zg.iter("WorkoutZone"):
                zones.append(round(float(z.get("duration")) * 60))
                bounds.append([num(z.get("minimum")), num(z.get("maximum"))])
    if zones:
        w["zones"] = zones
        w["_zoneBounds"] = bounds
    pauses = []
    for ev in el.iter("WorkoutEvent"):
        if ev.get("type") in ("HKWorkoutEventTypePause", "HKWorkoutEventTypeMotionPaused"):
            pauses.append([ts(ev.get("date")), None])
        elif ev.get("type") in ("HKWorkoutEventTypeResume", "HKWorkoutEventTypeMotionResumed") and pauses and pauses[-1][1] is None:
            pauses[-1][1] = ts(ev.get("date"))
    w["_pauses"] = [p for p in pauses if p[1]]
    ref = el.find("WorkoutRoute/FileReference")
    if ref is not None:
        w["_gpx"] = ref.get("path").lstrip("/")
    return w


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def read_gpx(path):
    """Return list of (t, lat, lon, ele_m, cum_dist_m)."""
    ns = "{http://www.topografix.com/GPX/1/1}"
    pts, dist = [], 0.0
    for _, el in ET.iterparse(path):
        if el.tag == ns + "trkpt":
            lat, lon = float(el.get("lat")), float(el.get("lon"))
            ele = el.findtext(ns + "ele")
            t = datetime.fromisoformat(el.findtext(ns + "time").replace("Z", "+00:00")).timestamp()
            if pts:
                dist += haversine(pts[-1][1:3], (lat, lon))
            pts.append((t, lat, lon, float(ele) if ele else None, dist))
            el.clear()
    return pts


def interp(xs, ys, x):
    """Linear interpolation of ys at x over sorted xs (clamped)."""
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    if x1 == x0:
        return ys[i]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def fill_gaps(vals, max_gap=3):
    """Linearly fill runs of None up to max_gap samples long."""
    out, n, i = list(vals), len(vals), 0
    while i < n:
        if out[i] is None:
            j = i
            while j < n and out[j] is None:
                j += 1
            if 0 < i and j < n and j - i <= max_gap:
                for k in range(i, j):
                    out[k] = out[i - 1] + (out[j] - out[i - 1]) * (k - i + 1) / (j - i + 1)
            i = j
        else:
            i += 1
    return out


def smooth(vals, w=2):
    out = []
    for i in range(len(vals)):
        win = [v for v in vals[max(0, i - w): i + w + 1] if v is not None]
        out.append(sum(win) / len(win) if win and vals[i] is not None else None)
    return out


def minetti(g):
    """Metabolic cost of running at grade g relative to flat (Minetti et al. 2002)."""
    g = max(-0.3, min(0.3, g))
    return (155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6) / 3.6


def records_in(recs, t0, t1):
    starts = recs["_starts"]
    i = bisect.bisect_left(starts, t0 - 5)
    j = bisect.bisect_right(starts, t1 + 5)
    return recs["rows"][i:j]


def build_stream(run, recs, gpx):
    t0, t1 = run["_t0"], run["_t1"]
    n = int((t1 - t0) // STEP) + 1
    if n < 6:
        return None
    bins = defaultdict(lambda: [[] for _ in range(n)])
    step_bins = [[0.0, 0.0] for _ in range(n)]  # steps, seconds
    for key, rs in recs.items():
        for a, b, v in records_in(rs, t0, t1):
            i = int(((a + b) / 2 - t0) // STEP)
            if 0 <= i < n:
                if key == "steps":
                    step_bins[i][0] += v
                    step_bins[i][1] += max(b - a, 1)
                else:
                    bins[key][i].append(v)
    paused = [False] * n
    for a, b in run["_pauses"]:
        for i in range(max(0, int((a - t0) // STEP)), min(n, int((b - t0) // STEP) + 1)):
            paused[i] = True

    s = {"t": [i * STEP for i in range(n)]}
    for key in ("hr", "power", "gct", "stride", "vo", "speed"):
        s[key] = fill_gaps([mean(b) for b in bins[key]]) if key in bins else [None] * n
    s["cad"] = smooth(fill_gaps([sb[0] / sb[1] * 60 if sb[1] >= 3 else None for sb in step_bins]), 1)

    # Distance: GPS track when we have one, else integrate the Watch's speed samples.
    if gpx and len(gpx) > 10:
        gt, gd = [p[0] for p in gpx], [p[4] / MI for p in gpx]
        d = [interp(gt, gd, t0 + t) for t in s["t"]]
        ele = [p[3] for p in gpx]
        if all(e is not None for e in ele):
            s["ele"] = smooth([interp(gt, ele, t0 + t) / 0.3048 for t in s["t"]], 3)
        s["lat"] = [round(interp(gt, [p[1] for p in gpx], t0 + t), 5) for t in s["t"]]
        s["lon"] = [round(interp(gt, [p[2] for p in gpx], t0 + t), 5) for t in s["t"]]
    elif any(v is not None for v in s["speed"]):
        d, acc = [], 0.0
        for v in s["speed"]:
            acc += (v or 0) * STEP / 3600
            d.append(acc)
    else:
        return None
    # The Watch's total (GPS + accelerometer fused) is more accurate than raw GPS; scale to match.
    if run.get("dist") and d[-1] > 0 and 0.85 < run["dist"] / d[-1] < 1.15:
        f = run["dist"] / d[-1]
        d = [x * f for x in d]
    s["d"] = d
    # Moving time: skip pauses and standing still, so splits reflect actual running.
    mt = [0]
    for i in range(1, n):
        moving = d[i] - d[i - 1] > 0.0015 and not paused[i]
        mt.append(mt[-1] + (STEP if moving else 0))
    s["mt"] = mt

    pace = []
    for i in range(n):
        a, b = max(0, i - 3), min(n - 1, i + 3)
        dd = d[b] - d[a]
        p = (s["t"][b] - s["t"][a]) / dd if dd > 0.005 else None
        pace.append(p if p and 200 < p < 1500 and not paused[i] else None)
    s["pace"] = pace

    # Grade-adjusted (flat-equivalent) distance per sample.
    eq = [0.0]
    for i in range(1, n):
        dd = d[i] - d[i - 1]
        g = 0.0
        if "ele" in s and dd > 0.002 and s["ele"][i] is not None and s["ele"][i - 1] is not None:
            g = (s["ele"][i] - s["ele"][i - 1]) / (dd * 5280)
        eq.append(eq[-1] + dd * minetti(g))

    miles, best = per_mile(s, eq, paused), best_efforts(mt, d)
    del s["speed"]
    out = {}
    for k, v in s.items():
        if k in ("lat", "lon"):
            out[k] = v
        elif k in ("d", "ele"):
            out[k] = [rnd(x, 3 if k == "d" else 1) for x in v]
        else:
            out[k] = [rnd(x, 2 if k == "stride" else 1) for x in v]
    return out, miles, best


def per_mile(s, eq, paused):
    d, t, n = s["d"], s["mt"], len(s["t"])
    rows, start, k = [], 0, 1
    while start < n - 1:
        end = start
        while end < n - 1 and d[end] < k:
            end += 1
        seg = range(start, end + 1)
        dist = d[end] - d[start]
        if dist < 0.1:
            break
        secs = t[end] - t[start]
        up = down = 0.0
        if "ele" in s:
            for i in range(start + 1, end + 1):
                de = (s["ele"][i] or 0) - (s["ele"][i - 1] or 0)
                up, down = (up + de, down) if de > 0 else (up, down - de)
        eqd = eq[end] - eq[start]
        hrs = [s["hr"][i] for i in seg if s["hr"][i] is not None]
        rows.append({
            "mile": k, "dist": rnd(dist, 2), "time": secs, "pace": rnd(secs / dist, 0),
            "gap": rnd(secs / eqd, 0) if eqd > 0 else None,
            "hr": rnd(mean(hrs), 0), "hrMax": rnd(max(hrs), 0) if hrs else None,
            "up": rnd(up, 0) if "ele" in s else None, "down": rnd(down, 0) if "ele" in s else None,
            "power": rnd(mean([s["power"][i] for i in seg]), 0),
            "cad": rnd(mean([s["cad"][i] for i in seg]), 0),
            "gct": rnd(mean([s["gct"][i] for i in seg]), 0),
            "stride": rnd(mean([s["stride"][i] for i in seg]), 2),
            "vo": rnd(mean([s["vo"][i] for i in seg]), 1),
        })
        start, k = end, k + 1
    return rows


def best_efforts(t, d):
    best = {}
    for name, target in BEST_EFFORTS.items():
        if d[-1] < target:
            continue
        j, top = 0, None
        for i in range(len(d)):
            while j < len(d) and d[j] - d[i] < target:
                j += 1
            if j >= len(d):
                break
            # Interpolate the exact moment the target distance is reached.
            over = (d[j] - d[i] - target) / max(d[j] - d[j - 1], 1e-9)
            secs = t[j] - t[i] - over * (t[j] - t[j - 1])
            if top is None or secs < top:
                top = secs
        if top and top / target > 240:  # ignore GPS glitches faster than 4:00/mi
            best[name] = round(top)
    return best


def derive(run, stream, miles):
    """Run-level analysis metrics from the stream."""
    hr, pace, t, d = stream["hr"], stream["pace"], stream["t"], stream["d"]
    if run.get("dist") and run.get("hr"):
        # Efficiency factor: metres per minute per heartbeat-per-minute.
        run["ef"] = round(run["dist"] * MI / (run["dur"] / 60) / run["hr"], 3)
    if run["dur"] >= 1800:
        half = len(t) // 2
        ef = []
        for a, b in ((0, half), (half, len(t) - 1)):
            h = mean(hr[a:b])
            if h and d[b] > d[a]:
                ef.append((d[b] - d[a]) / ((t[b] - t[a]) / 60) / h)
        if len(ef) == 2:
            run["decoupling"] = round((ef[0] - ef[1]) / ef[0] * 100, 1)
    full = [m for m in miles if m["dist"] >= 0.95]
    run["splits"] = [m["time"] for m in full]
    if len(full) >= 2:
        h = len(full) // 2
        a, b = mean([m["pace"] for m in full[:h]]), mean([m["pace"] for m in full[-h:]])
        run["splitDelta"] = round(b - a)  # negative = negative split
        ps = [m["pace"] for m in full]
        mu = mean(ps)
        run["paceCV"] = round((sum((p - mu) ** 2 for p in ps) / len(ps)) ** 0.5 / mu * 100, 1)
    gaps = [m for m in miles if m.get("gap")]
    if gaps:
        run["gap"] = round(sum(m["time"] for m in gaps) / sum(m["time"] / m["gap"] for m in gaps))
    if run.get("zones"):
        run["trimp"] = round(sum(sec / 60 * (i + 1) for i, sec in enumerate(run["zones"])))
        lo, hi = run["_zoneBounds"][1]
        z2 = [p for p, h in zip(pace, hr) if p and h and (lo or 0) <= h < (hi or 999)]
        if len(z2) >= 18:  # at least 3 minutes in zone 2
            run["z2pace"] = round(sorted(z2)[len(z2) // 2])


def main():
    if not EXPORT.exists():
        raise SystemExit(f"No export.xml in {ROOT}.\nPass your apple_health_export folder: "
                         f"python3 build_running_data.py ~/path/to/apple_health_export (or set RUNTRACK_EXPORT).")
    runs, daily = [], defaultdict(lambda: defaultdict(list))
    recs = {k: [] for k in STREAM_RECORDS.values()}
    context = ET.iterparse(EXPORT, events=("start", "end"))
    _, root = next(context)
    for event, el in context:
        if event != "end":
            continue
        tag = el.tag
        if tag == "Workout":
            if el.get("workoutActivityType") == RUN:
                runs.append(parse_workout(el))
            root.clear()
        elif tag == "Record":
            typ = el.get("type")
            key = DAILY_RECORDS.get(typ)
            if key:
                v = num(el.get("value"))
                if v is not None:
                    daily[key][el.get("startDate")[:10]].append(v)
            skey = STREAM_RECORDS.get(typ)
            if skey and "Watch" in (el.get("sourceName") or ""):
                v = num(el.get("value"))
                if v is not None:
                    recs[skey].append((ts(el.get("startDate")), ts(el.get("endDate")), v))
            root.clear()
    for key, rows in recs.items():
        rows.sort()
        recs[key] = {"rows": rows, "_starts": [r[0] for r in rows]}

    # Strava/Stryd often re-import a run the Watch already recorded; keep the Watch copy.
    runs.sort(key=lambda r: r["_t0"])
    watch = [r for r in runs if "Watch" in (r["src"] or "")]
    kept = [r for r in runs if r in watch or not any(w["_t0"] < r["_t1"] and r["_t0"] < w["_t1"] for w in watch)]
    dropped = len(runs) - len(kept)
    kept = [r for r in kept if r.get("dist", 0) >= 0.25]  # drop accidental 2-minute "runs"

    if (OUT / "streams").exists():
        shutil.rmtree(OUT / "streams")
    (OUT / "streams").mkdir(parents=True)
    ids, n_streams = set(), 0
    for r in kept:
        rid = datetime.fromisoformat(r["start"]).strftime("%Y%m%d-%H%M")
        while rid in ids:
            rid += "b"
        ids.add(rid)
        r["id"] = rid
        if r.get("steps") and r["dur"]:
            r["cadence"] = round(r["steps"] / (r["dur"] / 60))
        gpx_path = ROOT / r.pop("_gpx", "") if r.get("_gpx") else None
        gpx = read_gpx(gpx_path) if gpx_path and gpx_path.exists() else None
        if gpx and len(gpx) > 1:
            step = max(1, len(gpx) // 200)
            r["route"] = [[round(p[1], 5), round(p[2], 5)] for p in gpx[::step]]
        built = build_stream(r, recs, gpx)
        if built:
            stream, miles, best = built
            derive(r, stream, miles)
            if best:
                r["best"] = best
            r["hasStream"] = True
            (OUT / "streams" / f"{rid}.json").write_text(
                json.dumps({"id": rid, **stream, "miles": miles}, separators=(",", ":")))
            n_streams += 1
        for k in ("_t0", "_t1", "_pauses", "_zoneBounds"):
            r.pop(k, None)

    trends = {k: sorted([d, round(sum(v) / len(v), 1)] for d, v in days.items())
              for k, days in daily.items()}
    (OUT / "summary.json").write_text(json.dumps(
        {"generated": datetime.now().isoformat(timespec="seconds"), "runs": kept, "trends": trends},
        separators=(",", ":")))
    size = sum(f.stat().st_size for f in OUT.rglob("*.json")) // 1024
    print(f"{len(kept)} runs ({dropped} duplicate imports dropped), {sum('route' in r for r in kept)} with routes, "
          f"{n_streams} with streams -> {OUT}/ ({size} KB)")


if __name__ == "__main__":
    main()
