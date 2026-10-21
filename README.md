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
- **Activity**: daily steps split into walking and running (deduplicated across Watch and iPhones),
  step goal and streaks, 12-month calendar, Apple Watch ring closure, exercise minutes, steps by hour
  and weekday, flights climbed, year-by-year history, and walking workouts with routes.
- **Health**: a recovery check (HRV, resting HR, breathing rate, blood oxygen, walking HR against
  your own 60-day normal range), sleep duration, stages and schedule, heart-fitness trends, mobility
  (walking speed, step length, asymmetry, double support, steadiness), daylight and noise exposure.
- **Plan**: draw a route on the map that follows roads and paths, with live distance, mile
  markers, climb and descent, an elevation profile, hill-colored route, and a time estimate from your
  own recent pace adjusted for hills. Turn it into an out-and-back for a target distance or loop it
  back to the start; see past runs on the same route; save routes, export GPX for your watch, or
  import one. "Plan from this route" on any run loads its GPS track. Routing uses the free OSRM
  service at routing.openstreetmap.de and elevation comes from Open-Meteo; only the points you place
  are sent to them.
- **Ask Claude**: a side panel that runs `claude -p` in this folder, using whatever account the
  Claude Code CLI is logged into. It knows which tab and run you're looking at and can read the
  generated data files and run `python3` to analyse them. It has no other tools.

Everything runs on your machine. Your health data is never uploaded anywhere. The only network
requests are for map tiles (OpenStreetMap), the chart and map libraries (cdnjs), and, when you use the
Plan tab, routing and elevation lookups for the points you place.

## Setup

The code and your data live in separate folders. This repo holds only code. Your export,
and everything generated from it, stays in a private folder that is never part of a git repo.

1. On your iPhone: **Health → profile picture → Export All Health Data**. Unzip `export.zip`
   anywhere private, e.g. `~/health/apple_health_export` (the folder containing `export.xml`
   and `workout-routes/`).
2. Clone this repo somewhere else:

   ```sh
   git clone https://github.com/pantalok/runtrack.git ~/code/runtrack
   cd ~/code/runtrack
   ```

3. Build the data and start the dashboard, pointing both at your export folder:

   ```sh
   python3 build_running_data.py ~/health/apple_health_export   # ~20 s for a 1 GB export
   python3 server.py ~/health/apple_health_export               # opens http://localhost:8765
   ```

   To avoid typing the path each time, set it once in your shell profile:

   ```sh
   export RUNTRACK_EXPORT=~/health/apple_health_export
   python3 build_running_data.py && python3 server.py
   ```

### Several people

Build each person's export separately, then pass all the folders to one server, labelled with
`Name=path`. A switcher appears next to the title, and each person gets their own chat history.
Claude only sees the selected person's folder.

```sh
python3 build_running_data.py ~/health/me
python3 build_running_data.py ~/health/partner
python3 server.py Me=~/health/me Partner=~/health/partner
```

Profiles without any runs open on the Activity tab and hide the running pages.

The build writes `data/` inside the export folder, not inside this repo. Re-run it whenever you
replace the export with a fresh one.

Requirements: Python 3.11+ (standard library only) and a modern browser. For the chat panel, install
[Claude Code](https://claude.com/claude-code) and run `claude` once to log in. The dashboard works
without it. The chat runs inside your export folder, so Claude only sees that folder's files.

## What the build step produces

- `data/summary.json`: one record per run (distance, duration, HR, power, form metrics, weather,
  HR zones, derived metrics such as efficiency factor, aerobic decoupling, grade-adjusted pace,
  best efforts, and a simplified route) plus daily VO₂ max, resting HR, and HRV trends.
- `data/daily.json`: per-day steps, distance, flights and activity rings, hourly steps, sleep nights,
  daily health and mobility vitals, and walking workouts.
- `data/streams/<run id>.json`: 10-second samples (distance, pace, HR, power, cadence, ground
  contact, stride, vertical oscillation, elevation, lat/lon) and a per-mile table.

Notes:
- Runs that Strava or Stryd re-imported on top of an Apple Watch recording are de-duplicated.
- Mile splits use moving time, so pauses don't distort them.
- GPS distance is scaled to the Watch's fused total.

## Privacy

Your data never enters the repo: it lives in a separate folder that you pass on the command line.
As a second safeguard, `.gitignore` is an allow-list that tracks only the source files, so even an
export copied into the repo by mistake can't be committed. The local server binds to
127.0.0.1, rejects requests whose Host header isn't localhost, and requires a per-run token for
the chat endpoint, so other websites can't trigger it.
