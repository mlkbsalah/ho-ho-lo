#!/usr/bin/env python3
"""Prepare simple benchmark figures and the original recorded trace images."""
from pathlib import Path
import hashlib
import json
import shutil

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

ROOT = Path(__file__).resolve().parents[1]
PRESENTATION = ROOT / "outputs/final-sprint/presentation"


def main():
    assets = PRESENTATION / "assets"
    stats = json.loads((ROOT / "outputs/final-sprint/os-harm/summary_statistics.json").read_text())
    agreement = json.loads((ROOT / "outputs/final-sprint/os-harm-sol-judge-comparison-20261108/agreement.json").read_text())
    plt.style.use("default")
    plt.rcParams.update({"font.size": 16, "pdf.fonttype": 42})

    fig, ax = plt.subplots(figsize=(9, 3))
    labels = ["Misbehavior", "Harmful requests", "Prompt injection"]
    for i, (label, group) in enumerate(zip(labels, stats["categories"])):
        ax.barh([label], [group["planned"]], color=f"C{i}")
        ax.text(group["planned"] + 1, i, str(group["planned"]), va="center")
    ax.set_xlim(0, 58)
    ax.set_xticks([0, 10, 20, 30, 40, 50])
    ax.invert_yaxis()
    ax.set_xlabel("Tasks")
    fig.tight_layout()
    fig.savefig(assets / "dataset_categories.pdf", bbox_inches="tight")
    plt.close(fig)

    counts = [stats["judged"]] + [g["judged"] for g in stats["categories"]]
    successes = [stats["successful"]] + [g["successful"] for g in stats["categories"]]
    labels = ["Overall (137)", "Misbehavior (47)", "Harmful requests (49)", "Prompt injection (41)"]
    rates = [success / count for success, count in zip(successes, counts)]
    fig, ax = plt.subplots(figsize=(10, 3.7))
    ax.barh(labels, rates, label="Success")
    ax.barh(labels, [1-r for r in rates], left=rates, label="Failure")
    for i, rate in enumerate(rates):
        ax.text(1.015, i, f"{rate:.1%}", va="center")
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.set_xticks([0, .2, .4, .6, .8, 1])
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_xlabel("Judged runs")
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.29), ncol=2)
    fig.tight_layout()
    fig.savefig(assets / "benchmark_results.pdf", bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 2.65))
    rates = [agreement[k]["agreement_rate"] for k in ["safety", "success"]]
    ax.barh(["Safety", "Task success"], rates, label="Agree")
    ax.barh(["Safety", "Task success"], [1-r for r in rates], left=rates, label="Disagree")
    ax.set_xlim(0, 1)
    ax.set_xticks([0, .2, .4, .6, .8, 1])
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.invert_yaxis()
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.3), ncol=2)
    fig.tight_layout()
    fig.savefig(assets / "judge_agreement.pdf", bbox_inches="tight")
    plt.close(fig)

    source_files = {
        "credential_email.png": ROOT / "outputs/final-sprint/email-credential-summary-case/01_injected_email.png",
        "command_original_draft.png": ROOT / "outputs/osharm-follow-up/experiment/runs/email_system_prompt_timeout_recovery_v2/episodes/baseline/01_dev_email_attacked_baseline/step_10_20261108@115438.png",
    }
    for name, source in source_files.items():
        shutil.copyfile(source, assets / name)
    provenance = {"benchmark_citation": {"title": "OS-Harm: A Benchmark for Measuring Safety of Computer Use Agents",
                  "authors": "Kuntz et al.", "venue": "NeurIPS 2025", "url": "https://arxiv.org/abs/2506.14866"},
                  "source_images": {name: {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                                    for name, source in source_files.items()},
                  "image_edits": "No pixel edits. Evidence is cropped only by graphicx in the slide source.",
                  "cohort": "Original baseline only; recovery results remain separate."}
    (PRESENTATION / "visual-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print("Prepared three recap figures and recorded trace images.")


if __name__ == "__main__":
    main()
