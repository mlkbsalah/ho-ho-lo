#!/usr/bin/env python3
"""Prepare a root Git index while preserving local nested-repository metadata.

Respects ignore rules, includes files already tracked by the two source repos,
and rejects any selected file containing a real API key from env.conf.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "vmware_vm_data",
             ".uv-cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def git(*args, input=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=input, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def forbidden(path):
    p = Path(path)
    return (any(part in SKIP_DIRS for part in p.parts) or p.name == "env.conf" or
            p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example" or
            p.name in {"credentials.json", "client_secrets.json", "api_key.py"})


def secrets():
    values = []
    for line in (ROOT / "env.conf").read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and ("KEY" in name.upper() or "TOKEN" in name.upper()):
            value = value.strip().strip("\"'")
            if len(value) >= 16:
                values.append(value.encode())
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    options = parser.parse_args()
    candidates = []
    for base, dirs, files in os.walk(ROOT, followlinks=False):
        symlink_dirs = [d for d in dirs if (Path(base) / d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in symlink_dirs]
        for name in files + symlink_dirs:
            path = str((Path(base) / name).relative_to(ROOT))
            if not forbidden(path):
                candidates.append(path)
    payload = b"\0".join(os.fsencode(p) for p in candidates) + b"\0"
    checked = git("check-ignore", "--stdin", "-z", input=payload, check=False)
    if checked.returncode not in [0, 1]:
        raise RuntimeError(checked.stderr.decode())
    ignored = set(os.fsdecode(p) for p in checked.stdout.split(b"\0") if p)
    selected = set(candidates) - ignored
    for child in ["os-harm", "hai-agents-python"]:
        process = subprocess.run(["git", "-C", str(ROOT / child), "ls-files", "-z"],
                                 capture_output=True, check=True)
        for name in process.stdout.split(b"\0"):
            if name:
                path = child + "/" + os.fsdecode(name)
                if not forbidden(path) and (ROOT / path).exists():
                    selected.add(path)
    selected = sorted(selected)
    keys = secrets()
    secret_files, oversize, sizes = [], [], {}
    for name in selected:
        path = ROOT / name
        size = path.lstat().st_size
        sizes[name] = size
        if size >= 100 * 1024 * 1024:
            oversize.append(name)
        if not path.is_symlink():
            carry = b""
            found = False
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    data = carry + block
                    if any(key in data for key in keys):
                        found = True
                        break
                    carry = data[-max((len(k) for k in keys), default=1):]
            if found:
                secret_files.append(name)
    if secret_files or oversize:
        raise RuntimeError(json.dumps({"files_containing_real_keys": secret_files,
                                       "files_exceeding_github_limit": oversize}))
    manifest = {"files": selected, "file_count": len(selected), "bytes": sum(sizes.values()),
                "ignored_count": len(ignored), "largest_files": sorted(sizes.items(), key=lambda x: -x[1])[:8],
                "real_key_matches": 0, "nested_sources": "Included as files; original nested .git directories preserved"}
    Path("/tmp/ho-ho-lo-publish-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}), flush=True)
    if not options.stage:
        return
    if git("ls-files", "-z").stdout:
        raise RuntimeError("Index is not empty; refusing to replace existing staging")
    # Git plumbing records nested source files rather than embedded gitlinks.
    regular = [p for p in selected if not (ROOT / p).is_symlink()]
    regular_payload = "\n".join(regular).encode() + b"\n"
    if any("\n" in p for p in regular):
        raise ValueError("Unexpected newline in a filename")
    hashes = git("hash-object", "-w", "--stdin-paths", input=regular_payload).stdout.decode().splitlines()
    if len(hashes) != len(regular):
        raise RuntimeError("Unexpected Git hash count")
    entries = []
    for name, blob in zip(regular, hashes):
        mode = "100755" if (ROOT / name).stat().st_mode & stat.S_IXUSR else "100644"
        entries.append(f"{mode} {blob}\t{name}".encode() + b"\0")
    for name in selected:
        path = ROOT / name
        if path.is_symlink():
            blob = git("hash-object", "-w", "--stdin", input=os.fsencode(os.readlink(path))).stdout.decode().strip()
            entries.append(f"120000 {blob}\t{name}".encode() + b"\0")
    git("update-index", "--add", "-z", "--index-info", input=b"".join(entries))
    print(f"Staged {len(selected)} files without credentials, local VM, environments or nested .git metadata.", flush=True)


if __name__ == "__main__":
    main()
