"""Incremental backups of a crawler data folder, and their restore.

On Colab the crawl runs fastest with --data-dir on the runtime's own disk
(/content/...): Google Drive is slow with many small files (each rerun of a
5000-ID range spent 9 to 13 minutes only checking which metadata.json files
were already there, and 45 minutes re-reading the references), and the
downloads the crawler writes and then deletes there go to Drive's Trash. That
disk is lost when the runtime ends, so chunked_crawl.py calls save() after
every step: it writes one .tar.gz with only the files added or changed since
the previous archive (and the list of files deleted since), first on the
runtime's disk, then copied into the backup folder on Drive under its final
name. restore() replays the archives in order, so a new runtime gets the data
folder back as it was after the last saved step.

    python src/snapshot.py save    --data-dir /content/g11/m1 --backup-dir /content/drive/MyDrive/g11/m1
    python src/snapshot.py restore --data-dir /content/g11/m1 --backup-dir /content/drive/MyDrive/g11/m1

Archives are named 0001_<label>.tar.gz, 0002_<label>.tar.gz... Each holds
__snapshot__.json (its number, the files deleted since the previous archive,
and "full" when it holds the whole folder). Files that only exist while a
crawler runs are left out: work/downloads/ (partial downloads), *.tmp files
and folders (files being written) and work/crawler.lock. The data folder keeps
work/snapshot_index.json, the size and modification time of every file the
archives hold, so save() finds the changes without reading archives back.

Call save() only while no crawler writes into the folder (chunked_crawl.py
calls it between steps). Nothing in the backup folder is ever overwritten or
deleted, so nothing ends up in Drive's Trash.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

from common import atomic_write_json, now_iso

INDEX = "work/snapshot_index.json"
META = "__snapshot__.json"
ARCHIVE_NAME = re.compile(r"^(\d{4,})(?:_[^/\\]*)?\.tar\.gz$")
SKIPPED_FILES = {"work/crawler.lock", INDEX}


def _skipped(rel: PurePosixPath) -> bool:
    """Files that exist only while a crawler runs, and the index itself."""
    if rel.parts[:2] == ("work", "downloads") or rel.as_posix() in SKIPPED_FILES:
        return True
    return any(part.endswith(".tmp") for part in rel.parts)


def scan(data_dir: Path) -> dict[str, list[int]]:
    """Every file to back up: relative path -> [size, modification time in ns]."""
    files: dict[str, list[int]] = {}
    for directory, folders, names in os.walk(data_dir):
        here = PurePosixPath(Path(directory).relative_to(data_dir).as_posix())
        folders[:] = sorted(f for f in folders if not _skipped(here / f))
        for name in names:
            rel = here / name
            if not _skipped(rel):
                info = os.stat(Path(directory) / name)
                files[rel.as_posix()] = [info.st_size, info.st_mtime_ns]
    return files


def load_index(data_dir: Path) -> dict:
    try:
        index = json.loads((Path(data_dir) / INDEX).read_text(encoding="utf-8"))
        if isinstance(index, dict) and isinstance(index.get("files"), dict):
            return index
    except (OSError, ValueError):
        pass
    return {"seq": 0, "files": {}}


def archives(backup_dir: Path) -> list[tuple[int, Path]]:
    """(number, path) of the finished archives of `backup_dir`, in order
    (a copy still under way ends with .part and is left out)."""
    found = []
    if Path(backup_dir).is_dir():
        for path in Path(backup_dir).iterdir():
            match = ARCHIVE_NAME.match(path.name)
            if match and path.is_file():
                found.append((int(match[1]), path))
    return sorted(found)


def save(data_dir: Path, backup_dir: Path, label: str = "") -> Path | None:
    """Write the changes since the previous archive as the next archive of
    `backup_dir`. Returns its path, or None when nothing changed. Raises
    RuntimeError when `backup_dir` has archives this folder was not restored
    from (restore() first, or use another backup folder)."""
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    index = load_index(data_dir)
    present = archives(backup_dir)
    last = present[-1][0] if present else 0
    if last > index["seq"]:
        raise RuntimeError(f"{backup_dir} has archive {last}, newer than what {data_dir} holds "
                           f"(archive {index['seq']}): restore it first, or use another backup folder")
    current = scan(data_dir)
    # Archives missing from the backup (deleted, or a copy that never reached Drive):
    # this one then holds the whole folder, so that a restore does not depend on them.
    numbers = {seq for seq, _ in present}
    full = any(seq not in numbers for seq in range(1, index["seq"] + 1))
    changed = sorted(current) if full else sorted(p for p, st in current.items()
                                                    if index["files"].get(p) != st)
    removed = [] if full else sorted(p for p in index["files"] if p not in current)
    if not changed and not removed:
        return None

    seq = index["seq"] + 1
    label = re.sub(r"[^\w.-]+", "-", label).strip("-")
    name = f"{seq:04d}_{label}.tar.gz" if label else f"{seq:04d}.tar.gz"
    local = data_dir.parent / f".{data_dir.name}.{name}"
    meta = json.dumps({"seq": seq, "label": label, "created": now_iso(), "full": full,
                       "files": len(changed), "removed": removed}, ensure_ascii=False).encode()
    try:
        with tarfile.open(local, "w:gz", compresslevel=6) as tar:
            info = tarfile.TarInfo(META)
            info.size, info.mtime = len(meta), int(time.time())
            tar.addfile(info, io.BytesIO(meta))
            for rel in changed:
                tar.add(data_dir / rel, arcname=rel, recursive=False)
        backup_dir.mkdir(parents=True, exist_ok=True)
        part = backup_dir / (name + ".part")
        shutil.copyfile(local, part)
        os.replace(part, backup_dir / name)
    finally:
        local.unlink(missing_ok=True)
    atomic_write_json(data_dir / INDEX, {"seq": seq, "files": current})
    return backup_dir / name


def _extract(tar: tarfile.TarFile, data_dir: Path) -> list[str]:
    """Extract every file of `tar` but the metadata; returns their paths."""
    members = [m for m in tar.getmembers() if m.name != META]
    for member in members:
        rel = PurePosixPath(member.name)
        if rel.is_absolute() or ".." in rel.parts or not (member.isfile() or member.isdir()):
            raise RuntimeError(f"unexpected member in {tar.name}: {member.name!r}")
    try:
        tar.extractall(data_dir, members=members, filter="data")
    except TypeError:  # Python without extraction filters: members were checked above
        tar.extractall(data_dir, members=members)
    return [m.name for m in members if m.isfile()]


def restore(data_dir: Path, backup_dir: Path) -> int:
    """Replay into `data_dir` the archives of `backup_dir` it does not hold
    yet. Returns how many were applied. Raises RuntimeError when `data_dir`
    already holds files that do not come from this backup."""
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    index = load_index(data_dir)
    todo = [(seq, path) for seq, path in archives(backup_dir) if seq > index["seq"]]
    if not todo:
        return 0
    if index["seq"] == 0 and data_dir.is_dir() and scan(data_dir):
        raise RuntimeError(f"{data_dir} already holds files that do not come from {backup_dir}: "
                           f"restore into an empty folder")
    data_dir.mkdir(parents=True, exist_ok=True)
    expected = index["seq"] + 1
    for seq, path in todo:
        with tarfile.open(path, "r:gz") as tar:
            meta = json.loads(tar.extractfile(META).read().decode("utf-8"))
            if seq != expected and not meta.get("full"):
                print(f"[snapshot] archives {expected}..{seq - 1} are missing in {backup_dir}: "
                      f"the crawler does again what they held", flush=True)
            names = set(_extract(tar, data_dir))
        gone = (p for p in scan(data_dir) if p not in names) if meta.get("full") else meta.get("removed", [])
        for rel in list(gone):
            (data_dir / rel).unlink(missing_ok=True)
        expected = seq + 1
    atomic_write_json(data_dir / INDEX, {"seq": todo[-1][0], "files": scan(data_dir)})
    return len(todo)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Back up a data folder in incremental archives, or restore it")
    parser.add_argument("action", choices=("save", "restore"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--label", default="", help="save: short name in the archive's file name")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    if args.action == "save":
        path = save(args.data_dir, args.backup_dir, args.label)
        print(f"Saved {path} in {time.perf_counter() - started:.1f} s" if path else "Nothing changed")
    else:
        count = restore(args.data_dir, args.backup_dir)
        print(f"Restored {count} archives in {time.perf_counter() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
