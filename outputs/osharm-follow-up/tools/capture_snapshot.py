#!/usr/bin/env python3
"""Read recorded baseline artifacts and atomically save a reporting snapshot.

Never opens the VM, takes the benchmark lock, imports runner code, or writes to
the baseline directory. A mutable snapshot is explicitly labelled as such.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def read_json(path, optional=False):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if optional:
            return None, None
        raise
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def capture(run, output):
    started = datetime.now(timezone.utc)
    state_before, state_hash_before = read_json(run / 'follow-up-state.json', True)
    state_before = state_before or {}
    manifest, manifest_hash = read_json(run / 'manifest.json')
    config, config_hash = read_json(run / 'config.json')
    keys = ['/'.join(j[k] for k in ('suite', 'domain', 'id')) for j in manifest]
    if len(keys) != 150 or len(set(keys)) != 150:
        raise ValueError('Expected exactly 150 unique original baseline tasks')
    summaries, hashes, archives, failures = {}, {}, [], []
    for key in keys:
        path = run / key / 'summary.json'
        try:
            value, digest = read_json(path, True)
        except json.JSONDecodeError as exc:
            failures.append({'path': str(path), 'error': str(exc)})
            continue
        if value is not None:
            summaries[key], hashes[key] = value, digest
    for path in sorted((run / 'attempts').glob('*/*/*/attempt-*/summary.json')):
        try:
            value, digest = read_json(path)
            archives.append({'path': str(path), 'summary': value, 'sha256': digest})
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            failures.append({'path': str(path), 'error': str(exc)})
    state_after, state_hash_after = read_json(run / 'follow-up-state.json', True)
    state_after = state_after or {}
    changed_summaries = []
    for key in keys:
        path = run / key / 'summary.json'
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            digest = None
        if digest != hashes.get(key):
            changed_summaries.append(key)
    active_jobs = sorted({state.get('current_task') for state in [state_before, state_after]
                          if state.get('status') in ('running', 'launching')
                          and state.get('current_task') in keys
                          and summaries.get(state['current_task'], {}).get('status') != 'trajectory_complete'})
    ended = datetime.now(timezone.utc)
    changed = state_hash_before != state_hash_after or bool(changed_summaries) or bool(failures)
    running = state_after.get('status') in ('running', 'launching')
    counts = Counter(summaries.get(k, {}).get('status', 'running' if k in active_jobs else 'missing') for k in keys)
    snapshot = {'root': str(run), 'captured_at': ended.isoformat(),
                'captured_at_paris': ended.astimezone(ZoneInfo('Europe/Paris')).isoformat(),
                'capture_started_at': started.isoformat(), 'manifest': manifest,
                'config': config, 'summaries': summaries, 'archives': archives,
                'active_jobs': active_jobs, 'follow_up': state_after,
                'state_before': state_before, 'state_after': state_after,
                'snapshot_consistency': {'baseline_retry_active': running,
                    'state_changed_during_capture': state_hash_before != state_hash_after,
                    'summary_changed_during_capture': changed_summaries,
                    'read_failures': failures, 'mutable_snapshot': running or changed,
                    'label': 'Mutable snapshot; baseline retry active' if running else
                             'Files changed during capture' if changed else 'Stable recorded-file snapshot',
                    'method': 'Read-only, no benchmark lock; state and summary hashes checked before/after'},
                'source_sha256': {'manifest': manifest_hash, 'config': config_hash,
                    'state_before': state_hash_before, 'state_after': state_hash_after,
                    'summaries': hashes}, 'counts': dict(counts)}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.json.tmp')
    with temp.open('w') as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, output)
    print(json.dumps({'snapshot': str(output), 'captured_at': snapshot['captured_at'],
                      'captured_at_paris': snapshot['captured_at_paris'],
                      'counts': dict(counts), 'active_jobs': active_jobs,
                      'consistency': snapshot['snapshot_consistency'], 'archives': len(archives)}))


if __name__ == '__main__':
    base = Path(__file__).resolve().parents[1]
    repo = base.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=repo / 'os-harm/results/os-harm-hcompany/full-run')
    parser.add_argument('--output', type=Path, default=base / 'latest_snapshot.json')
    args = parser.parse_args()
    capture(args.run.resolve(), args.output.resolve())
