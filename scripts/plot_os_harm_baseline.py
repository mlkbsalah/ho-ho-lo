#!/usr/bin/env python3
"""Create baseline-only OS-Harm figures from manifest-linked saved summaries.

Run from any directory with a Python environment containing Matplotlib:
    MPLCONFIGDIR=/tmp/os-harm-matplotlib os-harm/.venv/bin/python scripts/plot_os_harm_baseline.py
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, PercentFormatter

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["misbehavior", "misuse", "injection"]
LABELS = ["Misbehavior", "Harmful user requests", "Prompt injection"]
DOMAINS = {"vs_code": "VS Code", "multi_apps": "Multiple applications",
           "libreoffice_writer": "LibreOffice Writer", "chrome": "Chrome",
           "os": "Operating system", "thunderbird": "Thunderbird",
           "libreoffice_impress": "LibreOffice Impress",
           "libreoffice_calc": "LibreOffice Calc", "gimp": "GIMP"}
STATUSES = ["trajectory_complete", "error", "unsupported_setup", "invalid_fixture"]
STATUS_LABELS = ["Judged trajectory", "Error", "Unsupported setup", "Invalid fixture"]


def read_json(path, hashes):
    raw = path.read_bytes()
    hashes[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
    return json.loads(raw)


def save(fig, output, stem):
    fig.tight_layout()
    for ext in ["png", "svg", "pdf"]:
        fig.savefig(output / f"{stem}.{ext}", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / "os-harm/results/os-harm-hcompany/full-run")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/final-sprint/os-harm")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    manifest = read_json(args.run / "manifest.json", hashes)
    config = read_json(args.run / "config.json", hashes)
    keys = ["/".join(job[k] for k in ["suite", "domain", "id"]) for job in manifest]
    if len(keys) != 150 or len(set(keys)) != len(keys):
        raise ValueError("Expected the original 150 unique baseline tasks")
    rows = []
    for job, key in zip(manifest, keys):
        data = read_json(args.run / key / "summary.json", hashes)
        if data["status"] not in STATUSES:
            raise ValueError(f"Unexpected task status: {key}: {data['status']}")
        judged = data["status"] == "trajectory_complete"
        if judged and (data.get("judging") != "llm_judged" or
                       type(data.get("safety")) is not bool or type(data.get("success")) is not bool):
            raise ValueError(f"Completed trajectory lacks boolean judge labels: {key}")
        rows.append({"suite": job["suite"], "domain": job["domain"], "id": job["id"],
                     "status": data["status"], "safety": data.get("safety") if judged else None,
                     "success": data.get("success") if judged else None,
                     "automatic_task_success": data.get("automatic_task_success") if judged else None})
    # Detect concurrent source changes before publishing any derived counts.
    for name, digest in hashes.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Source changed while reading: {name}")
    with (output / "task_statistics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    stats = []
    for suite in SUITES:
        subset = [r for r in rows if r["suite"] == suite]
        judged = [r for r in subset if r["status"] == "trajectory_complete"]
        unsafe = sum(r["safety"] is False for r in judged)
        success = sum(r["success"] is True for r in judged)
        stats.append({"suite": suite, "planned": len(subset), "judged": len(judged),
                      "unsafe": unsafe, "successful": success,
                      "unsafe_rate_among_judged": unsafe / len(judged),
                      "success_rate_among_judged": success / len(judged),
                      "statuses": dict(Counter(r["status"] for r in subset))})
    discrepancies = ["/".join(r[k] for k in ["suite", "domain", "id"]) for r in rows
                     if r["automatic_task_success"] is not None and
                     bool(r["automatic_task_success"]) != r["success"]]
    judged_total = sum(s["judged"] for s in stats)
    report = {"scope": "Original OS-Harm baseline only; follow-up experiments excluded",
              "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "categories": stats, "planned": len(rows), "judged": judged_total,
              "statuses": dict(Counter(r["status"] for r in rows)),
              "unsafe": sum(s["unsafe"] for s in stats),
              "successful": sum(s["successful"] for s in stats),
              "domains": dict(Counter(r["domain"] for r in rows)),
              "judge_evaluator_disagreements": discrepancies,
              "setup": {k: config[k] for k in ["model", "observation_type", "max_steps", "task_timeout",
                        "temperature", "top_p", "history_images", "workers", "judge", "harness", "network"]},
              "source_sha256": hashes}
    (output / "summary_statistics.json").write_text(json.dumps(report, indent=2) + "\n")
    plt.style.use("default")

    fig, ax = plt.subplots(figsize=(9, 6))
    domain_counts = Counter(r["domain"] for r in rows)
    domains = sorted(domain_counts, key=lambda k: (-domain_counts[k], k))
    left = [0] * len(domains)
    for suite, label in zip(SUITES, LABELS):
        values = [sum(r["suite"] == suite and r["domain"] == d for r in rows) for d in domains]
        ax.barh([DOMAINS[d] for d in domains], values, left=left, label=label)
        left = [a + b for a, b in zip(left, values)]
    ax.invert_yaxis()
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("Number of tasks")
    ax.set_title("OS-Harm dataset composition")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3)
    save(fig, output, "01_dataset_composition")

    fig, ax = plt.subplots(figsize=(9, 4.5))
    left = [0] * len(stats)
    for status, label in zip(STATUSES, STATUS_LABELS):
        values = [s["statuses"].get(status, 0) for s in stats]
        ax.barh(LABELS, values, left=left, label=label)
        left = [a + b for a, b in zip(left, values)]
    ax.set_xlim(0, 50)
    ax.set_xticks(range(0, 51, 10))
    ax.invert_yaxis()
    ax.set_xlabel("Number of tasks")
    ax.set_title("OS-Harm baseline run coverage")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)
    save(fig, output, "02_run_coverage")

    fig, ax = plt.subplots(figsize=(9, 3))
    success_rate = report["successful"] / judged_total
    ax.barh([f"All judged tasks (n={judged_total})"], [success_rate], label="Success")
    ax.barh([f"All judged tasks (n={judged_total})"], [1 - success_rate],
            left=[success_rate], label="Failure")
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Percentage of judged tasks")
    ax.set_title("OS-Harm task success and failure")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.3), ncol=2)
    save(fig, output, "03_task_success")

    fig, ax = plt.subplots(figsize=(9, 4.5))
    categories = [f"{label} (n={s['judged']})" for label, s in zip(LABELS, stats)]
    success_rates = [s["successful"] / s["judged"] for s in stats]
    ax.barh(categories, success_rates, label="Success")
    ax.barh(categories, [1 - rate for rate in success_rates], left=success_rates, label="Failure")
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Percentage of judged tasks")
    ax.set_title("OS-Harm task success and failure by category")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)
    save(fig, output, "04_success_by_category")
    print(json.dumps({"output": str(output), "planned": len(rows), "judged": judged_total,
                      "unsafe": report["unsafe"], "successful": report["successful"],
                      "evaluator_disagreements": len(discrepancies), "figures": 4}))


if __name__ == "__main__":
    main()
