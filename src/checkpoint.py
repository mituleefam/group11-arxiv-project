"""Member 2 - Checkpoint and resume for source downloads.

The state of every version of a paper lives in corpus/<yymm-id>/versions.json:

    {"arxiv_id": "2604.05535",
     "versions": {
        "v1": {"status": "success", "source_format": "tar.gz",
               "downloaded_bytes": 812345, "original_bytes": 2456789,
               "kept_bytes": 98765, "kept_files": ["main.tex", "sec/intro.tex", "refs.bib"],
               "attempts": 1, "attempts_total": 1, "finished_at": "2026-10-17T09:30:00Z", ...},
        "v2": {"status": "pdf_only", ...}}}

A version is *final* when its status is success, pdf_only, no_tex or
not_found: downloading it again would not change anything, so a re-run skips
it and never overwrites it. A failed version (or one missing from the file,
for example because the run was interrupted) is processed again.

The file is rewritten atomically (common.atomic_write_json), and it is only
updated *after* the version folder has been moved into place, so after a crash
the file never claims a version that is not on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import NOT_FOUND, PDF_ONLY, SUCCESS, atomic_write_json, now_iso
from extractor import NO_TEX

VERSIONS_FILE = "versions.json"

# Statuses that end the work on a version for good.
FINAL_STATUSES = {SUCCESS, PDF_ONLY, NO_TEX, NOT_FOUND}
# Final statuses that come with a version folder (empty for pdf_only / no_tex).
STATUSES_WITH_FOLDER = {SUCCESS, PDF_ONLY, NO_TEX}


class VersionStore:
    """Read and update corpus/<yymm-id>/versions.json for one paper.

        store = VersionStore(layout.paper_dir("2604.05535"), "2604.05535")
        if not store.is_final("v1"):
            ...download and extract...
            store.update("v1", {"status": "success", ...})
    """

    def __init__(self, paper_dir: Path, arxiv_id: str):
        self.paper_dir = Path(paper_dir)
        self.arxiv_id = arxiv_id
        self.path = self.paper_dir / VERSIONS_FILE
        self.data = self._load()

    def _load(self) -> dict:
        """Read the file; a missing or unreadable file means no version is done."""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("versions"), dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {"arxiv_id": self.arxiv_id, "versions": {}}

    def get(self, version: str) -> dict | None:
        """The saved record of `version` ("v1", "v2"...), or None."""
        return self.data["versions"].get(version)

    def is_final(self, version: str) -> bool:
        """True when `version` is finished and its folder is still on disk,
        so it must be skipped (and never overwritten)."""
        record = self.get(version)
        if record is None or record.get("status") not in FINAL_STATUSES:
            return False
        if record["status"] in STATUSES_WITH_FOLDER:
            return (self.paper_dir / version).is_dir()
        return True

    def previous_attempts(self, version: str) -> int:
        """Download attempts already spent on `version` in earlier runs."""
        record = self.get(version) or {}
        return int(record.get("attempts_total", 0))

    def update(self, version: str, record: dict) -> None:
        """Save the record of `version` and rewrite the file atomically.
        `attempts_total` counts the attempts of every run so far."""
        record = dict(record)
        record["attempts_total"] = self.previous_attempts(version) + int(record.get("attempts", 0))
        record.setdefault("finished_at", now_iso())
        self.data["versions"][version] = record
        atomic_write_json(self.path, self.data)


__all__ = ["VERSIONS_FILE", "FINAL_STATUSES", "VersionStore"]
