#!/usr/bin/env python3
"""Compare Sol medium against saved Terra medium judgments without a VM.

Reuses each exact original API request, changing only the model ID. Original
trajectory files, summaries, prompts and judgments are never modified.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "os-harm"))
from judge.responses_judge import make_client, validate_verdict
from scripts.run_holo_vm import load_credentials


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agreement(results, field):
    pairs = [(r["previous"][field], r["new"][field]) for r in results]
    n = len(pairs)
    if not n:
        return {"n": 0}
    observed = sum(a == b for a, b in pairs) / n
    a, b = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    expected = sum(a[v] * b[v] for v in [False, True]) / n ** 2
    return {"n": n, "agree": sum(a == b for a, b in pairs),
            "agreement_rate": observed,
            "cohen_kappa": (observed - expected) / (1 - expected) if expected < 1 else None,
            "confusion": {f"terra_{a}_sol_{b}": sum(x == a and y == b for x, y in pairs)
                          for a in [False, True] for b in [False, True]}}


def compare_one(case, output, model):
    dest = output / "cases" / case["key"]
    dest.mkdir(parents=True, exist_ok=True)
    result_path = dest / "comparison.json"
    if result_path.exists():
        previous = read(result_path)
        if previous.get("status") == "judged" and previous.get("source_request_sha256") == case["request_sha256"]:
            return previous
    source = Path(case["request_path"])
    if digest(source) != case["request_sha256"]:
        raise RuntimeError(f"Original judging request changed: {case['key']}")
    payload = read(source)
    if payload["model"] != "gpt-6.1-terra" or payload["reasoning"] != {"effort": "medium"}:
        raise ValueError("Expected original Terra medium request")
    payload["model"] = model
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    write(dest / "request-provenance.json", {"source_request": str(source),
          "source_request_sha256": case["request_sha256"], "changes": {"model": model},
          "request_sha256": request_hash, "reasoning_effort": "medium"})
    record = {"task": case["key"], "suite": case["suite"], "started_at": now(),
              "source_request_sha256": case["request_sha256"], "previous": case["previous"],
              "requested_model": model, "attempts": []}
    client = make_client()
    for attempt in range(1, 4):
        item = {"attempt": attempt, "started_at": now()}
        record["attempts"].append(item)
        try:
            response = client.responses.create(**payload)
            write(dest / f"response-attempt-{attempt:04d}.json", response.model_dump())
            if response.status != "completed" or not response.output_text:
                raise ValueError(f"Incomplete judge response: {response.status}")
            verdict = validate_verdict(json.loads(response.output_text), case["step_count"])
            write(dest / "judgment.json", verdict)
            item.update(status="judged", finished_at=now())
            record.update(status="judged", new=verdict, returned_model=response.model,
                          response_id=response.id, finished_at=now(),
                          usage=response.usage.model_dump() if response.usage else None)
            break
        except Exception as exc:
            item.update(status="error", finished_at=now(), error_type=type(exc).__name__, error=str(exc)[:1200])
            code = getattr(exc, "status_code", None)
            retryable = ("timeout" in type(exc).__name__.lower() or
                         "connection" in type(exc).__name__.lower() or code == 429 or
                         isinstance(code, int) and code >= 500)
            write(dest / "attempts.json", record)
            if not retryable or attempt == 3:
                record.update(status="unresolved", finished_at=now())
                break
            time.sleep(10)
    write(result_path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "os-harm/results/os-harm-hcompany/full-run")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/final-sprint/os-harm-sol-judge-comparison-20261108")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    model = "gpt-6.1-sol"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        cases = read(manifest_path)
    else:
        cases = []
        for job in read(args.baseline / "manifest.json"):
            key = "/".join(job[k] for k in ["suite", "domain", "id"])
            summary_path = args.baseline / key / "summary.json"
            summary = read(summary_path)
            if summary.get("status") != "trajectory_complete" or summary.get("judging") != "llm_judged":
                continue
            judgment_path = Path(summary["judgment_path"])
            request = judgment_path.parent / "request.json"
            if not request.exists():
                raise FileNotFoundError(f"No original request for {key}")
            cases.append({"key": key, "suite": job["suite"], "request_path": str(request),
                          "request_sha256": digest(request), "summary_path": str(summary_path),
                          "summary_sha256": digest(summary_path), "judgment_path": str(judgment_path),
                          "judgment_sha256": digest(judgment_path), "previous": read(judgment_path),
                          "step_count": len(read(args.baseline / key / "better_log.json")["steps"])})
        if len(cases) != 137:
            raise ValueError(f"Expected original 137 judged cases; got {len(cases)}")
        write(manifest_path, cases)
    if args.prepare_only:
        print(json.dumps({"status": "prepared", "tasks": len(cases), "output": str(output)}), flush=True)
        return
    load_credentials(str(ROOT / "env.conf"))
    state = {"status": "preflight", "pid": os.getpid(), "model": model, "reasoning_effort": "medium",
             "started_at": now(), "planned": len(cases), "processed": 0, "judged": 0, "unresolved": 0,
             "workers": args.workers, "manifest_sha256": digest(manifest_path),
             "script_sha256": digest(Path(__file__)), "results": {}}
    write(output / "state.json", state)
    try:
        make_client().models.retrieve(model)
        state["status"] = "running"
        write(output / "state.json", state)
        print(f"Starting {model} medium on {len(cases)} saved trajectories ({args.workers} workers)", flush=True)
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(compare_one, case, output, model): case for case in cases}
            for future in as_completed(futures):
                case = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"task": case["key"], "suite": case["suite"], "status": "unresolved",
                              "error_type": type(exc).__name__, "error": str(exc)[:1200]}
                results.append(result)
                state["results"][case["key"]] = result["status"]
                state.update(processed=len(results), judged=sum(r["status"] == "judged" for r in results),
                             unresolved=sum(r["status"] != "judged" for r in results), updated_at=now())
                write(output / "state.json", state)
                judged = [r for r in results if r["status"] == "judged"]
                comparison = {"planned": len(cases), "judged": len(judged), "unresolved": state["unresolved"],
                              "safety": agreement(judged, "safety"), "success": agreement(judged, "success"),
                              "disagreements": [r["task"] for r in judged if any(r["previous"][f] != r["new"][f] for f in ["safety", "success"])],
                              "by_category": {suite: {f: agreement([r for r in judged if r["suite"] == suite], f)
                                               for f in ["safety", "success"]} for suite in ["misbehavior", "misuse", "injection"]}}
                write(output / "agreement.json", comparison)
                print(f"[{len(results)}/{len(cases)}] {case['key']}: {result['status']}", flush=True)
        with (output / "comparison.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["task", "status", "terra_safety", "sol_safety", "terra_success", "sol_success",
                             "terra_reasoning", "sol_reasoning"])
            for result in sorted(results, key=lambda r: r["task"]):
                old, new = result.get("previous", {}), result.get("new", {})
                writer.writerow([result["task"], result["status"], old.get("safety"), new.get("safety"),
                                 old.get("success"), new.get("success"), old.get("reasoning"), new.get("reasoning")])
        for case in cases:
            if digest(Path(case["summary_path"])) != case["summary_sha256"] or digest(Path(case["judgment_path"])) != case["judgment_sha256"]:
                raise RuntimeError("Original baseline verdict or summary changed")
        state.update(status="finished_with_unresolved" if state["unresolved"] else "finished", finished_at=now(),
                     original_summaries_and_verdicts_unchanged=True)
        write(output / "state.json", state)
    except BaseException as exc:
        state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "blocked",
                     stopped_at=now(), error_type=type(exc).__name__, error=str(exc)[:1200])
        write(output / "state.json", state)
        raise


if __name__ == "__main__":
    main()
