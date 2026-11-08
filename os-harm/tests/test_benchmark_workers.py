import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.benchmark_workers import dispatch, prepare_workspace
from scripts.run_os_harm_local import manifest, resolve_vm_paths


class WorkerTests(unittest.TestCase):
    def test_upstream_duplicate_is_scheduled_only_once(self):
        jobs = manifest(["misbehavior", "injection", "misuse"])
        keys = [(job["suite"], job["domain"], job["id"]) for job in jobs]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 150)

    def test_tasks_overlap_without_duplicates_or_reusing_busy_vm(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, seen = set(), []

        def execute(worker, index, job, stop):
            with lock:
                self.assertNotIn(worker, active)
                active.add(worker)
                seen.append(job)
            if index <= 2:
                barrier.wait(timeout=3)
            with lock:
                active.remove(worker)

        dispatch(list(range(20)), ["vm0", "vm1"], execute)
        self.assertEqual(sorted(seen), list(range(20)))

    def test_worker_failure_cancels_sibling(self):
        barrier = threading.Barrier(2)
        cancelled = threading.Event()

        def execute(worker, index, job, stop):
            barrier.wait(timeout=3)
            if worker == "bad":
                raise RuntimeError("test failure")
            if stop.wait(3):
                cancelled.set()

        with self.assertRaisesRegex(RuntimeError, "test failure"):
            dispatch([1, 2], ["bad", "other"], execute)
        self.assertTrue(cancelled.is_set())

    def test_workspaces_do_not_share_mutable_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            for name in ("assets", "evaluation_examples"):
                (source / name).mkdir(parents=True)
                (source / name / "fixture").write_text("original")
            first = prepare_workspace(source, root / "one")
            second = prepare_workspace(source, root / "two")
            (first / "assets/fixture").write_text("modified")
            self.assertEqual((second / "assets/fixture").read_text(), "original")
            self.assertEqual((source / "assets/fixture").read_text(), "original")

    def test_same_vm_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            vm = Path(tmp) / "one.vmx"
            vm.touch()
            alias = Path(tmp) / "two.vmx"
            alias.symlink_to(vm)
            args = SimpleNamespace(vm_paths=[str(vm), str(alias)], workers=2)
            with self.assertRaisesRegex(ValueError, "distinct"):
                resolve_vm_paths(args)


if __name__ == "__main__":
    unittest.main()
