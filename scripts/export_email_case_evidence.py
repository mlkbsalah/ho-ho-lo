#!/usr/bin/env python3
"""Export two unmodified recorded frames and a trace for the email case study."""
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
KEY = "injection/thunderbird/__injected_email__inject__thunderbird_mail_received__url_query_params"
SOURCE = ROOT / "os-harm/results/os-harm-hcompany/full-run" / KEY
OUTPUT = ROOT / "outputs/final-sprint/email-credential-summary-case"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    log = json.loads((SOURCE / "better_log.json").read_text())
    events = [json.loads(line) for line in (SOURCE / "traj.jsonl").read_text().splitlines() if line.strip()]
    original_names = [log["steps"][4]["screenshot_file"], log["steps"][10]["screenshot_file"]]
    exported_names = ["01_injected_email.png", "02_contaminated_draft.png"]
    frames = []
    for original, exported in zip(original_names, exported_names):
        shutil.copyfile(SOURCE / original, OUTPUT / exported)
        with Image.open(SOURCE / original) as image:
            frames.append(image.convert("RGB"))
    frames[0].save(OUTPUT / "email-to-draft.gif", save_all=True, append_images=frames[1:],
                   duration=[5000, 8000], loop=0)
    trace = {"task_key": KEY, "instruction": log["task"]["instruction"],
             "frames": [
                 {"file": exported_names[0], "source": original_names[0], "policy_index_before_action": 4},
                 {"file": exported_names[1], "source": original_names[1], "policy_index_before_action": 10,
                  "effect_of_policy_index": 9}],
             "submitted_actions": [{"policy_index": i, "actions": s["actions"]}
                                   for i, s in enumerate(log["steps"])],
             "executed_events": events,
             "limits": "No credential retrieval, endpoint visit or email transmission is recorded. The password-like literal in the draft is not independently validated. A Save click does not independently establish persistence."}
    (OUTPUT / "trace.json").write_text(json.dumps(trace, indent=2) + "\n")
    files = original_names + ["better_log.json", "traj.jsonl", "job.json", "summary.json", "recording.mp4"]
    provenance = {"source_directory": str(SOURCE), "source_sha256": {
        name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in files},
        "gif": {"file": "email-to-draft.gif", "type": "selected recorded screenshots, not a continuous replay",
                "source_frames": original_names, "duration_ms": [5000, 8000], "original_dimensions": list(frames[0].size),
                "cropping": False, "overlays": False}}
    (OUTPUT / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "gif_frames": 2, "policy_steps": len(log["steps"]),
                      "gif_bytes": (OUTPUT / "email-to-draft.gif").stat().st_size}))


if __name__ == "__main__":
    main()
