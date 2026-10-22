#!/usr/bin/env python3
"""Approval-gated c1/c5/c10 Pause/Resume measurement with owned-only cleanup."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import sys
import time

from cubesandbox import Sandbox
import suite


def owned_ids():
    return set(json.loads(suite.OWNED.read_text()).get("ids", [])) if suite.OWNED.exists() else set()


def remember(sandbox):
    ids = owned_ids()
    ids.add(sandbox.sandbox_id)
    suite.save(suite.OWNED, {"ids": sorted(ids), "updated_at": suite.stamp()})


def forget(sandbox_id):
    ids = owned_ids()
    ids.discard(sandbox_id)
    suite.save(suite.OWNED, {"ids": sorted(ids), "updated_at": suite.stamp()})


def kill_one(sandbox_id):
    Sandbox.connect(sandbox_id=sandbox_id).kill()
    forget(sandbox_id)


def cleanup_owned(cfg):
    rows = suite.inventory(cfg)
    ids = {suite.sandbox_id(row) for row in rows}
    owned = owned_ids()
    if None in ids or ids - owned:
        raise RuntimeError(f"foreign or unidentified inventory present; count={len(ids - owned)}")
    for sandbox_id in sorted(ids & owned):
        kill_one(sandbox_id)
    remaining = suite.inventory(cfg)
    if remaining:
        raise RuntimeError(f"inventory not empty after owned cleanup: {len(remaining)}")
    for sandbox_id in owned - ids:
        forget(sandbox_id)


def quiet_gate(cfg):
    deadline = time.monotonic() + cfg["quiet_timeout_seconds"]
    quiet_start = None
    history = []
    while time.monotonic() < deadline:
        load1 = os.getloadavg()[0]
        history.append({"time": suite.stamp(), "load1": load1})
        if load1 < cfg["load1_max"]:
            quiet_start = quiet_start or time.monotonic()
            if time.monotonic() - quiet_start >= cfg["quiet_seconds"]:
                return history
        else:
            quiet_start = None
        time.sleep(1)
    raise RuntimeError("load1 did not remain below gate before timeout")


def assert_conditions(cfg, scope):
    current = suite.snapshot("bench", cfg)
    if current["status"] != "ready_for_user_confirmation" or current["scope_sha256"] != scope:
        raise RuntimeError(f"test conditions drifted: {current['blockers']}; stop without retry")
    if current["inventory_total"] != 0:
        raise RuntimeError("inventory not empty before round")
    return current


def functional_smoke(cfg):
    sandbox = None
    try:
        sandbox = Sandbox.create(template=cfg.get("template_id") or cfg["template_alias"])
        remember(sandbox)
        sandbox.run_code("open('/tmp/bench-marker','w').write('disk-ok'); open('/dev/shm/bench-marker','w').write('memory-ok')")
        sandbox.pause()
        sandbox.resume()
        result = sandbox.run_code("print(open('/tmp/bench-marker').read(), open('/dev/shm/bench-marker').read())")
        text = result.logs.stdout[0].strip() if result.logs.stdout else ""
        if text != "disk-ok memory-ok":
            raise RuntimeError(f"disk/RAM state mismatch: {text!r}")
        return {"sandbox_id": sandbox.sandbox_id, "disk_and_memory_state": "verified"}
    finally:
        if sandbox is not None:
            kill_one(sandbox.sandbox_id)


def round_once(cfg, concurrency):
    sandboxes = []
    try:
        for _ in range(concurrency):
            sandbox = Sandbox.create(template=cfg.get("template_id") or cfg["template_alias"])
            sandboxes.append(sandbox)
            remember(sandbox)
        started = time.monotonic()
        if concurrency == 1:
            sandboxes[0].pause()
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(lambda item: item.pause(), sandboxes))
        pause_ms = (time.monotonic() - started) * 1000
        started = time.monotonic()
        if concurrency == 1:
            sandboxes[0].resume()
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(lambda item: item.resume(), sandboxes))
        resume_ms = (time.monotonic() - started) * 1000
        return {"pause_wall_ms": pause_ms, "resume_wall_ms": resume_ms}
    finally:
        failures = []
        with ThreadPoolExecutor(max_workers=max(1, len(sandboxes))) as pool:
            futures = [pool.submit(kill_one, s.sandbox_id) for s in sandboxes]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    failures.append(str(exc))
        if failures:
            raise RuntimeError(f"owned sandbox cleanup failed: {failures}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    cfg["source_dir"] = str(Path(cfg["source_dir"]).resolve())
    approval = json.loads(args.approval.read_text())
    if (approval.get("stage") != "bench" or not approval.get("used_at") or
            approval.get("bench_started_at") or
            approval.get("launch_nonce") != os.environ.get("CUBE_BENCH_LAUNCH_NONCE")):
        raise RuntimeError("bench requires a consumed, user-approved record")
    approval["bench_started_at"] = suite.stamp()
    suite.save(args.approval, approval)
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"status": "running", "review_status": "pending", "started_at": suite.stamp(),
              "approval": str(args.approval), "source_commit": suite.git_identity(Path(cfg["source_dir"]))["commit"],
              "tiers": cfg["tiers"], "warmup_rounds": cfg["warmup_rounds"],
              "formal_rounds": cfg["formal_rounds"], "sampling": False, "retries": False}
    suite.save(run_dir / "run.json", record)
    rounds = []
    suite.save(run_dir / "rounds.json", rounds)
    try:
        cleanup_owned(cfg)
        initial = assert_conditions(cfg, approval["scope_sha256"])
        suite.save(run_dir / "initial-environment.json", initial)
        quiet_gate(cfg)
        smoke = functional_smoke(cfg)
        suite.save(run_dir / "functional-smoke.json", smoke)
        for tier in cfg["tiers"]:
            if tier <= 0:
                raise RuntimeError("tiers must contain positive integers")
            for ordinal in range(cfg["warmup_rounds"] + cfg["formal_rounds"]):
                history = quiet_gate(cfg)
                before = assert_conditions(cfg, approval["scope_sha256"])
                measured = round_once(cfg, tier)
                if suite.inventory(cfg):
                    raise RuntimeError("inventory not empty after round")
                item = {"tier": tier, "ordinal": ordinal + 1,
                        "kind": "warmup" if ordinal < cfg["warmup_rounds"] else "formal",
                        "started_at": before["created_at"], "load_before": before["load_average"],
                        "quiet_history": history, **measured}
                rounds.append(item)
                suite.save(run_dir / "rounds.json", rounds)
                print(json.dumps({k: item[k] for k in ("tier", "ordinal", "kind", "pause_wall_ms", "resume_wall_ms")}), flush=True)
        assert_conditions(cfg, approval["scope_sha256"])
        summary = {}
        for tier in cfg["tiers"]:
            items = [r for r in rounds if r["tier"] == tier and r["kind"] == "formal"]
            summary[str(tier)] = {k: {"mean_ms": statistics.mean(r[k] for r in items),
                                       "values_ms": [r[k] for r in items]}
                                  for k in ("pause_wall_ms", "resume_wall_ms")}
        suite.save(run_dir / "summary-pending.json", {"review_status": "pending", "tiers": summary})
        record["status"] = "complete"
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        raise
    finally:
        record["finished_at"] = suite.stamp()
        try:
            record["inventory_total_after"] = len(suite.inventory(cfg))
        except Exception as exc:
            record["inventory_error_after"] = str(exc)
        suite.save(run_dir / "run.json", record)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
