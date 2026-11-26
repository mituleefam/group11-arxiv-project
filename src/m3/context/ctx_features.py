"""Milestone 3, context experiment - step 3: context features of every (entry, candidate) pair.

    python src/m3/context/ctx_features.py --work WORK [--ctx WORK/context] [--seed 11]
                                          [--strict --m1 "M1_DIR/corpus/*/metadata.json" --out FOLDER]

Inputs: WORK/features.parquet (the pairs and the metadata features of
features.py), WORK/references_clean.parquet (the candidates, M1),
CTX/mentions.parquet (contexts.py) and CTX/links.parquet (corpus_links.py).
Writes CTX/features.parquet: the same rows and columns as WORK/features.parquet
plus the CTX_FEATURES below, and a copy of WORK/splits.json, so that
modeling/pairs.Pairs(CTX) reads it as it reads WORK.

No feature reads the BibTeX entry (title, authors, year, venue, id, DOI): the
entry is known only by the text around its citations. The candidate side is
M1's reference: title, authors, year, and its description by the rest of the
corpus.

Texts of an entry (all its mentions joined): `local` (the words just before
and after each citation), `sent` (the sentences), `all` (sentences,
neighbouring sentences and headings). Text of a candidate: its title, and its
description = local + sentences of every OTHER paper's entry linked to it by a
sure rule (links.parquet); the paper of the entry itself is always taken out
(its own links subtracted), so a paper never describes its own candidates.

--strict (a leak check): the description of a candidate only takes the
contexts of the papers submitted BEFORE the paper of the entry (M1's
submission_date) that share NO author with it (same normalised full name), so
neither a later paper nor the same authors' own wording describe the
candidates. Written to --out (with a copy of mentions / links not needed:
train_ctx.py and select_meta_ctx.py only read features.parquet and
splits.json).

TF-IDF (unigrams and bigrams, sublinear tf) and LSA (TruncatedSVD of it) are
fitted on the contexts of the whole corpus (25 000 papers) and the candidate
titles - our own data, no pretrained model.

Feature groups (CTX_GROUPS):
  ctx_title   cosine of local / sent / all with the title (TF-IDF), sent with
              the title (LSA); rank and gap to the best of the group
  ctx_desc    cosine of local / sent with the description (TF-IDF), sent with
              the description (LSA), rank and gap; n_desc = log(1 + other
              papers that cite the candidate), desc_missing
  ctx_author  the first author's surname in the local words / in the context,
              number of the candidate's surnames in the context
  ctx_year    candidate age (paper's year - candidate's year), its rank
  ctx_entry   (same for every candidate of the entry, for the interactions)
              log(1 + mentions), mean keys per citing element, position of
              the first mention, its section type, share in figures,
              ctx_missing, number of candidates
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import scipy.sparse as sp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contexts import CITE, MATH, OTHER, SECTION_TYPES  # noqa: E402

CTX_GROUPS = {
    "ctx_title": ["sim_local_title", "sim_sent_title", "sim_all_title", "lsa_sent_title", "rank_sent_title",
                  "gap_sent_title", "rank_lsa_title"],
    "ctx_desc": ["sim_local_desc", "sim_sent_desc", "lsa_sent_desc", "rank_sent_desc", "gap_sent_desc",
                 "rank_lsa_desc", "n_desc", "desc_missing"],
    "ctx_author": ["fa_in_local", "fa_in_ctx", "n_auth_in_ctx"],
    "ctx_year": ["cand_age", "rank_age"],
    "ctx_entry": ["n_mentions", "n_keys_mean", "first_position", "section", "in_figure", "ctx_missing", "n_cand"],
}
CTX_FEATURES = [f for fs in CTX_GROUPS.values() for f in fs]
# compact set (analyze_ctx.py, cross-validation on train): the features whose permutation costs MRR@5, one or two
# per signal; out: the year group, the LSA ranks, the title LSA, the entry's section / position / figure share
CTX_COMPACT = ["sim_local_title", "sim_all_title", "rank_sent_title", "gap_sent_title",
               "sim_local_desc", "sim_sent_desc", "gap_sent_desc", "lsa_sent_desc", "n_desc",
               "fa_in_local", "n_auth_in_ctx", "n_keys_mean", "n_mentions"]
CTX_SETS = {"full": CTX_FEATURES, "compact": CTX_COMPACT}
SECTIONS = ["none", "front", "abstract", "intro", "related", "method", "experiment", "discussion", "appendix",
            "other"]
assert {s for s, _ in SECTION_TYPES} <= set(SECTIONS)
MARKERS = (CITE, OTHER, MATH)


def strip_markers(s: pd.Series) -> pd.Series:
    for m in MARKERS:
        s = s.str.replace(m, " ", regex=False)
    return s


def aggregate_mentions(path: Path) -> pd.DataFrame:
    m = pq.read_table(path).to_pandas()
    m["loc"] = m["left"] + " " + m["right"]
    first = m.sort_values("position").drop_duplicates(["arxiv_id", "citation_key"])
    g = m.groupby(["arxiv_id", "citation_key"], sort=False)
    agg = pd.DataFrame({
        "local": g["loc"].agg(" ".join), "sent": g["sentence"].agg(" ".join),
        "nb": g["neighbours"].agg(" ".join), "heads": g["headings"].agg(lambda x: " ".join(dict.fromkeys(x))),
        "n_mentions": g.size(), "n_keys_mean": g["n_keys"].mean(), "first_position": g["position"].min(),
        "in_figure": g["kind"].agg(lambda x: float(np.mean(x == "figure")))}).reset_index()
    agg = agg.merge(first[["arxiv_id", "citation_key", "section"]], on=["arxiv_id", "citation_key"])
    for c in ("local", "sent", "nb"):
        agg[c] = strip_markers(agg[c])
    agg["all"] = agg["sent"] + " " + agg["nb"] + " " + agg["heads"]
    return agg


def row_normalize(X):
    n = np.sqrt(np.asarray(X.multiply(X).sum(axis=1)).ravel()) if sp.issparse(X) else np.linalg.norm(X, axis=1)
    n[n == 0] = 1
    return (sp.diags(1 / n) @ X) if sp.issparse(X) else X / n[:, None]


def group_rank(values: np.ndarray, gidx: np.ndarray, descending: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Rank inside each group (1 = best; ties share the best rank) and gap to the group's best."""
    v = np.where(np.isnan(values), -np.inf, values) if descending else np.where(np.isnan(values), np.inf, -values)
    order = np.lexsort((-v, gidx))
    rank = np.empty(len(v))
    g_sorted, v_sorted = gidx[order], v[order]
    start = np.r_[0, np.flatnonzero(np.diff(g_sorted)) + 1]
    pos = np.arange(len(v)) - np.repeat(start, np.diff(np.r_[start, len(v)]))
    # ties: same value as the previous one in the group -> same rank
    same = np.r_[False, (np.diff(v_sorted) == 0) & (np.diff(g_sorted) == 0)]
    r = pos + 1.0
    for i in np.flatnonzero(same):
        r[i] = r[i - 1]
    rank[order] = r
    best = np.full(gidx.max() + 1, -np.inf)
    np.maximum.at(best, gidx, v)
    gap = best[gidx] - v
    gap[~np.isfinite(gap)] = np.nan
    return rank, gap


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--ctx", type=Path, help="default WORK/context")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--lsa-dims", type=int, default=200)
    p.add_argument("--strict", action="store_true", help="descriptions from earlier papers without shared authors")
    p.add_argument("--m1", help="glob of M1's metadata.json files (for --strict)")
    p.add_argument("--out", type=Path, help="output folder (default CTX)")
    args = p.parse_args(argv)
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    ctx = args.ctx or args.work / "context"
    out_dir = args.out or ctx
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    def log(msg):
        print(f"[{time.perf_counter() - t0:5.0f} s] {msg}", flush=True)

    F = pq.read_table(args.work / "features.parquet").to_pandas()
    log(f"{len(F)} pairs, {F.group.nunique()} entries")
    agg = aggregate_mentions(ctx / "mentions.parquet")
    log(f"{len(agg)} cited entries in {agg.arxiv_id.nunique()} papers")
    refs = pq.read_table(args.work / "references_clean.parquet",
                         columns=["ref_arxiv_id", "title_clean", "authors_last", "year"]).to_pandas()
    refs = refs.drop_duplicates("ref_arxiv_id").set_index("ref_arxiv_id")
    links = pq.read_table(ctx / "links.parquet").to_pandas()

    # --- TF-IDF and LSA on the corpus's contexts + the candidate titles -------------------------------------
    titles = refs["title_clean"].fillna("")
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_df=0.3, max_features=400_000, sublinear_tf=True,
                          stop_words="english", dtype=np.float32)
    vec.fit(pd.concat([agg["sent"], titles], ignore_index=True))
    log(f"TF-IDF vocabulary {len(vec.vocabulary_)}")
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(len(agg), size=min(300_000, len(agg)), replace=False)
    svd = TruncatedSVD(args.lsa_dims, random_state=args.seed).fit(vec.transform(agg["sent"].iloc[sample]))
    log(f"LSA {args.lsa_dims} dims, explained variance {svd.explained_variance_ratio_.sum():.3f}")

    # entries of the labelled papers (the rows of F)
    ent = F[["arxiv_id", "citation_key"]].drop_duplicates().merge(agg, on=["arxiv_id", "citation_key"], how="left")
    ent["ctx_missing"] = ent["sent"].isna().astype(np.float32)
    for c in ("local", "sent", "all", "nb"):
        ent[c] = ent[c].fillna("")
    E_local = row_normalize(vec.transform(ent["local"]))
    E_sent = row_normalize(vec.transform(ent["sent"]))
    E_all = row_normalize(vec.transform(ent["all"]))
    L_sent = row_normalize(svd.transform(vec.transform(ent["sent"])))
    ent_ix = {k: i for i, k in enumerate(zip(ent.arxiv_id, ent.citation_key))}
    log(f"{len(ent)} entries vectorised ({int(ent.ctx_missing.sum())} without context)")

    # candidates: titles and descriptions (sum of the linked contexts of the whole corpus)
    cands = pd.Index(F["ref_arxiv_id"].unique())
    cand_ix = {c: i for i, c in enumerate(cands)}
    T = row_normalize(vec.transform(titles.reindex(cands).fillna("")))
    L_title = row_normalize(svd.transform(vec.transform(titles.reindex(cands).fillna(""))))
    lk = links[links.ref_arxiv_id.isin(cand_ix)].merge(agg[["arxiv_id", "citation_key", "local", "sent"]],
                                                        on=["arxiv_id", "citation_key"])
    V = row_normalize(vec.transform(lk["local"] + " " + lk["sent"]))  # one unit vector per linked context
    VL = svd.transform(V)
    ci = lk["ref_arxiv_id"].map(cand_ix).to_numpy()
    A = sp.csr_matrix((np.ones(len(lk), np.float32), (ci, np.arange(len(lk)))), shape=(len(cands), len(lk)))
    D = (A @ V).tocsr()  # candidate x vocabulary, sum of the contexts
    DL = A @ VL
    n_desc_all = np.asarray(A.sum(axis=1)).ravel()
    log(f"{len(lk)} linked contexts describe {int((n_desc_all > 0).sum())} of {len(cands)} candidates")
    lk_by_paper = {a: g for a, g in lk.reset_index(drop=True).reset_index().groupby("arxiv_id")}
    if args.strict:  # submission date and authors of every paper of the corpus
        import glob
        from text_clean import normalize_text  # noqa: E402 (src/m3 is on the path through contexts)
        date, auth = {}, {}
        for f in glob.glob(args.m1):
            md = json.loads(Path(f).read_text(encoding="utf-8"))
            date[md["arxiv_id"]] = md.get("submission_date") or ""
            auth[md["arxiv_id"]] = frozenset(normalize_text(x) for x in md.get("authors") or [] if x)
        q_ids = lk["arxiv_id"].to_numpy()
        q_date = np.array([date.get(q, "") for q in q_ids])
        q_auth = [auth.get(q, frozenset()) for q in q_ids]
        A = A.tocsr()
        log(f"--strict: metadata of {len(date)} papers")

    # --- pair features, paper by paper -------------------------------------------------------------------------
    out = {f: np.full(len(F), np.nan, np.float32) for f in CTX_FEATURES}
    F_e = np.array([ent_ix[k] for k in zip(F.arxiv_id, F.citation_key)])
    F_c = F["ref_arxiv_id"].map(cand_ix).to_numpy()
    by_paper = F.groupby("arxiv_id", sort=False).indices
    for n, (a, rows) in enumerate(by_paper.items()):
        e_u, e_inv = np.unique(F_e[rows], return_inverse=True)
        c_u, c_inv = np.unique(F_c[rows], return_inverse=True)
        Tc = T[c_u]
        out["sim_local_title"][rows] = (E_local[e_u] @ Tc.T).toarray()[e_inv, c_inv]
        out["sim_sent_title"][rows] = (E_sent[e_u] @ Tc.T).toarray()[e_inv, c_inv]
        out["sim_all_title"][rows] = (E_all[e_u] @ Tc.T).toarray()[e_inv, c_inv]
        out["lsa_sent_title"][rows] = (L_sent[e_u] @ L_title[c_u].T)[e_inv, c_inv]
        # description without this paper's own links
        if args.strict:  # only earlier papers sharing no author (the paper itself is never earlier than itself)
            sub = A[c_u].tocsr()
            d0, a0 = date.get(a, ""), auth.get(a, frozenset())
            keep = (q_date[sub.indices] < d0) & (q_date[sub.indices] != "") & (d0 != "")
            for k in np.flatnonzero(keep):
                if q_auth[sub.indices[k]] & a0:
                    keep[k] = False
            sub.data = sub.data * keep
            Dc, DLc, nd = (sub @ V).tocsr(), sub @ VL, np.asarray(sub.sum(axis=1)).ravel()
            own = None
        else:
            Dc, DLc, nd = D[c_u], DL[c_u].copy(), n_desc_all[c_u].copy()
            own = lk_by_paper.get(a)
        if own is not None:  # subtract the paper's own linked contexts (sparse: candidates x own contexts @ V)
            j = np.searchsorted(c_u, own["ref_arxiv_id"].map(cand_ix).to_numpy())
            ok = (j < len(c_u)) & (c_u[np.minimum(j, len(c_u) - 1)] == own["ref_arxiv_id"].map(cand_ix).to_numpy())
            idx = own["index"].to_numpy()[ok]
            Own = sp.csr_matrix((np.ones(ok.sum(), np.float32), (j[ok], np.arange(ok.sum()))),
                                shape=(len(c_u), ok.sum()))
            Dc = (Dc - Own @ V[idx]).tocsr()
            DLc -= Own @ VL[idx]
            nd -= np.asarray(Own.sum(axis=1)).ravel()
        Dn = np.sqrt(np.asarray(Dc.multiply(Dc).sum(axis=1)).ravel())
        has = (nd > 0.5) & (Dn > 1e-4)
        Dc = (sp.diags(1 / np.where(has, Dn, 1)) @ Dc).tocsr()
        DLn = np.linalg.norm(DLc, axis=1)
        DLc = DLc / np.where(DLn > 1e-9, DLn, 1)[:, None]
        for name, E in (("sim_local_desc", E_local), ("sim_sent_desc", E_sent)):
            s = (E[e_u] @ Dc.T).toarray()
            s[:, ~has] = np.nan
            out[name][rows] = s[e_inv, c_inv]
        s = L_sent[e_u] @ DLc.T
        s[:, ~has] = np.nan
        out["lsa_sent_desc"][rows] = s[e_inv, c_inv]
        out["n_desc"][rows] = np.log1p(np.maximum(nd, 0))[c_inv]
        out["desc_missing"][rows] = (~has)[c_inv].astype(np.float32)
        if (n + 1) % 500 == 0:
            log(f"  {n + 1}/{len(by_paper)} papers")

    # authors: candidate surnames in the context words
    loc_words = [set(x.split()) for x in ent["local"]]
    ctx_words = [set((s + " " + b).split()) for s, b in zip(ent["sent"], ent["nb"])]
    surnames = refs["authors_last"].reindex(cands)
    cand_sn = [[w for w in (x if x is not None else []) if w and len(w) >= 3] for x in surnames]
    fa = [x[0] if x else None for x in cand_sn]
    out["fa_in_local"] = np.array([float(fa[c] is not None and fa[c] in loc_words[e]) for e, c in zip(F_e, F_c)],
                                  np.float32)
    out["fa_in_ctx"] = np.array([float(fa[c] is not None and fa[c] in ctx_words[e]) for e, c in zip(F_e, F_c)],
                                np.float32)
    out["n_auth_in_ctx"] = np.array([sum(w in ctx_words[e] for w in cand_sn[c]) for e, c in zip(F_e, F_c)],
                                    np.float32)
    # year: the paper's (from its id) minus the candidate's
    paper_year = F["arxiv_id"].str.slice(0, 4).map(lambda s: 2000 + int(s[:2]) + (int(s[2:]) - 0.5) / 12
                                                   if s.isdigit() else np.nan).to_numpy(np.float32)
    cy = refs["year"].reindex(cands).to_numpy(np.float32)
    out["cand_age"] = paper_year - cy[F_c]
    # entry-level
    for f in ("n_mentions", "n_keys_mean", "first_position", "in_figure", "ctx_missing"):
        out[f] = ent[f].to_numpy(np.float32)[F_e]
    out["n_mentions"] = np.log1p(np.nan_to_num(out["n_mentions"]))
    sec = ent["section"].fillna("none").map({s: i for i, s in enumerate(SECTIONS)}).to_numpy(np.float32)
    out["section"] = sec[F_e]
    gidx = pd.factorize(F["group"])[0]
    out["n_cand"] = np.bincount(gidx)[gidx].astype(np.float32)
    # ranks and gaps inside the entry's group
    for src, rk, gp in (("sim_sent_title", "rank_sent_title", "gap_sent_title"),
                        ("sim_sent_desc", "rank_sent_desc", "gap_sent_desc")):
        out[rk], out[gp] = group_rank(out[src], gidx)
    out["rank_lsa_title"], _ = group_rank(out["lsa_sent_title"], gidx)
    out["rank_lsa_desc"], _ = group_rank(out["lsa_sent_desc"], gidx)
    out["rank_age"], _ = group_rank(out["cand_age"], gidx, descending=False)
    for f in CTX_FEATURES:
        out[f] = out[f].astype(np.float32)
    log("features computed")

    t = pa.Table.from_pandas(F, preserve_index=False)
    for f in CTX_FEATURES:
        t = t.append_column(f, pa.array(out[f]))
    pq.write_table(t, out_dir / "features.parquet", compression="zstd")
    shutil.copy(args.work / "splits.json", out_dir / "splits.json")
    (out_dir / "features_ctx.json").write_text(json.dumps({"groups": CTX_GROUPS, "sections": SECTIONS,
                                                           "strict": args.strict,
                                                       "rows": len(F), "tfidf_vocabulary": len(vec.vocabulary_),
                                                       "lsa_dims": args.lsa_dims}, indent=1), encoding="utf-8")
    log(f"-> {out_dir / 'features.parquet'} ({len(CTX_FEATURES)} context features)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
