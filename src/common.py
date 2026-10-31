"""Shared helpers: data layout, status codes, atomic writes, the JSONL paper
manifest, append-only CSV logs and the lock that keeps one crawler per data
folder.

Every file is either written atomically (temporary file + rename) or appended
line by line and flushed, so a crash or a Colab disconnect never leaves a
half-written file that looks complete.
"""

from __future__ import annotations

import csv
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Status codes shared by every module (section VIII of the team plan).
SUCCESS = "success"
NOT_FOUND = "not_found"
API_ERROR = "api_error"
PDF_ONLY = "pdf_only"
FAILED = "failed"


def now_iso() -> str:
    """Current UTC time in ISO 8601, e.g. '2026-10-15T08:30:00Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def folder_name(arxiv_id: str) -> str:
    """Folder name of a paper: '2605.12338' -> '2605-12338'."""
    return arxiv_id.replace(".", "-")


class DataLayout:
    """Where the working data lives. The data root stays outside the code
    repository (for example on Google Drive)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.corpus = self.root / "corpus"  # one folder per paper
        self.work = self.root / "work"      # manifest and other working files
        self.logs = self.root / "logs"      # CSV logs used for the report
        self.manifest = self.work / "paper_manifest.jsonl"

    def paper_dir(self, arxiv_id: str) -> Path:
        return self.corpus / folder_name(arxiv_id)

    def create(self) -> None:
        for folder in (self.corpus, self.work, self.logs):
            folder.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data) -> None:
    """Write JSON so that readers see either the old file or the complete new
    one, never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Manifest:
    """Append-only JSONL file with one record per arXiv ID.

    A later line for the same ID overrides an earlier one, so updating a
    paper only means appending a new line. A broken last line (crash while
    writing) is ignored when reading.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def load(self) -> dict[str, dict]:
        records: dict[str, dict] = {}
        if not self.path.exists():
            return records
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partially written line after a crash
                records[record["arxiv_id"]] = record
        return records


class CsvLog:
    """Append-only CSV log. The header is written once; every row is flushed
    immediately so nothing is lost if the run stops.

    If the file was written by an older version of the code with other
    columns, it is rewritten once with the new header (old rows keep their
    values, new columns stay empty, dropped columns are kept at the end), so
    rows are never appended under a header that does not match them."""

    def __init__(self, path: Path, fields: list[str]):
        self.path = Path(path)
        self.fields = list(fields)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.fields).writeheader()
            return
        with open(self.path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != self.fields:
            self._upgrade(header)

    def _upgrade(self, old_header: list[str]) -> None:
        """Rewrite the file under self.fields plus any old column it lacks."""
        self.fields += [name for name in old_header if name and name not in self.fields]
        with open(self.path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def write(self, **row) -> None:
        with self._lock:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fields, extrasaction="ignore")
                writer.writerow(row)


class RunLock:
    """work/crawler.lock: keeps a second crawler away from the same data folder.

    Two crawlers on one folder (a second Colab runtime started while the
    first one still runs, two members using the same folder...) overwrite
    each other's downloads and double the requests sent to arXiv. The
    running crawler rewrites the lock every `beat` seconds; a lock that has
    not been rewritten for `stale` seconds is left over from a crash or a
    dead Colab runtime, and is taken over.
    """

    def __init__(self, path: Path, *, stale: float = 180.0, beat: float = 30.0):
        self.path = Path(path)
        self.stale, self.beat = stale, beat
        self.owner = f"{socket.gethostname()} pid {os.getpid()}"
        self._started = now_iso()
        self._stop = threading.Event()

    def acquire(self, force: bool = False) -> None:
        """Take the lock, or raise RuntimeError if another crawler holds it."""
        other = self._read()
        if other and not force:
            age = time.time() - float(other.get("heartbeat", 0))
            if age < self.stale:
                raise RuntimeError(
                    f"another crawler is using this data folder ({other.get('owner')}, "
                    f"last seen {max(age, 0):.0f} s ago). Two crawlers on one folder overwrite "
                    f"each other's files: stop the other one or use another --data-dir. If it "
                    f"has really stopped (e.g. its Colab runtime died), wait "
                    f"{self.stale - max(age, 0):.0f} s or add --force.")
        self._write()
        threading.Thread(target=self._keep_alive, name="run-lock", daemon=True).start()

    def release(self) -> None:
        self._stop.set()
        other = self._read()
        if other and other.get("owner") == self.owner:
            try:
                self.path.unlink()
            except OSError:
                pass

    def _keep_alive(self) -> None:
        while not self._stop.wait(self.beat):
            try:
                self._write()
            except OSError:
                pass  # e.g. Drive briefly unavailable: try again at the next beat

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _write(self) -> None:
        atomic_write_json(self.path, {"owner": self.owner, "started": self._started,
                                      "heartbeat": time.time(), "heartbeat_utc": now_iso()})
