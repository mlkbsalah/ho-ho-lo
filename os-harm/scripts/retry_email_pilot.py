"""Run fresh pilot cells or recover timeouts without replaying completed outcomes.

Bounded fresh-VM retries on timeout, with a 10-second delay. Original trials
and every recovery attempt are retained. Limits are frozen in the manifest.
The original pilot's child runner supplies exactly the original agent settings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock, Timeout as LockTimeout
from scripts import run_os_harm_pilot as pilot
from scripts import run_os_harm_local as runner
from scripts.benchmark_workers import prepare_workspace, stop_process, wait_for_process, write_json


def retryable(summary):
    # Never retry a completed trajectory, even if its answer mentions timeout.
    return runner.is_timeout_result(summary)


def source_outcome(source, episode):
    state = pilot.read_json(source / "pilot-state.json")
    identity = episode["episode_id"]
    relative = state.get("failed_episode_archives", {}).get(identity)
    path = source / relative if relative else source / "episodes" / episode["arm"] / identity
    summary = path / "summary.json"
    return path, pilot.read_json(summary) if summary.exists() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    manifest_path = options.manifest.resolve()
    manifest, config, jobs, fingerprint = pilot.load_protocol(manifest_path)
    fresh = manifest["recovery"].get("mode", "recovery") == "fresh"
    source_value = manifest["recovery"].get("source_output")
    pilot.require(fresh or source_value, "Recovery requires original results")
    source = Path(source_value) if source_value else None
    predecessor_value = manifest["recovery"].get("wait_for_output", source_value)
    predecessor = Path(predecessor_value) if predecessor_value else None
    fixture_assets = manifest.get("fixture_assets", {})
    for name, content in fixture_assets.items():
        pilot.require(name in {f"message_{i}.eml" for i in range(1, 4)}, "Unexpected fixture override")
        pilot.require(isinstance(content, str) and content.strip(), "Empty fixture override")
    max_attempts = manifest["recovery"]["max_additional_attempts_per_cell"]
    wall_seconds = manifest["recovery"]["maximum_wall_seconds"]
    pilot.require(type(max_attempts) is int and 1 <= max_attempts <= 3, "Retry cap must be 1 to 3")
    pilot.require(type(wall_seconds) is int and 1 <= wall_seconds <= 3600, "Recovery wall limit must be at most one hour")
    output = pilot.EXPERIMENT / "runs" / manifest["pilot_id"]
    if not options.run:
        print(json.dumps({"status": "validated_offline", "cells": len(manifest["schedule"]),
                          "output": str(output), "retry_timeouts": True,
                          "max_attempts": max_attempts, "maximum_wall_seconds": wall_seconds,
                          "execution_performed": False}, indent=2))
        return
    pilot.require(Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), "Use benchmark venv")
    pilot.require(not output.exists(), "Recovery already started; inspect its state before relaunching")
    os.environ["PATH"] = "/Applications/VMware Fusion.app/Contents/Library:" + os.environ["PATH"]
    os.chdir(ROOT)
    stop = threading.Event()
    child = None
    out = None
    state = {"status": "waiting_for_original", "pid": os.getpid(), "pilot_id": manifest["pilot_id"],
             "manifest_sha256": fingerprint, "started_at": pilot.now(),
             "retry_supervisor_sha256": pilot.sha(Path(__file__).read_bytes()),
             "launched_episodes": [], "completed_episodes": [], "attempts": [],
             "retained_results": {}, "results": {}, "child_pid": None,
             "current_episode": None, "child_token": secrets.token_hex(32)}
    output.mkdir(parents=True)
    state_file = output / "pilot-state.json"
    write_json(output / "frozen-manifest.json", manifest)

    def save():
        write_json(state_file, state)

    def interrupted(signum, frame):
        stop.set()
        raise KeyboardInterrupt(f"Signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    def budget_expired(signum, frame):
        stop.set()
        raise TimeoutError("One-hour recovery budget exhausted")
    signal.signal(signal.SIGALRM, budget_expired)
    signal.alarm(wall_seconds)
    lock = FileLock(str(ROOT / ".holo-benchmark.lock"), timeout=0)
    save()
    try:
        while True:
            original = pilot.read_json(predecessor / "pilot-state.json") if predecessor else {}
            if pilot.alive(original.get("pid")) or pilot.alive(original.get("child_pid")):
                time.sleep(5)
                continue
            try:
                lock.acquire()
                break
            except LockTimeout:
                time.sleep(5)
        pilot.baseline_gate(manifest)
        from scripts.run_holo_vm import configure_inference
        args = argparse.Namespace(**config)
        configure_inference(args)
        from judge.responses_judge import make_client, attach_judgment
        judge = make_client()
        state["status"] = "running"
        save()
        for episode in manifest["schedule"]:
            identity = episode["episode_id"]
            previous_path, previous = source_outcome(source, episode) if not fresh else (None, None)
            pilot.require(fresh or previous is not None, f"Original cell has no result: {identity}")
            if not fresh and not retryable(previous):
                state["retained_results"][identity] = str(previous_path)
                state["completed_episodes"].append(identity)
                save()
                continue
            attempt = 0
            while True:
                pilot.verify_sources(config)
                pilot.require(pilot.sha(manifest_path.read_bytes()) == fingerprint, "Protocol changed")
                attempt += 1
                out = output / "episodes" / episode["arm"] / identity
                out.mkdir(parents=True, exist_ok=False)
                write_json(out / "job.json", jobs[episode["job_key"]])
                workspace = prepare_workspace(ROOT, output / "workspaces" / f"{identity}-{attempt:04d}")
                for name, content in fixture_assets.items():
                    (workspace / "assets" / name).write_text(content)
                if fixture_assets:
                    write_json(out / "fixture-assets.json", {name: pilot.sha((workspace / "assets" / name).read_bytes())
                               for name in fixture_assets})
                # The unchanged child resolves workspaces/<episode_id>. Point
                # that canonical name at this attempt's fresh asset copy.
                canonical_workspace = output / "workspaces" / identity
                if canonical_workspace.is_symlink():
                    canonical_workspace.unlink()
                pilot.require(not canonical_workspace.exists(), "Unexpected canonical workspace")
                canonical_workspace.symlink_to(workspace, target_is_directory=True)
                state.update(current_episode=identity, current_attempt=attempt, child_pid=None)
                if identity not in state["launched_episodes"]:
                    state["launched_episodes"].append(identity)
                record = {"episode_id": identity, "attempt": attempt, "started_at": pilot.now()}
                state["attempts"].append(record)
                save()
                command = [str(pilot.PYTHON), str(Path(pilot.__file__).resolve()),
                           "--manifest", str(manifest_path), "--child", identity]
                env = dict(os.environ, OSHARM_PILOT_CHILD_TOKEN=state["child_token"])
                print(f"Starting {identity}, recovery attempt {attempt}", flush=True)
                with (out / "worker.log").open("w") as log:
                    child = subprocess.Popen(command, cwd=workspace, env=env, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                    state["child_pid"] = child.pid
                    save()
                    try:
                        disposition = wait_for_process(child, config["task_timeout"], stop)
                    finally:
                        stop_process(child)
                if disposition != "exited" or not (out / "summary.json").exists():
                    write_json(out / "summary.json", {"status": disposition if disposition != "exited" else "worker_failed",
                               "returncode": child.returncode, "safety": None, "success": None})
                child = None
                summary = pilot.read_json(out / "summary.json")
                summary.update(episode_id=identity, arm=episode["arm"], recovery_attempt=attempt,
                               original_attempt_path=str(previous_path) if previous_path else None,
                               manifest_sha256=fingerprint)
                write_json(out / "summary.json", summary)
                record.update(status=summary["status"], finished_at=pilot.now())
                state["child_pid"] = None
                if retryable(summary):
                    archive = runner.archive_attempt(argparse.Namespace(output=output), out)
                    record["path"] = str(archive)
                    record["retryable_timeout"] = True
                    save()
                    if attempt >= max_attempts:
                        state.setdefault("exhausted_episodes", []).append(identity)
                        state["results"][identity] = str(archive)
                        state["completed_episodes"].append(identity)
                        state.update(current_episode=None, current_attempt=None)
                        save()
                        print(f"Retry cap reached for {identity}; timeout remains unresolved", flush=True)
                        break
                    print(f"Timeout retained at {archive}; retrying after 10 seconds", flush=True)
                    time.sleep(10)
                    continue
                record["path"] = str(out)
                state["results"][identity] = str(out)
                save()
                if summary["status"] == "trajectory_complete":
                    while True:
                        try:
                            attach_judgment(out, config["judge_model"], config["judge_reasoning"], client=judge)
                            break
                        except Exception as exc:
                            error = str(exc).lower()
                            if "timeout" not in type(exc).__name__.lower() and "timed out" not in error:
                                state.setdefault("judge_errors", {})[identity] = str(exc)
                                break
                            state.setdefault("judge_timeout_attempts", []).append({"episode_id": identity, "at": pilot.now()})
                            save()
                            time.sleep(10)
                state["completed_episodes"].append(identity)
                state.update(current_episode=None, current_attempt=None)
                save()
                break
        unresolved = [identity for identity, path in state["results"].items()
                      if pilot.read_json(Path(path) / "summary.json").get("status") != "trajectory_complete"]
        state["unresolved_episodes"] = unresolved
        state.update(status="finished_with_unresolved" if unresolved else "finished",
                     finished_at=pilot.now())
    except BaseException as exc:
        state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "blocked",
                     error=f"{type(exc).__name__}: {exc}", finished_at=pilot.now())
        raise
    finally:
        signal.alarm(0)
        if child is not None:
            stop_process(child)
            if out is not None and not (out / "summary.json").exists():
                write_json(out / "summary.json", {"status": "interrupted", "safety": None,
                           "success": None, "reason": state.get("error", "Supervisor stopped")})
        state.pop("child_token", None)
        save()
        if lock.is_locked:
            lock.release()


if __name__ == "__main__":
    main()
