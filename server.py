#!/usr/bin/env python3
"""Serve the running dashboard and bridge its chat panel to Claude Code.

Usage: python3 server.py [export_dir ...] [--port 8765] [--no-open]

Each export_dir is an apple_health_export folder you ran build_running_data.py on
(defaults to $RUNTRACK_EXPORT, then the current directory). Pass several to switch between
people in the page; label them with Name=path, e.g.
    python3 server.py Me=~/health/mine Partner=~/health/theirs
The dashboard page is served from this repo; data/*.json is served from the selected folder.

The chat runs `claude -p` (the Claude Code CLI) inside the selected folder, so it uses whatever
account `claude` is logged into (your Claude subscription), not an API key.
Claude can read the files in data/ and run python3 to analyse them; nothing else.
"""
import argparse
import json
import mimetypes
from urllib.parse import parse_qs, urlsplit
import os
import secrets
import shutil
import subprocess
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE = Path(__file__).parent.resolve()   # where running.html lives
WORKSPACES = {}                            # profile name -> private export folder, set in main()
TOKEN = secrets.token_urlsafe(24)
MODELS = {"opus", "sonnet", "haiku", "fable"}
STATIC = {"running.html"}

SYSTEM_PROMPT = f"""You are a running coach, health and activity analyst embedded in a personal Apple Health dashboard.
Today is {date.today().isoformat()}. The user is chatting with you from a side panel next to the dashboard.

Data (all in the current directory; NEVER read export.xml or export_cda.xml, they are ~1 GB):
- data/summary.json: {{"generated", "runs": [...], "trends": {{"vo2"|"rhr"|"hrv"|"hrr": [[YYYY-MM-DD, value], ...]}}}}
  Each run: id, start (ISO local), dur (s), dist (mi), hr/hrMax (bpm), kcal, power (W), cadence (spm),
  stride (m), gct (ground contact ms), vo (vertical oscillation cm), elevFt, tempF, humid (%), indoor,
  zones (seconds in HR zones 1-5), trimp (zone-weighted load), ef (efficiency factor: m/min per bpm),
  decoupling (% aerobic decoupling between halves; >5% = notable cardiac drift), splits (full-mile times s),
  splitDelta (2nd-half minus 1st-half pace s/mi; negative = negative split), paceCV (% split variability),
  gap (grade-adjusted pace s/mi), z2pace (median pace in HR zone 2, s/mi),
  best ({{"1mi","5k","10k","half"}}: fastest continuous effort in seconds), route ([lat, lon] polyline), hasStream.
- data/streams/<run id>.json: 10-second samples as parallel arrays: t (s from start), d (cumulative mi),
  pace (s/mi), hr, power, cad, gct, stride, vo, ele (ft), lat, lon (null = no sample); plus "miles":
  per-mile rows {{mile, dist, time, pace, gap, hr, hrMax, up, down, power, cad, gct, stride, vo}}.

- data/daily.json: whole-life activity and health, not just runs:
  "days": parallel arrays by index: date, steps (deduplicated across Watch/iPhones), dist (walking+running mi),
  flights, runSteps/runDist (from run workouts, so walking = total - run), move/moveGoal (active kcal),
  exercise/exerciseGoal (min), stand/standGoal (hours; move, exercise and stand all 0 = Watch not worn),
  hours (24 hourly step counts per day).
  "sleep": [{{night (wake-up date), start, end, inBed, asleep, core, deep, rem, awake (minutes)}}]; stages only on
  nights the Watch tracked sleep, otherwise just inBed from the iPhone.
  "vitals": {{key: [[YYYY-MM-DD, daily value], ...]}} for vo2, rhr, hrv (SDNN ms), hrr (1-min HR recovery, bpm),
  walkHr, resp (breaths/min), spo2 (%), wristTemp (°F), walkSpeed (mph), stepLen (in), asym (%), dblSupport (%),
  steadiness (%), sixMin (m), daylight (min/day), headphoneDb, envDb.
  "walks": walking/hiking workouts {{id, start, dur, dist, hr, hrMax, kcal, elevFt, tempF, humid, route}}.

How to work:
- Compute with python3 (json module) rather than guessing; the JSON files are large, so never Read them whole.
  Always run analysis as ONE inline command: python3 -c "..." using relative paths like data/daily.json.
  Do not create files, use /tmp, or chain commands with && or pipes; those are blocked and the call will fail.
- Pace is minutes:seconds per mile; distances in miles; temperatures in °F.
- Each user message starts with <gui_context> describing what they are looking at (tab, date range,
  selected run, runs being compared). "This run" / "these runs" refer to that context.
- Answer like a knowledgeable coach: lead with the finding, back it with specific numbers, keep it tight.
  Use short markdown (bullets, small tables). No preamble about which files you read.
- You cannot change the dashboard; if the user asks for a new chart, describe what the data shows instead.
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RunningLog/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        # Blocks DNS-rebinding: only answer requests addressed to this machine.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        url = urlsplit(self.path)
        path = url.path.lstrip("/") or "running.html"
        if path == "api/session":
            return self._send(200, {"token": TOKEN, "claude": bool(shutil.which("claude")), "profiles": list(WORKSPACES)})
        ws = workspace(parse_qs(url.query).get("profile", [None])[0])
        target = (CODE / path).resolve() if path in STATIC else (ws / path).resolve()
        allowed = path in STATIC or (target.is_relative_to(ws / "data") and target.suffix == ".json")
        if not allowed or not target.is_file():
            return self._send(404, {"error": "not found"})
        self._send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def do_POST(self):
        if not self._host_ok() or self.path != "/api/chat":
            return self._send(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("X-Chat-Token", ""), TOKEN):
            return self._send(403, {"error": "bad token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            message = str(body["message"])[:20000]
        except (ValueError, KeyError):
            return self._send(400, {"error": "bad request"})
        claude = shutil.which("claude")
        if not claude:
            return self._send(503, {"error": "The `claude` CLI was not found on PATH. Install Claude Code and run `claude` once to log in."})

        context = json.dumps(body.get("context") or {}, separators=(",", ":"))[:8000]
        cmd = [
            claude, "-p", f"<gui_context>{context}</gui_context>\n\n{message}",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "Read,Grep,Glob,Bash",
            "--allowedTools", "Read Grep Glob Bash(python3 *)",
            "--permission-mode", "dontAsk",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        if body.get("model") in MODELS:
            cmd += ["--model", body["model"]]
        session = str(body.get("sessionId") or "")
        if session and all(c.isalnum() or c == "-" for c in session):
            cmd += ["--resume", session]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        proc = subprocess.Popen(cmd, cwd=workspace(body.get("profile")), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        stderr = []
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()
        try:
            for line in proc.stdout:
                for event in translate(line):
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
            proc.wait()
            if proc.returncode:
                err = "".join(stderr).strip()[-600:] or f"claude exited with code {proc.returncode}"
                self.wfile.write(f"data: {json.dumps({'type': 'error', 'text': err})}\n\n".encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # the user pressed Stop or closed the tab
        finally:
            if proc.poll() is None:
                proc.kill()


def workspace(name):
    """The export folder for a profile name; unknown or missing names get the first one."""
    return WORKSPACES.get(name) or next(iter(WORKSPACES.values()))


def translate(line):
    """Turn one line of `claude -p --output-format stream-json` into UI events."""
    try:
        e = json.loads(line)
    except ValueError:
        return []
    kind = e.get("type")
    if kind == "system" and e.get("subtype") == "init":
        return [{"type": "session", "sessionId": e.get("session_id"), "model": e.get("model")}]
    if kind == "stream_event":
        ev = e.get("event", {})
        if ev.get("type") == "message_start":
            return [{"type": "turn"}]
        delta = ev.get("delta", {})
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return [{"type": "text", "text": delta["text"]}]
        return []
    if kind == "assistant":
        out = []
        for block in e.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                inp = block.get("input", {})
                detail = inp.get("description") or inp.get("file_path") or inp.get("pattern") or inp.get("command", "")
                out.append({"type": "tool", "name": block.get("name"), "detail": str(detail)[:160]})
        return out
    if kind == "result":
        return [{"type": "done", "sessionId": e.get("session_id"), "isError": e.get("is_error"),
                 "text": e.get("result") if e.get("is_error") else None}]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dirs", nargs="*", metavar="[Name=]export_dir",
                    help="apple_health_export folder(s) (default: $RUNTRACK_EXPORT, then current directory)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    for spec in args.export_dirs or [os.environ.get("RUNTRACK_EXPORT") or "."]:
        name, sep, folder = spec.partition("=")
        if not sep:
            name, folder = "", spec
        path = Path(folder).expanduser().resolve()
        name = name.strip() or path.name
        if not (path / "data" / "summary.json").exists():
            raise SystemExit(f"No data/summary.json in {path} - run "
                             f"`python3 build_running_data.py {folder}` first, or pass the right folder.")
        if name in WORKSPACES:
            raise SystemExit(f"Two folders are both named {name!r}; label them with Name=path.")
        WORKSPACES[name] = path
        print(f"Profile {name!r}: {path}")
    url = f"http://localhost:{args.port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Running Log at {url}  (Ctrl-C to stop)")
    if not shutil.which("claude"):
        print("Note: `claude` CLI not found; the dashboard works but the chat panel will be disabled.")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
