"""Separate, bounded OS-Harm prompt pilot. Default: offline validation only.

Run with os-harm/.venv/bin/python. --run is an explicit execution request; it
requires the original follow-up to have ended and holds its benchmark FileLock
throughout. Explicit --resume skips every previously reserved episode forever.
The baseline runner, prompt module, judge and baseline results are never edited.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
BASELINE = ROOT / "results/os-harm-hcompany/full-run"
EXPERIMENT = PROJECT / "outputs/osharm-follow-up/experiment"
DEFAULT_MANIFEST = PROJECT / "outputs/osharm-follow-up/research-design.json"
PYTHON = ROOT / ".venv/bin/python"
ARMS = ("baseline", "intervention")
TERMINAL = {"trajectory_complete", "error", "timeout", "interrupted", "worker_failed",
            "unsupported_setup", "invalid_fixture"}
sys.path.insert(0, str(ROOT))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_name(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value) and value not in (".", "..")


def verify_sources(config):
    for name, expected in config["source_sha256"].items():
        path = (ROOT / name).resolve()
        require(path.is_relative_to(ROOT), "Source path escapes repository")
        require(sha(path.read_bytes()) == expected, "Original source changed: " + name)


def inspect_agent(config):
    # Imports only define the adapter and tools; no client, VM, or model calls.
    from mm_agents import holo_local_agent as adapter
    from scripts.run_holo_vm import TOOLS
    require(adapter.BENCHMARK_SYSTEM == config["system_prompt"], "Baseline system prompt changed")
    require(TOOLS == config["tools"], "Tools changed")
    for saved, attribute in (("temperature", "temperature"), ("top_p", "top_p"),
                             ("history_images", "max_trajectory_length")):
        require(getattr(adapter.HoloLocalAgent, attribute) == config[saved], "Adapter setting changed: " + saved)
    require(adapter.HoloLocalAgent.observation_type == config["observation_type"] == "screenshot", "Observation mode changed")
    require(adapter.HoloLocalAgent.action_space == "pyautogui", "Action space changed")
    require(adapter.HoloLocalAgent.platform == "ubuntu", "Platform changed")
    return adapter


def load_protocol(manifest_path):
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    config_bytes = (BASELINE / "config.json").read_bytes()
    config = json.loads(config_bytes)
    require(manifest.get("schema_version") == 1 and manifest.get("status") == "frozen", "Manifest must be frozen schema version 1")
    require(safe_name(manifest.get("pilot_id")), "Invalid pilot_id")
    require(manifest.get("baseline_config_sha256") == sha(config_bytes), "Saved baseline config hash differs")
    verify_sources(config)
    if "baseline_manifest_sha256" in manifest:
        require(sha((BASELINE / "manifest.json").read_bytes()) == manifest["baseline_manifest_sha256"], "Original manifest changed")
    inspect_agent(config)
    require(config["max_steps"] == 15 and config["task_timeout"] == 900, "Expected baseline 15 steps and 900-second timeout")
    require(config["workers"] == 1 and config["vm_paths"] == [config["path_to_vm"]], "Expected saved single VM")
    require(config["skip_judge"] is False and config["judge"] == {
        "model": config["judge_model"], "method": "responses_visual_v1", "reasoning": config["judge_reasoning"]}, "Judge configuration differs")
    suffix = manifest.get("intervention_suffix")
    require(isinstance(suffix, str) and suffix.strip(), "Nonempty exact intervention suffix required")
    jobs = {}
    for row in manifest["jobs"]:
        key, job = row["key"], row["job"]
        require(safe_name(key) and key not in jobs, "Duplicate or invalid job key")
        require(all(safe_name(job.get(k)) for k in ("suite", "domain", "id")), "Unsafe job path")
        require(isinstance(job["example"], dict) and isinstance(job["example"].get("instruction"), str)
                and job["example"]["instruction"].strip(), "Missing fixture instruction")
        require("injection" in job, "Explicit injection field required, including null for clean jobs")
        require(job["injection"] is None or isinstance(job["injection"], dict), "Invalid injection")
        require(not job["injection"] or job["injection"].get("type") != "website", "Unsupported website fixture")
        jobs[key] = job
    require(jobs, "No frozen jobs")
    identities, pairs = set(), set()
    for episode in manifest["schedule"]:
        identity, key, arm = episode["episode_id"], episode["job_key"], episode["arm"]
        require(safe_name(identity) and identity not in identities, "Duplicate or invalid episode ID")
        require(key in jobs and arm in ARMS and (key, arm) not in pairs, "Invalid or duplicate scheduled arm/job")
        identities.add(identity)
        pairs.add((key, arm))
    require(pairs == {(key, arm) for key in jobs for arm in ARMS}, "Every frozen job needs exactly one episode per arm")
    tasks = manifest["original_followup_tasks"]
    require(len(tasks) == 11 and len(set(tasks)) == 11, "Expected eleven original follow-up tasks")
    require(all(len(key.split("/")) == 3 and all(safe_name(part) for part in key.split("/")) for key in tasks), "Invalid follow-up task paths")
    return manifest, config, jobs, sha(manifest_bytes)


def alive(pid):
    if pid is None:
        return False
    require(type(pid) is int and pid > 0, "Invalid recorded PID")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def baseline_gate(manifest):
    state = read_json(BASELINE / "follow-up-state.json")
    pids = [state.get("pid"), state.get("original_runner_pid")]
    runner_pid = BASELINE / "runner.pid"
    if runner_pid.exists():
        pids.append(int(runner_pid.read_text().strip()))
    active = [pid for pid in set(pids) if alive(pid)]
    require(not active, "Original baseline/follow-up PID remains active: " + str(active))
    expected = set(manifest["original_followup_tasks"])
    require(set(state.get("retry_candidates", [])) == expected, "Original follow-up task list differs from frozen list")
    if state.get("status") != "retries_finished":
        require(expected.issubset(state.get("retried_tasks", [])), "Original follow-up has not attempted all eleven cases")
    for key in expected:
        path = BASELINE / key / "summary.json"
        require(path.is_file() and read_json(path).get("status") in TERMINAL,
                "Original follow-up missing terminal result: " + key)
    # A killed coordinator can leave a child holding the VM after its lock dies.
    process_list = subprocess.run(["ps", "-axo", "pid=,args="], check=True,
                                  capture_output=True, text=True).stdout
    active_controllers = benchmark_process_pids(process_list, os.getpid())
    require(not active_controllers, "Benchmark-related Python process still active: " + str(active_controllers))
    return {"checked_at": now(), "follow_up_status": state.get("status"), "active_recorded_pids": []}


def benchmark_process_pids(process_list, own_pid):
    """Match executable Python controllers, not shell/editor command mentions."""
    scripts = {"run_os_harm_local.py", "follow_up_os_harm.py", "run_os_harm_pilot.py"}
    modules = {"scripts." + name[:-3] for name in scripts}
    active = []
    for line in process_list.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, command = int(fields[0]), fields[1]
        if pid == own_pid:
            continue
        # Read only the executable first: shell arguments can contain arbitrary
        # quoted Python commands and are not themselves VM controllers.
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        executable = next(lexer, "")
        if not Path(executable).name.startswith("python"):
            continue
        arguments = shlex.split(command)[1:]
        if any(Path(argument).name in scripts for argument in arguments) or any(
                arguments[index] == "-m" and arguments[index + 1] in modules
                for index in range(len(arguments) - 1)):
            active.append(pid)
    return active


def effective_config(config, manifest, arm, fingerprint):
    value = dict(config)
    prompt = config["system_prompt"] + (manifest["intervention_suffix"] if arm == "intervention" else "")
    value.update(system_prompt=prompt, effective_system_prompt_sha256=sha(prompt.encode()), arm=arm,
                 pilot_id=manifest["pilot_id"], manifest_sha256=fingerprint,
                 pilot_wrapper_sha256=sha(Path(__file__).read_bytes()),
                 agent_class="mm_agents.holo_local_agent.HoloLocalAgent", tool_choice="required",
                 action_space="pyautogui", platform="ubuntu", retry_timeouts=False,
                 timeout_retry_policy={"enabled": False, "max_attempts": 1,
                                       "reset_vm_each_attempt": True, "completed_outcomes_never_retried": True})
    return value


def child(options, manifest, config, jobs, fingerprint, output):
    from scripts.benchmark_workers import write_json
    state_path = output / "pilot-state.json"
    state = read_json(state_path)
    token = os.environ.pop("OSHARM_PILOT_CHILD_TOKEN", "")
    require(token and secrets.compare_digest(token, state.get("child_token", "")), "Child requires its live coordinator token")
    require(state["pid"] == os.getppid() and alive(state["pid"]), "Child coordinator is not active")
    require(state["manifest_sha256"] == fingerprint and state["status"] == "running", "Child state differs from frozen pilot")
    require(state.get("current_episode") == options.child and options.child in state["launched_episodes"], "Child episode was not reserved")
    episode = next(row for row in manifest["schedule"] if row["episode_id"] == options.child)
    out = output / "episodes" / episode["arm"] / episode["episode_id"]
    require(read_json(out / "job.json") == jobs[episode["job_key"]], "Child job differs from frozen manifest")
    args = argparse.Namespace(**config)
    args.job, args.output = out / "job.json", output
    args.work_dir = str(output / "workspaces" / episode["episode_id"])
    args.worker_id, args.retry_timeouts = "pilot-sequential", False
    os.chdir(ROOT)
    from scripts.run_holo_vm import configure_inference
    configure_inference(args)
    require(all(getattr(args, key) == config[key] for key in ("provider", "model", "base_url", "max_tokens", "thinking")), "Inference configuration changed")
    adapter = inspect_agent(config)
    effective = effective_config(config, manifest, episode["arm"], fingerprint)
    if episode["arm"] == "intervention":
        adapter.BENCHMARK_SYSTEM = effective["system_prompt"]
    effective.update(output=str(out), job=str(args.job), work_dir=args.work_dir, worker_id=args.worker_id)
    write_json(out / "effective-config.json", effective)
    # Only this child-local module variable differs between the two arms.
    from scripts import run_os_harm_local as runner
    runner.run_job(args)


def vmware_preflight(config, state):
    """Read-only VM checks at execution time, never during --validate."""
    state["vmware_checks"] = []
    for arguments in (["list"], ["getGuestIPAddress", config["path_to_vm"]]):
        result = subprocess.run(["vmrun", "-T", "fusion", *arguments],
                                capture_output=True, text=True, timeout=30)
        state["vmware_checks"].append({"command": arguments[0], "returncode": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        if result.returncode:
            raise RuntimeError(f"VMware preflight {arguments[0]} failed ({result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}; run only from an authorized VM-capable runtime")
        if arguments[0] == "list":
            require(config["path_to_vm"] in result.stdout.splitlines(), "Saved benchmark VM is not running")
        else:
            try:
                ipaddress.ip_address(result.stdout.strip())
            except ValueError as exc:
                raise RuntimeError("VMware returned no valid IPv4/IPv6 address; pilot not launched") from exc


def ledger(output, manifest, state):
    from scripts.benchmark_workers import write_json
    rows = []
    for episode in manifest["schedule"]:
        identity = episode["episode_id"]
        relative = state.get("failed_episode_archives", {}).get(identity)
        out = output / relative if relative else output / "episodes" / episode["arm"] / identity
        summary = read_json(out / "summary.json") if (out / "summary.json").exists() else {}
        rows.append({**episode, "launched": identity in state["launched_episodes"],
            "disposition": summary.get("status", "reserved_without_terminal_summary" if
                identity in state["launched_episodes"] else "not_launched"),
            "result_path": str(out.relative_to(output)) if out.exists() else None,
            "judging": summary.get("judging"), "safety": summary.get("safety"),
            "success": summary.get("success")})
    write_json(output / "episode-ledger.json", rows)


def resume_state(output, manifest, fingerprint):
    from scripts import run_os_harm_local as runner
    from scripts.benchmark_workers import write_json
    state = read_json(output / "pilot-state.json")
    require(state["manifest_sha256"] == fingerprint and state["pilot_id"] == manifest["pilot_id"], "Cannot resume a changed frozen protocol")
    require(state["pilot_wrapper_sha256"] == sha(Path(__file__).read_bytes()), "Cannot resume after wrapper source changes")
    require(not alive(state.get("pid")) and not alive(state.get("child_pid")), "Pilot coordinator or child is still active")
    require(state["status"] != "finished", "Pilot is already finished")
    scheduled = {row["episode_id"] for row in manifest["schedule"]}
    launched = state["launched_episodes"]
    require(len(launched) == len(set(launched)) and set(launched).issubset(scheduled), "Corrupt persistent launch ledger")
    require(set(state["completed_episodes"]).issubset(launched), "Corrupt persistent completion ledger")
    # Preserve a crashed attempt without ever launching it again, including the
    # narrow crash window between recording a reservation and invoking Popen.
    for episode in manifest["schedule"]:
        identity = episode["episode_id"]
        if identity not in launched or identity in state["completed_episodes"]:
            continue
        out = output / "episodes" / episode["arm"] / identity
        if out.exists():
            if not (out / "summary.json").exists():
                write_json(out / "summary.json", {"status": "interrupted", "safety": None,
                    "success": None, "judging": "not_available", "episode_id": identity,
                    "reason": "Reserved before coordinator interruption; never relaunched"})
            if read_json(out / "summary.json").get("status") != "trajectory_complete":
                archived = runner.archive_attempt(argparse.Namespace(output=output), out)
                state.setdefault("failed_episode_archives", {})[identity] = str(archived.relative_to(output))
        elif identity not in state.get("failed_episode_archives", {}):
            history = output / "attempts" / "episodes" / episode["arm"] / identity
            archives = list(history.glob("attempt-*"))
            require(len(archives) <= 1, "Unexpected multiple archives for a single-attempt episode")
            if archives:
                state.setdefault("failed_episode_archives", {})[identity] = str(archives[0].relative_to(output))
        state["completed_episodes"].append(identity)
    state.setdefault("resume_history", []).append({"at": now(), "previous_status": state["status"],
                                                  "previous_pid": state.get("pid")})
    state.update(status="launching", pid=os.getpid(), child_pid=None, current_episode=None,
                 child_token=secrets.token_hex(32))
    state.pop("finished_at", None)
    state.pop("error", None)
    return state


def run(options, manifest, config, jobs, fingerprint, output):
    from filelock import FileLock
    from scripts import run_os_harm_local as runner
    from scripts.benchmark_workers import prepare_workspace, stop_process, wait_for_process, write_json
    require(Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), "Use os-harm/.venv/bin/python")
    os.chdir(ROOT)
    os.environ["PATH"] = "/Applications/VMware Fusion.app/Contents/Library:" + os.environ["PATH"]
    # Never release this lock between arms, judging, or episodes.
    with FileLock(str(ROOT / ".holo-benchmark.lock"), timeout=0):
        gate = baseline_gate(manifest)
        state_path = output / "pilot-state.json"
        if options.resume:
            require(state_path.is_file(), "No persistent pilot state to resume")
            state = resume_state(output, manifest, fingerprint)
            state["baseline_gate"] = gate
        else:
            require(not output.exists(), "Pilot output already exists; use --run --resume for unlaunched cells only")
            output.mkdir(parents=True)
            state = {"status": "launching", "pilot_id": manifest["pilot_id"], "pid": os.getpid(),
                     "manifest_sha256": fingerprint, "pilot_wrapper_sha256": sha(Path(__file__).read_bytes()),
                     "started_at": now(), "baseline_gate": gate,
                     "launched_episodes": [], "completed_episodes": [], "child_token": secrets.token_hex(32)}
        write_json(state_path, state)
        ledger(output, manifest, state)
        stop = threading.Event()
        process = None
        def stopped(signum, frame):
            stop.set()
            raise KeyboardInterrupt(f"Received signal {signum}")
        previous = {sig: signal.signal(sig, stopped) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            vmware_preflight(config, state)
            write_json(output / "frozen-manifest.json", manifest)
            for arm in ARMS:
                arm_config = effective_config(config, manifest, arm, fingerprint)
                arm_config.update(output=str(output / "episodes" / arm), work_dir=None, job=None,
                                  worker_id="pilot-sequential")
                write_json(output / f"{arm}-config.json", arm_config)
            from scripts.run_holo_vm import configure_inference
            args = argparse.Namespace(**config)
            args.output = output
            configure_inference(args)
            from judge.responses_judge import make_client, attach_judgment
            judge_client = make_client()
            judge_client.models.retrieve(config["judge_model"])
            state["status"] = "running"
            write_json(state_path, state)
            for episode in manifest["schedule"]:
                verify_sources(config)
                require(sha(options.manifest.read_bytes()) == fingerprint, "Frozen manifest changed during pilot")
                require(sha((BASELINE / "config.json").read_bytes()) == manifest["baseline_config_sha256"], "Baseline config changed during pilot")
                identity = episode["episode_id"]
                if identity in state["launched_episodes"]:
                    continue
                out = output / "episodes" / episode["arm"] / identity
                if out.exists():
                    require({p.name for p in out.iterdir()} <= {"job.json"}, "Unexpected unreserved episode artifacts")
                out.mkdir(parents=True, exist_ok=True)
                write_json(out / "job.json", jobs[episode["job_key"]])
                work_dir = prepare_workspace(ROOT, output / "workspaces" / identity)
                # Reserve durably before Popen; a crash never allows a second launch.
                state.update(current_episode=identity, child_pid=None)
                state["launched_episodes"].append(identity)
                write_json(state_path, state)
                command = [str(PYTHON), str(Path(__file__).resolve()), "--manifest", str(options.manifest),
                           "--output", str(output), "--child", identity]
                env = dict(os.environ, OSHARM_PILOT_CHILD_TOKEN=state["child_token"])
                with (out / "worker.log").open("w") as log:
                    process = subprocess.Popen(command, cwd=work_dir, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True, env=env)
                    state["child_pid"] = process.pid
                    write_json(state_path, state)
                    try:
                        result = wait_for_process(process, config["task_timeout"], stop)
                    finally:
                        stop_process(process)
                if result != "exited" or not (out / "summary.json").exists():
                    write_json(out / "summary.json", {"status": result if result != "exited" else "worker_failed",
                        "returncode": process.returncode, "safety": None, "success": None, "judging": "not_available"})
                process = None
                summary = read_json(out / "summary.json")
                summary.update(arm=episode["arm"], episode_id=identity, job_key=episode["job_key"],
                               attempt_number=1, manifest_sha256=fingerprint)
                write_json(out / "summary.json", summary)
                if summary["status"] == "trajectory_complete":
                    try:
                        attach_judgment(out, config["judge_model"], config["judge_reasoning"], client=judge_client)
                    except Exception as exc:
                        # Judge records its own error; keep the rollout, never replay it.
                        state.setdefault("judge_errors", {})[identity] = f"{type(exc).__name__}: {exc}"
                else:
                    archived = runner.archive_attempt(args, out)
                    state.setdefault("failed_episode_archives", {})[identity] = str(archived.relative_to(output))
                state["completed_episodes"].append(identity)
                state.update(current_episode=None, child_pid=None)
                write_json(state_path, state)
                ledger(output, manifest, state)
                # Shared IP/setup outage must not consume the remaining frozen jobs.
                if "Unable to get the IP address" in summary.get("error", ""):
                    raise RuntimeError("Shared VM IP failure; remaining pilot episodes not launched")
            state.update(status="finished", finished_at=now())
        except BaseException as exc:
            state.update(status="blocked", error=f"{type(exc).__name__}: {exc}", finished_at=now())
            raise
        finally:
            if process is not None:
                stop_process(process)
            state.pop("child_token", None)
            write_json(state_path, state)
            ledger(output, manifest, state)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            verify_sources(config)
            require(sha((BASELINE / "config.json").read_bytes()) == manifest["baseline_config_sha256"], "Original config changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, help="Canonical experiment/runs/<pilot_id> directory (alternate locations refused)")
    parser.add_argument("--resume", action="store_true", help="With --run, continue only never-reserved cells after a stopped coordinator")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run", action="store_true", help="Execute only after baseline follow-up ends; default is offline validation")
    modes.add_argument("--validate", action="store_true", help="Offline validation; no credentials, lock, VM or network access")
    modes.add_argument("--child", help=argparse.SUPPRESS)
    options = parser.parse_args()
    require(not options.resume or options.run, "--resume requires --run")
    options.manifest = options.manifest.expanduser().resolve()
    manifest, config, jobs, fingerprint = load_protocol(options.manifest)
    canonical_output = (EXPERIMENT / "runs" / manifest["pilot_id"]).resolve()
    output = (options.output or canonical_output).expanduser().resolve()
    require(output == canonical_output, "Use the canonical pilot directory to prevent duplicate launches")
    require(not output.is_relative_to(BASELINE) and not BASELINE.is_relative_to(output), "Pilot output overlaps original baseline")
    require(output.is_relative_to(EXPERIMENT), "Output must stay inside separate experiment directory")
    require(not options.manifest.is_relative_to(output), "Output cannot contain the frozen source manifest")
    if options.child:
        child(options, manifest, config, jobs, fingerprint, output)
    elif options.run:
        run(options, manifest, config, jobs, fingerprint, output)
    else:
        print(json.dumps({"status": "validated_offline", "pilot_id": manifest["pilot_id"],
            "episodes": len(manifest["schedule"]), "manifest_sha256": fingerprint,
            "pilot_wrapper_sha256": sha(Path(__file__).read_bytes()),
            "baseline_config_sha256": manifest["baseline_config_sha256"], "source_hashes_verified": True,
            "baseline_follow_up_status": read_json(BASELINE / "follow-up-state.json").get("status"),
            "output": str(output), "execution_performed": False,
            "arms": {arm: effective_config(config, manifest, arm, fingerprint)["effective_system_prompt_sha256"] for arm in ARMS}}, indent=2))


if __name__ == "__main__":
    main()
