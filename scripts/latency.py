#!/usr/bin/env python3
"""p50 and p95 of POST /analyze over 5 runs. The targets (p50 < 20 s, p95 < 30 s) are suggested, not hard.

Usage: python scripts/latency.py            # against API_URL (default http://127.0.0.1:8000)
If nothing answers /health it starts a throwaway offline server (temp DB, free port) and kills it at exit.
"""
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    import httpx
except ImportError:  # run with the project venv when the shell python lacks deps
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists() and Path(sys.executable).resolve() != venv_py.resolve():
        os.execv(str(venv_py), [str(venv_py), *sys.argv])
    raise

API = os.environ.get("API_URL", "http://127.0.0.1:8000").rstrip("/")
RUNS = int(os.environ.get("RUNS", "5"))
P50_TARGET, P95_TARGET = 20.0, 30.0
SEED = ROOT / "data" / "sample_feedback.json"


def healthy(client, base):
    try:
        r = client.get(f"{base}/health", timeout=2)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_temp_server(client):
    tmp = tempfile.mkdtemp(prefix="latency-")
    port = free_port()
    env = dict(os.environ, DB_PATH=f"{tmp}/feedback.db", LEDGER_PATH=f"{tmp}/ledger.jsonl", LOG_FILE=f"{tmp}/app.log",
               LLM_MODE="offline", OPEN_LOG_WINDOW="0", PORT=str(port), API_URL=f"http://127.0.0.1:{port}")
    env.pop("ANTHROPIC_API_KEY", None)
    out = open(f"{tmp}/uvicorn.out", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        if healthy(client, base):
            return proc, tmp, base
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    proc.kill()
    print(open(f"{tmp}/uvicorn.out").read()[-1500:], file=sys.stderr)
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit("could not start a temporary server")


def pct(sorted_vals, p):
    """Nearest-rank percentile. With 5 runs the p95 is the slowest run."""
    return sorted_vals[max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)]


def main():
    proc = tmp = None
    with httpx.Client(timeout=120) as client:
        base = API
        try:
            if not healthy(client, base):
                print(f"nothing answers {base}/health, starting a temporary offline server")
                proc, tmp, base = start_temp_server(client)
            health = client.get(f"{base}/health").json()
            if health.get("feedback_count", 0) == 0:
                rows = json.loads(SEED.read_text())
                r = client.post(f"{base}/feedback", json=rows)
                if r.status_code != 201:
                    sys.exit(f"seed ingest failed: {r.status_code} {r.text[:200]}")
                print(f"ingested {len(rows)} seed rows")
            health = client.get(f"{base}/health").json()
            print(f"target {base}  mode={health.get('mode')}  feedback_count={health.get('feedback_count')}")

            times, mode = [], health.get("mode")
            for i in range(1, RUNS + 1):
                t0 = time.perf_counter()
                r = client.post(f"{base}/analyze", params={"mode": "all", "stream": "false"})
                dt = time.perf_counter() - t0
                if r.status_code != 200:
                    sys.exit(f"run {i}: POST /analyze returned {r.status_code}: {r.text[:200]}")
                body = r.json()
                mode = body.get("mode", mode)
                times.append(dt)
                print(f"run {i}: {dt:6.2f} s  analyzed={body.get('analyzed')}  themes={body.get('themes') if not isinstance(body.get('themes'), list) else len(body['themes'])}"
                      f"  dropped_unverified={body.get('dropped_unverified')}  server_duration_s={body.get('duration_s')}")
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    s = sorted(times)
    p50, p95 = statistics.median(s), pct(s, 95)
    print(f"\nmode={mode}  runs={len(s)}  min={s[0]:.2f} s  max={s[-1]:.2f} s")
    for label, val, target in (("p50", p50, P50_TARGET), ("p95", p95, P95_TARGET)):
        status = "OK  " if val < target else "WARN"
        print(f"{status} {label} {val:6.2f} s  (target < {target:.0f} s, suggested not hard)")


if __name__ == "__main__":
    main()
