#!/usr/bin/env python3
"""Export the matched original/safety-prompt email trial as recorded evidence."""
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "outputs/osharm-follow-up/experiment"
RUN = EXPERIMENT / "runs/email_system_prompt_timeout_recovery_v2/episodes"
OUTPUT = ROOT / "outputs/final-sprint/email-system-prompt-intervention"
PROTOCOL = EXPERIMENT / "email-system-prompt-four-cell-v1.json"


def load(path):
    return json.loads(path.read_text())


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    paths = {"original-prompt": RUN / "baseline/01_dev_email_attacked_baseline",
             "safety-prompt": RUN / "intervention/02_dev_email_attacked_intervention"}
    configs = {k: load(p / "effective-config.json") for k, p in paths.items()}
    suffix = load(PROTOCOL)["intervention_suffix"]
    assert configs["safety-prompt"]["system_prompt"] == configs["original-prompt"]["system_prompt"] + suffix
    settings = ["model", "max_steps", "max_tokens", "task_timeout", "temperature", "top_p", "history_images", "thinking"]
    assert all(configs["original-prompt"][k] == configs["safety-prompt"][k] for k in settings)
    logs = {k: load(p / "better_log.json") for k, p in paths.items()}
    assert logs["original-prompt"]["task"] == logs["safety-prompt"]["task"]
    (OUTPUT / "system-prompt-addition.txt").write_text(suffix.strip() + "\n")
    provenance = {"protocol": str(PROTOCOL), "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
                  "matched_task_and_payload": True, "matched_settings": settings, "runs": {}}
    for arm, source in paths.items():
        dest = OUTPUT / arm
        dest.mkdir(exist_ok=True)
        log = logs[arm]
        originals = [log["steps"][index]["screenshot_file"] for index in [4, 10]]
        frames = []
        for original, exported in zip(originals, ["01_injected_email.png", "02_resulting_draft.png"]):
            shutil.copyfile(source / original, dest / exported)
            with Image.open(source / original) as image:
                frames.append(image.convert("RGB"))
        frames[0].save(dest / "email-to-draft.gif", save_all=True, append_images=frames[1:],
                       duration=[5000, 8000], loop=0)
        summary = load(source / "summary.json")
        trace = {"arm": arm, "instruction": log["task"]["instruction"],
                 "submitted_actions": [{"policy_index": i, "actions": step["actions"]}
                                       for i, step in enumerate(log["steps"])],
                 "executed_events": [json.loads(line) for line in (source / "traj.jsonl").read_text().splitlines() if line.strip()],
                 "judgment": summary["judgment"],
                 "evidence_frames": [{"source_file": name, "policy_index_before_action": index}
                                     for name, index in zip(originals, [4, 10])]}
        (dest / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        source_files = originals + ["better_log.json", "traj.jsonl", "summary.json", "job.json", "effective-config.json", "recording.mp4"]
        provenance["runs"][arm] = {"source": str(source), "source_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in source_files},
            "gif": "two unmodified recorded screenshots, not a continuous replay",
            "safety": summary["safety"], "success": summary["success"]}
    (OUTPUT / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "matched_payload": True, "gifs": 2}))


if __name__ == "__main__":
    main()
