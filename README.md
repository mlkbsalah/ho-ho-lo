# OS-Harm with Holo

Run the 150-task OS-Harm safety benchmark with Holo4-27B. Results and findings are in [final sprint.md](final%20sprint.md).

## Requirements

- Python 3.12 and `uv`.
- VMware Fusion on macOS or Workstation on Linux, with `vmrun` available.
- An OSWorld Ubuntu VM (`.vmx`) with an `init_state` snapshot and its desktop server on port 5000. See the [benchmark setup notes](os-harm/README.md#installation).
- An H Company API key for Holo and an OpenAI API key for the safety judge.

## Setup

```sh
cd os-harm
uv sync --frozen --python 3.12
uv run playwright install chromium
```

Put your keys in `env.conf` at the repo root, using [env.conf.example](env.conf.example) as a template. This file is ignored by Git.

## Run

From `os-harm/`, with VMware open:

```sh
.venv/bin/python scripts/run_os_harm_local.py \
  --provider hcompany --env-file ../env.conf \
  --path-to-vm /path/to/Ubuntu.vmx \
  --suite all --output results/os-harm-hcompany/new-run
```

Replace the VM path with yours. Each task resets the VM to `init_state`.

For a quick check, replace `--suite all` with `--suite misbehavior --limit 1`. The full run uses one VM, 15 steps per task, and the default visual safety judge.

Screenshots, recordings, judgments, and `progress.json` go in the output directory. Rerun the same command to resume. Timeouts retry until stopped; add `--no-retry-timeouts` to disable this.

More runner options: [HOLO_LOCAL.md](os-harm/scripts/HOLO_LOCAL.md).
