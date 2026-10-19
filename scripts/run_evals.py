#!/usr/bin/env python3
"""Run the 30 eval cases in tests/evals/cases.json against the real API and CLI.

Starts the API on a temp database and port in offline mode (like scripts/smoke.sh), runs every case, prints one PASS or
FAIL line per case and a total. Recall is asserted, precision never is. Nothing under app/ is touched.

    .venv/bin/python scripts/run_evals.py              # all 30
    .venv/bin/python scripts/run_evals.py --only H1,E8 # a few
    .venv/bin/python scripts/run_evals.py --list       # ids and titles
Exit code is 1 when any case fails (use --soft to always exit 0).
"""
import argparse
import contextlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
try:
    import httpx  # noqa: F401
except ImportError:  # started with a bare python3, re-run under the project venv
    if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), __file__, *sys.argv[1:]])
    raise

sys.path.insert(0, str(ROOT / "tests" / "evals"))
import harness  # noqa: E402
from harness import Case, Ctx, FakeAnthropic  # noqa: E402

HANDLER_MODULES = ("happy", "edge_a", "edge_b", "chat")


def load_handlers() -> dict:
    out = {}
    for name in HANDLER_MODULES:
        try:
            out.update(importlib.import_module(name).HANDLERS)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    return out


def run_case(raw: dict, ctx: Ctx, handlers: dict):
    c, setup = Case(raw, ctx), raw.get("setup", {})
    fn = handlers.get(raw["id"])
    if fn is None:
        return False, "no handler written for this case"
    if setup.get("server", "baseline") == "baseline":
        return fn(c, ctx.baseline)
    fake, env, launcher, url = None, dict(setup.get("env") or {}), False, None
    if setup.get("fake_anthropic"):
        p = raw.get("params", {})
        fake = FakeAnthropic(setup["fake_anthropic"], p.get("target"), p.get("fake_quote"), p.get("fake_deadline"))
        env.update(ANTHROPIC_API_KEY=p.get("fake_key", "eval-fake-key-not-real"), LLM_MODE="auto")
        launcher, url = True, fake.url
    try:
        with ctx.fresh(seed=setup.get("seed", False), analyze=setup.get("analyze", True), env=env,
                       launcher=launcher, fake_url=url) as srv:
            srv.fake = fake
            return fn(c, srv)
    finally:
        if fake:
            fake.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma separated case ids, for example H1,E8")
    ap.add_argument("--list", action="store_true", help="print the case ids and titles, then exit")
    ap.add_argument("--keep", action="store_true", help="keep the temp databases and logs")
    ap.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    ap.add_argument("--soft", action="store_true", help="exit 0 even when a case fails")
    ap.add_argument("--verbose", action="store_true", help="print a traceback when a case crashes")
    args = ap.parse_args()

    cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text())["cases"]
    if args.list:
        for c in cases:
            print(f"{c['id']:<4} {c['kind']:<6} {c['title']}  |  {c['pass_when']}")
        return 0
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"].upper() in want]
        if not cases:
            print(f"no case matches {args.only}")
            return 2

    handlers, tmp, ctx, results, t_all = load_handlers(), Path(tempfile.mkdtemp(prefix="evals-")), None, [], time.perf_counter()
    ctx = Ctx(tmp)
    print(f"evals: {len(cases)} cases, offline mode, temp dir {tmp}", flush=True)
    try:
        for raw in cases:
            t0 = time.perf_counter()
            try:
                ok, detail = run_case(raw, ctx, handlers)
            except Exception as exc:  # a crashed case is a failed case
                ok, detail = False, f"crashed {type(exc).__name__}: {str(exc)[:160]}"
                if args.verbose:
                    traceback.print_exc()
            dt = time.perf_counter() - t0
            results.append({"id": raw["id"], "kind": raw["kind"], "title": raw["title"], "pass": bool(ok), "detail": detail, "seconds": round(dt, 2)})
            shown = detail if ok else detail[:300]
            print(f"{'PASS' if ok else 'FAIL'}  {raw['id']:<4} {dt:5.1f}s  {raw['title']}  ({shown[:200] if ok else shown})", flush=True)
    finally:
        ctx.close()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    def tally(kind=None):
        rs = [r for r in results if kind in (None, r["kind"])]
        return sum(r["pass"] for r in rs), len(rs)

    (p, n), (hp, hn), (ep, en), (cp, cn) = tally(), tally("happy"), tally("edge"), tally("chat")
    print(f"\nEVALS {p}/{n} passed  (happy {hp}/{hn}, edge {ep}/{en}, chat {cp}/{cn})  in {time.perf_counter() - t_all:.0f}s")
    failed = [r["id"] for r in results if not r["pass"]]
    if failed:
        print("FAILED " + ", ".join(failed))
    if args.json:
        Path(args.json).write_text(json.dumps({"passed": p, "total": n, "results": results}, indent=2) + "\n")
    return 0 if args.soft or not failed else 1


if __name__ == "__main__":
    sys.exit(main())
