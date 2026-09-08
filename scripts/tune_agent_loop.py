#!/usr/bin/env python3
"""
Pętla dostrajania agenta (lokalnie, bez Della):
  1) corpus pytest
  2) live firm_discovery eval
  3) zapis raportu
  4) powtórz N razy

Użycie:
  python scripts/tune_agent_loop.py --rounds 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--skip-live", action="store_true")
    args = ap.parse_args()

    # ensure full TIZ sources for live eval
    run(["bash", "scripts/ensure_tiz_sources.sh"], timeout=60)

    history: list[dict] = []
    for i in range(1, args.rounds + 1):
        t0 = time.time()
        pytest = run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_firm_discovery.py",
                "tests/test_firm_discovery_corpus.py",
                "-q",
                "--tb=line",
            ],
            timeout=120,
        )
        live: dict = {}
        if not args.skip_live:
            ev = run([sys.executable, "scripts/eval_firm_discovery_iter.py"], timeout=120)
            try:
                live = json.loads(ev.stdout[ev.stdout.find("{") :])
            except Exception:
                live = {"raw": ev.stdout[-2000:], "stderr": ev.stderr[-1000:], "code": ev.returncode}
            run([sys.executable, "scripts/write_firm_discovery_report.py"], timeout=120)

        row = {
            "round": i,
            "pytest_ok": pytest.returncode == 0,
            "pytest_tail": (pytest.stdout + pytest.stderr)[-800:],
            "live_n": live.get("n"),
            "live_noise_rate": live.get("noise_rate"),
            "live_gold_fails": live.get("gold_fails"),
            "live_unknown": live.get("unknown"),
            "elapsed_s": round(time.time() - t0, 2),
        }
        history.append(row)
        print(json.dumps({k: row[k] for k in row if k != "pytest_tail"}, ensure_ascii=False))
        if not row["pytest_ok"]:
            print(row["pytest_tail"])
            break
        if live.get("gold_fails"):
            print("gold_fails", live["gold_fails"])
            break

    out = ROOT / "reports" / "tune_loop_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "rounds": history,
        "ok": bool(history) and all(r["pytest_ok"] for r in history),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
