"""Milestone 3 - run the matching pipeline step by step; a step whose script
is not written yet is skipped.

    python src/m3/pipeline_m3.py --tables M2_TABLES --data-dir M1_DATA_DIR [...] --work WORK_DIR
                                 [--labels LABELS.csv] [--members 5] [--seed 11] [--from STEP]

Steps, in order:
  data        m3_data.py      entries_clean.parquet, references_clean.parquet (Member 1)
  candidates  candidates.py   candidates.parquet, paper_stats.csv             (Member 1)
  split       split.py        splits.json, needs --labels                     (Member 1)
  features    features.py     features.parquet                                (Member 3)
  model       modeling/train.py     models chosen by cross-validation          (Member 4)
  predict     modeling/predict.py   predictions.parquet                        (Member 4)
  evaluate    modeling/evaluate.py  MRR@5 on valid                             (Member 4)

Convention for the later steps: each script is run as
    python src/m3/<script> --work WORK_DIR
and reads / writes its files in WORK_DIR (labels: WORK_DIR/labels.csv unless
--labels is given). --from STEP starts at that step and reuses the files of
the steps before it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = ["data", "candidates", "split", "features", "model", "predict", "evaluate"]
SCRIPTS = {"data": "m3_data.py", "candidates": "candidates.py", "split": "split.py", "features": "features.py",
           "model": "modeling/train.py", "predict": "modeling/predict.py", "evaluate": "modeling/evaluate.py"}


def command(step: str, args) -> list[str] | None:
    script = HERE / SCRIPTS[step]
    if not script.is_file():
        return None
    base = [sys.executable, str(script)]
    if step == "data":
        return base + ["--tables", *args.tables, "--data-dir", *args.data_dir, "--out", str(args.work)]
    if step == "candidates":
        return base + ["--work", str(args.work)]
    if step == "split":
        labels = args.labels or next((p for p in (args.work / "labels.csv", args.work / "labels.parquet") if p.is_file()), None)
        if labels is None:
            return []
        return base + ["--labels", str(labels), "--members", str(args.members), "--seed", str(args.seed),
                       "--stats", str(args.work / "paper_stats.csv"), "--out", str(args.work / "splits.json")]
    return base + ["--work", str(args.work)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Milestone 3 pipeline")
    p.add_argument("--tables", nargs="+", required=True)
    p.add_argument("--data-dir", nargs="+", required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--labels", type=Path)
    p.add_argument("--members", type=int, default=5)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--from", dest="start", choices=STEPS, default="data")
    args = p.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    for step in STEPS[STEPS.index(args.start):]:
        cmd = command(step, args)
        if cmd is None:
            print(f"[{step}] skipped: src/m3/{SCRIPTS[step]} is not written yet")
            continue
        if cmd == []:
            print(f"[{step}] skipped: no label file (--labels, or labels.csv in the work folder)")
            continue
        print(f"[{step}] {' '.join(Path(c).name if i < 2 else c for i, c in enumerate(cmd))}", flush=True)
        started = time.perf_counter()
        code = subprocess.run(cmd).returncode
        print(f"[{step}] exit code {code} in {time.perf_counter() - started:.1f} s", flush=True)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
