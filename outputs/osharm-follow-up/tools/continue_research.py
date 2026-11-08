#!/usr/bin/env python3
"""Wait for the baseline, refresh reports, then run the frozen separate pilot.

No baseline files are changed. The pilot owns its own persistent launch ledger
and acquires the benchmark lock atomically before any VM interaction.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[3]
OUTPUT = PROJECT / 'outputs/osharm-follow-up'
ROOT = PROJECT / 'os-harm'
sys.path.insert(0, str(ROOT))
from filelock import FileLock
from scripts.benchmark_workers import write_json

PYTHON = ROOT / '.venv/bin/python'
NODE = Path('/Users/malek/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
PROTOCOL = OUTPUT / 'research-design.json'
STATE = OUTPUT / 'research-workflow-state.json'
TERMINAL = {'trajectory_complete', 'error', 'timeout', 'worker_failed', 'interrupted', 'invalid_fixture', 'unsupported_setup'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(path):
    return json.loads(path.read_text())


def publish(state, **changes):
    state.update(changes, updated_at=now())
    write_json(STATE, state)


def run_logged(command, name):
    print(f'{now()} {name}', flush=True)
    with (OUTPUT / f'{name}.log').open('a') as log:
        log.write(f'\n{now()}\n')
        log.flush()
        return subprocess.run([str(x) for x in command], cwd=PROJECT, stdout=log,
                              stderr=subprocess.STDOUT).returncode


def refresh():
    commands = [([PYTHON, OUTPUT / 'tools/capture_snapshot.py'], 'capture-snapshot'),
                ([NODE, OUTPUT / 'tools/refresh_workbook.mjs'], 'refresh-workbook'),
                ([NODE, OUTPUT / 'tools/build_deck.mjs'], 'build-deck')]
    for command, name in commands:
        code = run_logged(command, name)
        if code:
            raise RuntimeError(f'{name} failed with exit {code}; see {OUTPUT / (name + ".log")}')


def baseline_finished(protocol):
    run = ROOT / 'results/os-harm-hcompany/full-run'
    follow_up = load(run / 'follow-up-state.json')
    if follow_up.get('status') in ('running', 'launching'):
        return False, follow_up
    if follow_up.get('status') != 'retries_finished':
        raise RuntimeError('Baseline follow-up stopped before completing the eleven attempts: ' + follow_up.get('status', 'unknown'))
    for key in protocol['original_followup_tasks']:
        path = run / key / 'summary.json'
        if not path.exists() or load(path).get('status') not in TERMINAL:
            return False, follow_up
    return True, follow_up


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-only', action='store_true', help='Refresh reports after baseline finishes; no pilot launch')
    parser.add_argument('--max-wait-seconds', type=int, default=14400)
    options = parser.parse_args()
    protocol_bytes = PROTOCOL.read_bytes()
    protocol = json.loads(protocol_bytes)
    fingerprint = hashlib.sha256(protocol_bytes).hexdigest()
    expected = 'edec1bffb8a195ddcd379170a7718476523cb7a36c861f260214e9fd3826e9ed'
    if fingerprint != expected:
        raise RuntimeError('Frozen research protocol changed; no workflow launched')
    with FileLock(str(OUTPUT / '.research-workflow.lock'), timeout=0):
        state = load(STATE) if STATE.exists() else {}
        state.update(pid=os.getpid(), protocol_sha256=fingerprint,
                     report_page_id='page_dc1601e4ad9c819191401b4ab450da32',
                     pilot_results=str(OUTPUT / 'experiment/runs' / protocol['pilot_id']),
                     library_workbook_id='libfile_2dce9fd3a0988191bdce4776d518f581',
                     library_replacement_status='blocked: current saved workbook unavailable; preserve newer annotations before replacement')
        try:
            deadline = time.monotonic() + options.max_wait_seconds
            last_task = None
            while True:
                finished, follow_up = baseline_finished(protocol)
                if finished:
                    break
                current = follow_up.get('current_task')
                publish(state, status='waiting_for_baseline', baseline_current_task=current,
                        baseline_attempts_started=follow_up.get('retry_launches_this_follow_up'))
                if current != last_task:
                    print(f'{now()} Baseline active: {current}', flush=True)
                    last_task = current
                if time.monotonic() >= deadline:
                    raise TimeoutError('Baseline wait deadline reached; no VM control attempted')
                time.sleep(30)
            # Reports capture the finished baseline before any further experiment.
            publish(state, status='refreshing_finished_baseline', baseline_current_task=None)
            refresh()
            if options.refresh_only:
                publish(state, status='baseline_reports_refreshed', pilot_status='not_launched_by_refresh_only')
                return 0
            pilot_output = OUTPUT / 'experiment/runs' / protocol['pilot_id']
            pilot_state_path = pilot_output / 'pilot-state.json'
            pilot_state = load(pilot_state_path) if pilot_state_path.exists() else {}
            if pilot_state.get('status') != 'finished':
                command = [PYTHON, ROOT / 'scripts/run_os_harm_pilot.py', '--manifest', PROTOCOL, '--run']
                if pilot_state_path.exists():
                    command.append('--resume')
                publish(state, status='attempting_separate_pilot')
                code = run_logged(command, 'pilot-execution')
                state['pilot_exit_code'] = code
                pilot_state = load(pilot_state_path) if pilot_state_path.exists() else {}
                # Refresh baseline and any terminal pilot evidence even after a blocked launch.
                refresh()
                if code:
                    publish(state, status='blocked_pilot_runtime', pilot_status=pilot_state.get('status', 'not_launched'),
                            action_required='Run this same continuation command from a VM-capable Mac Terminal. Do not stop an active runner.',
                            error=pilot_state.get('error', 'Pilot prerequisite or process inspection failed; see pilot-execution.log'))
                    return code
            publish(state, status='pilot_finished_needs_evidence_audit', pilot_status='finished',
                    next_action='Audit all twelve cells against frozen endpoints, refresh paired comparisons and report. Do not claim efficacy from custom judge alone.')
            return 0
        except BaseException as exc:
            publish(state, status='blocked_workflow', error=f'{type(exc).__name__}: {exc}')
            raise


if __name__ == '__main__':
    raise SystemExit(main())
