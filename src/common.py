"""Shared helpers: data layout, status codes, atomic writes, the JSONL paper
manifest and append-only CSV logs.

Every file is either written atomically (temporary file + rename) or appended
line by line and flushed, so a crash or a Colab disconnect never leaves a
half-written file that looks complete.
"""

from __future__ import annotations

import csv
import json
import os
import threading
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
    immediately so nothing is lost if the run stops."""

    def __init__(self, path: Path, fields: list[str]):
        self.path = Path(path)
        self.fields = fields
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()

    def write(self, **row) -> None:
        with self._lock:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fields, extrasaction="ignore")
                writer.writerow(row)
