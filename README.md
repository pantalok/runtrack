# runtrack

A local, private dashboard for the running data in your Apple Health export, with an optional
chat panel that hands questions to Claude Code.

- **Overview**: weekly mileage, personal bests (fastest mile / 5K / 10K / half within any run),
  daily calendar, pace trend, heart-rate zones, VO₂ max, resting HR, running-form trends, route map.
- **Insights**: fitness / fatigue / form from heart-rate load, mileage ramp (acute:chronic ratio),
  aerobic efficiency, zone-2 pace, cardiac drift, best-effort progression, race predictor,
  heat impact, and when you run, plus auto-generated findings.
- **Run**: every metric along the run (stacked, overlaid, or per mile) with a synced cursor and map
  marker, mile-by-mile table with grade-adjusted pace, route colored by pace / HR / elevation,
  and comparison with your typical run of similar distance.
- **Compare**: overlay up to 4 runs by distance, time gap vs. the first run, side-by-side stats,
  mile splits, and routes. "Compare same route" finds earlier runs on the same course.
- **Ask Claude**: a side panel that runs `claude -p` in this folder, using whatever account the
  Claude Code CLI is logged into. It knows which tab and run you're looking at and can read the
  generated data files and run `python3` to analyse them. It has no other tools.

Everything runs on your machine. Your health data is never uploaded anywhere. The only network
requests are for map tiles (OpenStreetMap) and the chart and map libraries (cdnjs).

## Setup

1. On your iPhone: **Health → profile picture → Export All Health Data**. Unzip `export.zip`.
2. Copy `build_running_data.py`, `server.py`, and `running.html` into the unzipped
   `apple_health_export` folder (the one containing `export.xml` and `workout-routes/`).
3. Build the data and start the dashboard:

   ```sh
   python3 build_running_data.py   # ~20 s for a 1 GB export; writes data/
   python3 server.py               # opens http://localhost:8765
   ```

Re-run `build_running_data.py` whenever you drop in a fresh export.

Requirements: Python 3.11+ (standard library only) and a modern browser. For the chat panel, install
[Claude Code](https://claude.com/claude-code) and run `claude` once to log in. The dashboard works
without it.

## What the build step produces

- `data/summary.json`: one record per run (distance, duration, HR, power, form metrics, weather,
  HR zones, derived metrics such as efficiency factor, aerobic decoupling, grade-adjusted pace,
  best efforts, and a simplified route) plus daily VO₂ max, resting HR, and HRV trends.
- `data/streams/<run id>.json`: 10-second samples (distance, pace, HR, power, cadence, ground
  contact, stride, vertical oscillation, elevation, lat/lon) and a per-mile table.

Notes:
- Runs that Strava or Stryd re-imported on top of an Apple Watch recording are de-duplicated.
- Mile splits use moving time, so pauses don't distort them.
- GPS distance is scaled to the Watch's fused total.

## Privacy

`.gitignore` is an allow-list: only the source files are tracked, so `export.xml`, routes, ECGs,
and the generated `data/` folder can't be committed by accident. The local server binds to
127.0.0.1, rejects requests whose Host header isn't localhost, and requires a per-run token for
the chat endpoint, so other websites can't trigger it.
