"""Worker scheduling and filesystem isolation for local OS-Harm runs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid


def write_json(path, value):
    """Publish a whole JSON document so other workers cannot read half a write."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_workspace(source, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Copies are intentional: original injection setup writes temporary files
    # inside assets. Symlinks would leave the workers sharing those mutations.
    for name in ("assets", "evaluation_examples"):
        shutil.copytree(Path(source) / name, directory / name, dirs_exist_ok=True)
    (directory / "cache").mkdir(exist_ok=True)
    return directory


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def wait_for_process(process, timeout, stop):
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if stop.is_set():
            stop_process(process)
            return "interrupted"
        if time.monotonic() >= deadline:
            stop_process(process)
            return "timeout"
        stop.wait(0.2)
    return "exited"


def dispatch(jobs, workers, execute):
    """One sequential loop per VM, pulling unique tasks from a shared queue."""
    pending = queue.Queue()
    for index, job in enumerate(jobs, 1):
        pending.put((index, job))
    stop = threading.Event()

    def loop(worker):
        try:
            while not stop.is_set():
                try:
                    index, job = pending.get_nowait()
                except queue.Empty:
                    return
                if not stop.is_set():
                    execute(worker, index, job, stop)
        except BaseException:
            stop.set()
            raise

    pool = ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="os-harm")
    futures = [pool.submit(loop, worker) for worker in workers]
    try:
        for future in as_completed(futures):
            future.result()
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
