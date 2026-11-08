"""Resume only missing/recoverable cases using the original OS-Harm runner.

One follow-up attempt per case, with the benchmark lock held throughout.
Original config, manifest, completed trajectories, and unsupported fixtures remain intact.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock
from scripts import run_os_harm_local as runner
from scripts.benchmark_workers import write_json


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ip-failures-only', action='store_true',
                        help='Retry only the eleven VM IP discovery failures')
    options = parser.parse_args()
    output = ROOT / 'results/os-harm-hcompany/full-run'
    state_file = output / 'follow-up-state.json'
    os.chdir(ROOT)
    os.environ['PATH'] = '/Applications/VMware Fusion.app/Contents/Library:' + os.environ['PATH']
    with FileLock(str(ROOT / '.holo-benchmark.lock'), timeout=0):
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if state.get('status') in ('running', 'launching'):
            raise RuntimeError('Persistent follow-up state reports an unfinished launch; inspect it before resuming')
        original_pid = int((output / 'runner.pid').read_text())
        try:
            os.kill(original_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError('Original runner PID still exists; refusing concurrent VM control')
        config_bytes = (output / 'config.json').read_bytes()
        config = json.loads(config_bytes)
        for name, expected in config['source_sha256'].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Original runner source changed: ' + name)
        jobs = json.loads((output / 'manifest.json').read_text())
        already = set(state.get('retried_tasks', []))
        selected = []
        for job in jobs:
            key = '/'.join(job[k] for k in ('suite', 'domain', 'id'))
            p = output / key / 'summary.json'
            s = json.loads(p.read_text()) if p.exists() else {}
            recoverable = not s or s.get('status') in ('timeout', 'interrupted', 'worker_failed') or (
                s.get('status') == 'error' and ('Unable to get the IP address' in s.get('error', '')
                                              or 'No parsed tool calls' in s.get('error', '')
                                              or runner.is_timeout_result(s)))
            if options.ip_failures_only:
                recoverable = s.get('status') == 'error' and 'Unable to get the IP address' in s.get('error', '')
            if recoverable and key not in already:
                selected.append((key, job))
        state.update(checked_at=now(), retry_candidates=[key for key, _ in selected],
                     retry_scope='ip_failures_only' if options.ip_failures_only else 'all_recoverable')
        checks = []
        for args in [['list'], ['getGuestIPAddress', config['path_to_vm']]]:
            p = subprocess.run(['vmrun', '-T', 'fusion'] + args, capture_output=True, text=True, timeout=30)
            checks.append(dict(command=args[0], returncode=p.returncode, stdout=p.stdout.strip(), stderr=p.stderr.strip()))
            if p.returncode:
                state.update(status='blocked_vmware', vmware_checks=checks)
                write_json(state_file, state)
                print(json.dumps({'status': state['status'], 'retry_candidates': len(selected), 'checks': checks}), flush=True)
                return 2
        from scripts.run_holo_vm import configure_inference
        args = argparse.Namespace(**config)
        args.output = output
        configure_inference(args)
        judge_client = None
        if not args.skip_judge:
            from judge.responses_judge import make_client
            judge_client = make_client()
            judge_client.models.retrieve(args.judge_model)
        workers = json.loads((output / 'workers.json').read_text())
        if len(workers) != 1 or workers[0]['vm_path'] != config['path_to_vm']:
            raise RuntimeError('Expected the saved single-VM worker configuration')
        state.update(status='running', pid=os.getpid(), started_at=now(), vmware_checks=checks)
        write_json(state_file, state)
        try:
            for index, (key, job) in enumerate(selected, 1):
                # Persist before launch; a later invocation never launches this case twice.
                state = json.loads(state_file.read_text())
                if key in state.get('retried_tasks', []):
                    continue
                out = output / key
                if (out / 'summary.json').exists() and json.loads((out / 'summary.json').read_text()).get('status') == 'trajectory_complete':
                    continue
                state.update(current_task=key, retried_tasks=state.get('retried_tasks', []) + [key])
                write_json(state_file, state)
                if out.exists() and any(out.iterdir()):
                    archived = runner.archive_attempt(args, out)
                    state.setdefault('follow_up_archives', {})[key] = str(archived)
                state['retry_launches_this_follow_up'] = state.get('retry_launches_this_follow_up', 0) + 1
                write_json(state_file, state)
                runner.execute_attempt(args, workers[0], index, job, threading.Event(), len(selected))
                summary = json.loads((out / 'summary.json').read_text())
                history = output / 'attempts' / key
                summary.update(attempt_number=1 + len(list(history.glob('attempt-*'))),
                               attempt_history=str(history.relative_to(output)))
                write_json(out / 'summary.json', summary)
                runner.record_case(args, out, len(jobs), judge_client)
                # A repeated shared VM failure blocks the batch; don't repeat it on every task.
                if 'Unable to get the IP address' in summary.get('error', ''):
                    state.update(status='blocked_vm_ip', finished_at=now())
                    write_json(state_file, state)
                    return 2
            state.update(status='retries_finished', finished_at=now(), current_task=None,
                         spreadsheet_status='refresh_pending')
            write_json(state_file, state)
        except BaseException as exc:
            state.update(status='blocked_runner', error=f'{type(exc).__name__}: {exc}', finished_at=now())
            write_json(state_file, state)
            raise
        finally:
            assert (output / 'config.json').read_bytes() == config_bytes
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
