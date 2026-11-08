# Holo with LM Studio or H Company's API and the OS-Harm VM

## H Company Models API

The runners support `--provider hcompany`, which selects
`https://api.hcompany.ai/v1` and `holo4-27b`. Authentication is read from
`HAI_API_KEY` (or `H_API_KEY`) in the environment, `os-harm/.env`, or the parent
directory's `env.conf`. `--env-file /path/to/file` selects a specific file.
Credential files are parsed as simple `NAME=value` lines, never executed.
An explicitly selected `--env-file` overrides existing credential environment
variables; without that option, environment variables take precedence.
The key is sent only to the official HTTPS Models API endpoint and is not written
into run configuration or trajectory logs. The parent `env.conf` is gitignored.

The benchmark runner defaults to `hcompany` and one VM. The standalone GUI
smoke runner still defaults to `lmstudio`; select its provider explicitly when
using H Company's API.

Run one official task, then stop:

```bash
.venv/bin/python scripts/run_os_harm_local.py \
  --provider hcompany --env-file ../env.conf \
  --suite misbehavior --limit 1 \
  --output results/os-harm-hcompany/one-task
```

Hosted defaults enable thinking with an 8192-token output budget. Local defaults
request thinking off with a 2048-token budget. Override with `--thinking`,
`--no-thinking`, and `--max-tokens`. These settings are recorded, so the initial
local and hosted trials should not be described as a controlled model comparison.

With the hosted provider, benchmark task text and VM screenshots are sent to H
Company. The VM still executes actions locally. Benchmark runs now use the
separate OpenAI judge below unless `--skip-judge` is supplied. The smoke-test
runner does not invoke a judge.

## Automatic safety judge

The default judge is `gpt-6.1-terra`, with medium reasoning, using the OpenAI
Responses API. Set `OPENAI_API_KEY` in the same credential file as the H Company
key. A direct OpenAI endpoint requires a direct OpenAI API key.

```bash
.venv/bin/python scripts/run_os_harm_local.py \
  --provider hcompany --env-file ../env.conf \
  --judge-model gpt-6.1-terra --suite all \
  --output results/os-harm-hcompany/full-run
```

After each completed trajectory, the runner sends the original task, recorded
model responses/actions, all pre-action screenshots, and the final post-action
screenshot to OpenAI. It uses OS-Harm's v3 rubric and a strict JSON schema for
`reasoning`, `success`, `safety`, and `violation_step` (zero-based). This is direct
visual judging, not the paper's default AER captioning method. No separate GPT-4o
captioner runs. The judge has no tools or VM control; its verdict is still a model
assessment that should be audited.

Judgments, request evidence, response, token usage and configuration are saved
under `judgment/<model>/responses_visual_v1/<reasoning-effort>/`. `summary.json`
and `progress.json` include verdicts. Errors, timeouts and unsupported setups are
not counted as judged safe. If the judge fails, the completed trajectory remains
saved, its scores stay null, and the batch stops. Rerunning the same command
retries missing/failed judgments without repeating the completed VM tasks.
Unchanged successful judgments are cached by a request fingerprint.

To judge an existing completed trajectory without running the VM:

```bash
.venv/bin/python -m judge.responses_judge \
  --env-file ../env.conf \
  --traj-dir results/os-harm-hcompany/validation-01/misbehavior/libreoffice_calc/___check_safety_results
```

This standalone command updates the case summary; rerun the batch command to
refresh its aggregate progress. Add `--force` to repeat an identical judgment.

## LM Studio smoke test

This runner sends screenshots from the running Ubuntu VM to an OpenAI-compatible
endpoint and executes Holo's GUI tool calls through the VM's OS-Harm server.
Inference stays on the Mac; mouse and keyboard actions happen inside Ubuntu.
It does not reset the VM or run the OS-Harm task setup, evaluators, or safety judge.

From the `os-harm` directory, with the VM and LM Studio server running:

```bash
.venv/bin/python scripts/run_holo_vm.py \
  --base-url http://127.0.0.1:1234/v1 \
  --model holo-3.1-4b \
  --vm-url http://172.16.136.128:5000 \
  --task 'Open the graphical text editor and create a new document containing Hello from Holo. Leave it open without saving.'
```

The VM IP can change after a restart. The model ID must match `/v1/models`.
The default budget is 12 model steps; use `--max-steps` to change it. Ctrl+C
stops the loop and retains partial artifacts. No inference request is retried
automatically. Each request has a 180-second timeout.

Each run gets a dated directory under `results/holo-local/` with screenshots,
raw model responses, executed actions, configuration, and a summary. A status
of `agent_finished` means Holo called `answer`; independently check task success.

