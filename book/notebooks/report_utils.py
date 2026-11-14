"""Helpers shared by the Milestone 1 report notebooks.

Every table and figure of the book is computed at build time from the small
aggregated files in data/m1/, written by the code repository's
src/export_report_data.py. Nothing here reads the raw corpus.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.io as pio
from IPython.display import HTML, display
from plotly.offline import get_plotlyjs_version

pio.templates.default = "plotly_white"
COLORS = px.colors.qualitative.Safe
FINAL_SOURCE = {"success", "pdf_only", "no_tex"}  # final version states with a folder on disk
REF_OK = {"success", "empty"}


def data_dir() -> Path:
    """data/m1/ of the book, found from the notebook's working directory."""
    for folder in [Path.cwd(), *Path.cwd().parents]:
        candidate = folder / "data" / "m1"
        if (candidate / "meta.json").is_file():
            return candidate
    raise FileNotFoundError(
        "data/m1/meta.json not found. Run src/export_report_data.py of the code repository "
        "with every member's data folder and --out <book>/data/m1 before building the book.")


def load(name: str) -> pd.DataFrame:
    """One exported table, e.g. load('papers') reads data/m1/papers.csv.gz."""
    folder = data_dir()
    for path in (folder / f"{name}.csv.gz", folder / f"{name}.csv"):
        if path.is_file():
            return pd.read_csv(path, low_memory=False)
    raise FileNotFoundError(f"{name}.csv(.gz) is missing in {folder}: re-run export_report_data.py")


def meta() -> dict:
    return json.loads((data_dir() / "meta.json").read_text(encoding="utf-8"))


def members() -> pd.DataFrame:
    """members.csv without the 'group' row."""
    df = load("members")
    return df[df["member"] != "group"].reset_index(drop=True)


def group() -> pd.Series:
    df = load("members")
    return df[df["member"] == "group"].iloc[0]


def metric(name: str) -> float:
    """A metric of corpus_statistics.py for the whole group: the "group" block
    (every member's papers read together), or the sum over the members for
    an export made before that block existed (count metrics only)."""
    stats = load("statistics")
    rows = stats[stats["metric"] == name]
    if (rows["member"] == "group").any():
        rows = rows[rows["member"] == "group"]
    values = pd.to_numeric(rows["value"], errors="coerce")
    return float(values.sum()) if values.notna().any() else float("nan")


def one_machine() -> bool:
    """True when the ranges were crawled one after the other on one runtime."""
    return bool(meta().get("one_machine"))


def mib(n) -> float:
    return n / 2**20


def human_bytes(n) -> str:
    if n is None or pd.isna(n):
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024


def human_seconds(s) -> str:
    if s is None or pd.isna(s):
        return "–"
    if s < 120:
        return f"{s:,.1f} s"
    if s < 7200:
        return f"{s / 60:,.1f} phút"
    return f"{s / 3600:,.2f} giờ"


def pct(x) -> str:
    return "–" if x is None or pd.isna(x) else f"{100 * x:.2f}%"


def table(df: pd.DataFrame, caption: str | None = None) -> None:
    """Show a DataFrame as a plain HTML table, without the index."""
    html = df.to_html(index=False, escape=True, border=0, classes="dataframe")
    if caption:
        html = f"<p><em>{caption}</em></p>" + html
    display(HTML(html))


def show(fig) -> None:
    """Interactive plotly figure, embedded as HTML that loads plotly.js from its CDN.

    Jupyter Book 2 inserts outputs into the page with JavaScript, so the
    <script src> of plotly's own to_html() may not have loaded when its
    Plotly.newPlot() call runs. Here the call waits for the library."""
    fig.update_layout(margin=dict(l=40, r=20, t=60, b=40), font=dict(size=13))
    div = f"plotly-{uuid.uuid4().hex}"
    height = fig.layout.height or 450
    cdn = f"https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"
    display(HTML(f"""<div id="{div}" style="width:100%;height:{height}px"></div>
<script>(function () {{
  var spec = {fig.to_json()};
  function draw() {{
    Plotly.newPlot("{div}", spec.data, spec.layout, {{responsive: true, displaylogo: false}});
  }}
  if (window.Plotly) {{ draw(); return; }}
  var lib = document.querySelector("script[data-plotly-cdn]");
  if (!lib) {{
    lib = document.createElement("script");
    lib.src = "{cdn}";
    lib.setAttribute("data-plotly-cdn", "");
    document.head.appendChild(lib);
  }}
  lib.addEventListener("load", draw);
}})();</script>"""))


def provenance() -> None:
    """Where the numbers of a chapter come from."""
    m = meta()
    commit = (m.get("code_commit") or "không rõ")[:10]
    display(HTML(f"<p><small>Nguồn số liệu: <code>data/m1/</code> xuất lúc {m['generated_at']} "
                 f"từ {len(m['members'])} thư mục dữ liệu ({', '.join(m['members'])}); "
                 f"commit code <code>{commit}</code>.</small></p>"))
