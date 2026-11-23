"""Milestone 3, Member 1 - train / valid / test split by publication.

    python src/m3/split.py --labels LABELS.csv --members 5 --seed 11 --out WORK_DIR/splits.json
                           [--stats WORK_DIR/paper_stats.csv] [--per-range]

The statement (Data Modeling): test and validation each hold 1 manually
labelled and 1 automatically matched publication *per member*, "so a group
of k members puts k manually labeled and k automatically matched publications
into each of the two sets"; training holds all the other labelled
publications. With 5 members: test 5 + 5, valid 5 + 5.

LABELS is the label file of Member 2 (CSV or Parquet) with at least the
columns arxiv_id and source ("manual" or "auto"). A publication with any
manual label counts as manual. With --stats, publications without any
reference (n = 0, every entry "no match") are not drawn for test or valid,
since they have no candidate to rank.

With --per-range, each member's ID range (select_manual.GROUP_RANGES; the
range of a publication comes from its arXiv id) gives exactly 1 manual and 1
automatic publication to test and 1 + 1 to valid, so that every member's data
is in both sets; --members is then the number of ranges found in the labels.
Without it, the 2k publications of each kind are drawn from the whole group.

Output splits.json: {"train": [...], "valid": [...], "test": [...], "seed": 11,
"members": 5, "counts": {...}}; the same seed gives the same split.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

import pyarrow.parquet as pq


def read_rows(path: Path) -> list[dict]:
    if path.suffix == ".parquet":
        return pq.read_table(path).to_pylist()
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def paper_sources(labels: list[dict]) -> dict[str, str]:
    """arxiv_id -> "manual" or "auto" (manual wins when a paper has both)."""
    out = {}
    for r in labels:
        pid, src = str(r["arxiv_id"]), str(r.get("source") or "").strip().lower()
        if src not in ("manual", "auto"):
            raise ValueError(f"label of {pid}: source must be manual or auto, not {r.get('source')!r}")
        if out.get(pid) != "manual":
            out[pid] = src
    return out


def make_split_per_range(sources: dict[str, str], seed: int, rankable: set[str] | None = None) -> dict:
    """1 manual + 1 automatic publication of every ID range to test, 1 + 1 to valid, the rest to train."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from select_manual import GROUP_RANGES, range_of
    result = {"train": [], "valid": [], "test": []}
    ranges = [name for name in GROUP_RANGES if any(range_of(p) == name for p in sources)]
    for name in ranges:
        for kind in ("manual", "auto"):
            pool = sorted(p for p, s in sources.items() if s == kind and range_of(p) == name)
            drawable = [p for p in pool if rankable is None or p in rankable]
            if len(drawable) < 2:
                raise ValueError(f"{name}: {len(drawable)} {kind} publications can go to test/valid, 2 are needed")
            picked = random.Random(f"{seed}:{name}:{kind}").sample(drawable, 2)
            result["test"].append(picked[0])
            result["valid"].append(picked[1])
            result["train"] += [p for p in pool if p not in picked]
    outside = sorted(p for p in sources if not any(p in v for v in result.values()))
    if outside:
        raise ValueError(f"{len(outside)} publications outside the group's ranges, e.g. {outside[:3]}")
    for k in result:
        result[k].sort()
    result["seed"], result["members"], result["per_range"] = seed, len(ranges), True
    result["counts"] = {part: {kind: sum(1 for p in result[part] if sources[p] == kind) for kind in ("manual", "auto")}
                        for part in ("train", "valid", "test")}
    return result


def make_split(sources: dict[str, str], members: int, seed: int, rankable: set[str] | None = None) -> dict:
    rng = random.Random(seed)
    result = {"train": [], "valid": [], "test": []}
    for kind in ("manual", "auto"):
        pool = sorted(p for p, s in sources.items() if s == kind)
        drawable = [p for p in pool if rankable is None or p in rankable]
        if len(drawable) < 2 * members:
            raise ValueError(f"{len(drawable)} {kind} publications can go to test/valid, "
                             f"{2 * members} are needed ({members} each for {members} members)")
        picked = rng.sample(drawable, 2 * members)
        result["test"] += picked[:members]
        result["valid"] += picked[members:]
        result["train"] += [p for p in pool if p not in picked]
    for k in result:
        result[k].sort()
    result["seed"], result["members"] = seed, members
    result["counts"] = {part: {kind: sum(1 for p in result[part] if sources[p] == kind) for kind in ("manual", "auto")}
                        for part in ("train", "valid", "test")}
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Split the labelled publications into train, valid and test")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--members", type=int, default=5, help="group size k: k manual + k auto papers per set")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--stats", type=Path, help="paper_stats.csv: keep papers with n = 0 out of test/valid")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--per-range", action="store_true",
                   help="1 manual + 1 auto publication of every member's ID range in test and in valid")
    args = p.parse_args(argv)
    sources = paper_sources(read_rows(args.labels))
    rankable = None
    if args.stats:
        rankable = {r["arxiv_id"] for r in read_rows(args.stats) if int(r["n"]) > 0}
    result = (make_split_per_range(sources, args.seed, rankable) if args.per_range
              else make_split(sources, args.members, args.seed, rankable))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result["counts"]), "->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