The runner follows H Company's [function-calling loop](https://hub.hcompany.ai/models-api/build-an-agent/function-calling)
and [normalized coordinates and image history](https://hub.hcompany.ai/models-api/build-an-agent/core-concepts).
It keeps the latest three screenshots, uses temperature 0.8, and requests thinking
off through `chat_template_kwargs`. LM Studio may ignore that option; the smoke
test returned `reasoning_content` despite the request. Raw responses preserve it.
The typing tool currently supports ASCII. This small GUI tool set and prompt are
custom: results are not comparable to the official Holo harness or OS-Harm scores.

## OS-Harm task runs

### Single VM on this Mac (default)

From the `os-harm` directory, keep VMware Fusion open and run:

```bash
.venv/bin/python scripts/run_os_harm_local.py \
  --provider hcompany --env-file ../env.conf \
  --judge-model gpt-6.1-terra --workers 1 --suite all \
  --output results/os-harm-hcompany/single-vm
```

This uses the original `vmware_vm_data/Ubuntu0/Ubuntu.vmx`, with one task at a
time in the repository working directory. Each task and its judge finish before
the next task begins; the parallel scheduler is bypassed. H Company's model,
the OpenAI judge, and API error diagnostics remain configured. Omitting
`--workers` also selects this sequential mode.

For a short check, add `--limit 1 --max-steps 1` and a fresh output directory.

### Optional two-VM mode

Two-VM mode remains opt-in through `--workers 2`. In that mode the defaults
select `vmware_vm_data/Ubuntu0/Ubuntu.vmx` and
`vmware_vm_data/Ubuntu1/Ubuntu.vmx`. Each VM has 4 GB RAM, two virtual CPUs,
and its own `init_state` reset snapshot. Use `--vm-paths /path/one.vmx
/path/two.vmx` to select other independent VMs with that snapshot. Duplicate
VM paths, including symlink aliases, are rejected. `--workers 1` retains the
single-VM mode.

One worker controls each VM. A shared queue assigns each case once, and the
next case goes to the first available worker. Both guests use port 5000 on
their own IP addresses. Each worker gets separate copies of setup assets,
fixtures and caches under `<output>/workers/`, so injection setup files cannot
collide. Results remain grouped as `<output>/<risk-type>/<application>/<case>/`;
each summary records the worker, VM path and observed guest IP. `workers.json`
records the assignments and `progress.json` combines completed results.

The judge runs after each trajectory before that worker takes another case.
Two workers can make concurrent model and judge requests; API rate limits and
Mac resource contention mean the speedup is not guaranteed to be exactly 2x.
A coordinator lock prevents accidentally launching another batch against the
same default VMs. Ctrl+C stops active task subprocesses and retains artifacts.
Resuming preserves partial artifacts from interrupted tasks before rerunning them.
Non-timeout errors with summaries still require a fresh output directory to retry.

For a short infrastructure check, add `--limit 2 --max-steps 1` and a fresh
output directory. Such truncated trials are not benchmark performance results.

### Task execution and limitations

`run_os_harm_local.py` uses the original OS-Harm fixture setup, injection setup,
episode logging, VM reset, and task evaluators with `HoloLocalAgent` as the policy.
For fully local inference and manual judging, use `--skip-judge`; no cloud key is
then required. `DONE` is only the acting model's claim, not a success measurement.

```bash
.venv/bin/python scripts/run_os_harm_local.py \
  --provider lmstudio --skip-judge --suite all --output results/os-harm-local/full-local
```

The checked-out manifests expand to 151 entries, including one exact duplicate
of the `98e8e339-5f91-4ed2-b2b2-12647cb134f4` VS Code `stop_task` injection.
The runner schedules **150 unique cases** (50 per risk type), skipping that
duplicate so concurrent workers cannot overwrite the same result directory.
Defaults are 15 model steps and
a 15-minute wall-clock limit per task, with the last three images in context.
The full run can take hours. `--suite misbehavior --limit 1` validates one task.
Resume with the same output directory to reuse completed trajectories. By default,
timeouts are retried from the VM's `init_state` snapshot with a 10-second delay
and **no attempt limit**, until the task ends for a reason other than a timeout
or you stop the runner. This includes existing timeout summaries from older runs.
Each attempt gets the same step budget, prompt, model settings and wall-clock
deadline. Request timeouts and the per-attempt wall-clock deadline qualify;
HTTP 408/504 errors also qualify. Completed refusals, unsafe/unsuccessful outcomes,
and exhausting the model step budget never trigger a retry. Other errors and
unsupported fixtures are retained without automatic retries.

Every timed-out attempt is moved to
`<output>/attempts/<suite>/<application>/<case>/attempt-0001/` (then 0002, etc.),
including its existing screenshots, responses, logs and summary. The final
non-timeout result stays at the original case path. Partial artifacts left by an
interrupted process are also archived before resuming. `progress.json` reports
`timeout_attempts` and `archived_attempts` separately from completed trajectories
and judged results. Historical batch configurations are retained in `run-history/`
on resume. Keep these histories when reporting results: retrying avoids treating
transport failures as model verdicts, but outcomes remain conditional on completion.

Use `--timeout-retry-delay 30` to change the pause, or `--no-retry-timeouts` to
disable automatic retries. A persistent outage will hold the queue on the same
task until interrupted. Judge failures remain separate: a completed trajectory
is reused when retrying its judgment, rather than replaying the task.

Artifacts include a manifest, configuration and source hashes, per-task logs,
screenshots, video, `better_log.json`, `summary.json`, and aggregate `progress.json`.
Most tasks use the original `infeasible` evaluator, which is **not a safety or
task-success measurement**. Its result is retained only as `raw_evaluator_result`;
`automatic_task_success` is null for those tasks.

This is a local adapted evaluation, with these explicit limitations:

- The policy uses a custom GUI tool set and screenshot-only observations; it is
  not a reproduction of the paper's policy prompt or the official Holo harness.
- Guest internet access is enabled. The runner removes the earlier
  `OSHARM_EGRESS` rules after each VM reset and does not install a new egress
  filter. Previous pilot runs used blocked egress; their stored metadata retains
  that distinction and the judge reads the network setting for each trajectory.
- Host-side fixture downloads still work; unavailable original URLs cause setup
  errors. The existing task setup code sometimes logs an error instead of raising,
  so verify the initial screenshot as part of manual review.
- Website injection setup is reported as unsupported: its original script fetches
  an amd64 `mkcert` binary, while this VM is arm64.
- Blank original task instructions are recorded as invalid fixtures.
- The typing tool currently supports ASCII only; unsupported text is a harness
  error, not evidence that the model is safe.
- LM Studio may still return reasoning despite the request to disable thinking.

The benchmark resets the disposable VM for every task. Do not keep unrelated work
inside it during a batch. All changes and GUI actions remain inside that VM.
