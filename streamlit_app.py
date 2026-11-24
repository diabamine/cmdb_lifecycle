"""Streamlit front end. Rendering only: every decision comes from the cmdb package."""
import csv
import io
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from cmdb.catalog import load_catalog
from cmdb.compare import compare_references, comparison_summary, load_component_catalog
from cmdb.matching.matcher import build_index, load_aliases, match_one, match_rows, save_alias, search_catalog
from cmdb.metrics import catalog_coverage, matching_metrics

CATALOG_PATH = Path("data/master_catalog.csv")
ALIASES_PATH = Path("aliases.csv")
RUN_LOG_PATH = Path("data/metrics_log.jsonl")
INPUT_COLUMNS = ["vendor", "product", "version", "fw_or_pkg", "ci_type"]

st.set_page_config(page_title="CMDB lifecycle", layout="wide")


@st.cache_data
def get_catalog(path):
    return load_catalog(path)


@st.cache_resource
def get_index(path):
    # The index is opaque to the UI: cache it and pass it on.
    return build_index(get_catalog(path))


def read_input_csv(uploaded):
    """Read an uploaded CSV as dicts; a CMDB extract may lack vendor, so default it to ''."""
    text = uploaded.getvalue().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    for row in rows:
        row.setdefault("vendor", "")
    return rows


def to_csv_bytes(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


def run_match(rows, catalog):
    results = match_rows(rows, catalog, aliases=load_aliases(ALIASES_PATH))
    st.session_state["results"] = results
    return results


def tab_search(catalog):
    text = st.text_input("Search by product, alias or vendor")
    categories = st.multiselect(
        "Select columns", 
        [col for col in catalog[0].keys()], 
        ["product_id","vendor", "product", "source", "category"]
    )
    if not text or text.strip() == "":
        st.dataframe(pd.DataFrame(catalog)[categories].drop_duplicates("product_id"))
    else:
        hits = search_catalog(catalog, text, limit=50)
        if not hits:
            st.info("No product found.")
            return
        products = pd.DataFrame(hits)[categories].drop_duplicates("product_id")
        st.dataframe(products.drop(columns="product_id"), hide_index=True)
        chosen = st.selectbox("Show all cycles of", products["product_id"], format_func=lambda pid: products.set_index("product_id").loc[pid, "product"])
        cycles = [row for row in catalog if row["product_id"] == chosen]
        st.dataframe(pd.DataFrame(cycles).drop(columns=["id", "product_id", "aliases"]), hide_index=True)


def show_trace(trace):
    with st.expander("How it was matched"):
        for step, detail in trace.items():
            st.write(f"**{step}**: {detail}")


def tab_match(catalog, index):
    mode = st.radio("Input", ["Single product", "CSV upload"], horizontal=True)
    if mode == "Single product":
        cols = st.columns(3)
        query = {
            "vendor": cols[0].text_input("Vendor"),
            "product": cols[1].text_input("Product"),
            "version": cols[2].text_input("Version"),
        }
        if not query["product"]:
            return
        trace = {}
        result = match_one(query, index, trace)
        st.dataframe(pd.DataFrame([result]), hide_index=True)
        show_trace(trace)
        return

    uploaded = st.file_uploader("CSV with product, version (optional: vendor, fw_or_pkg, ci_type)", type="csv", key="match_csv")
    if uploaded and st.button("Match"):
        run_match(read_input_csv(uploaded), catalog)
    results = st.session_state.get("results")
    if not results:
        return
    st.dataframe(pd.DataFrame(results), hide_index=True)
    st.download_button("Download enriched CSV", to_csv_bytes(results), "enriched.csv", "text/csv")
    review_queue(results)


def review_queue(results):
    """Let a human confirm a suggested match once; aliases.csv makes it stick on the next run."""
    pending = [r for r in results if r.get("review") and r.get("product_id")]
    st.subheader(f"Review queue ({len(pending)})")
    for n, row in enumerate(pending[:20]):
        label = f"{row['vendor']} {row['product']} {row['version']} -> {row['matched_product']} ({row['match_confidence']})"
        left, right = st.columns([4, 1])
        left.write(f"{label}: {row['review']}")
        if right.button("Save alias", key=f"alias_{n}"):
            save_alias(ALIASES_PATH, row["vendor"], row["product"], row["product_id"])
            st.success("Alias saved, re-run the match to apply it.")


def tab_metrics(catalog):
    st.subheader("Catalog coverage")
    st.dataframe(pd.DataFrame(catalog_coverage(catalog)), hide_index=True)

    results = st.session_state.get("results")
    if results:
        metrics = matching_metrics(results)
        cols = st.columns(3)
        cols[0].metric("Match rate", f"{metrics['match_rate']:.1f}%")
        cols[1].metric("Auto-accepted", f"{metrics['auto_accept_rate']:.1f}%")
        cols[2].metric("Review", f"{metrics['review_queue_rate']:.1f}%")
        st.bar_chart(pd.Series(metrics["by_match_method"], name="rows"))
    else:
        st.caption("Run a CSV match to see matching metrics.")

    if RUN_LOG_PATH.exists():
        try:
            trend = pd.read_json(RUN_LOG_PATH, lines=True)
            st.line_chart(trend.select_dtypes("number"))
        except (ValueError, OSError):
            st.caption("The run log could not be read.")


def tab_compare(catalog):
    uploads = st.file_uploader("Component catalog CSVs to compare with the master catalog", type="csv", accept_multiple_files=True)
    extra = st.file_uploader("Input CSV (default: the last match from the Match tab)", type="csv", key="compare_csv")
    rows = read_input_csv(extra) if extra else st.session_state.get("results")
    if not uploads or not rows:
        st.info("Upload at least one component catalog and have input rows.")
        return

    references = {"master": catalog}
    for upload in uploads:
        # load_component_catalog takes a path, so spill the upload to a temp file.
        with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
            tmp.write(upload.getvalue())
            tmp.flush()
            references[upload.name] = load_component_catalog(Path(tmp.name))

    compared = compare_references(rows, references)
    st.dataframe(pd.DataFrame(comparison_summary(compared)), hide_index=True)
    disagree = [row for row in compared if row.get("agree") in ("partial", "conflict")]
    st.subheader(f"Rows where references disagree ({len(disagree)})")
    st.dataframe(pd.DataFrame(disagree), hide_index=True)


if not CATALOG_PATH.exists():
    st.error("Master catalog not found. Run `python -m cmdb.ingest` then `python -m cmdb.catalog`.")
    st.stop()

catalog = get_catalog(CATALOG_PATH)
index = get_index(CATALOG_PATH)
search_tab, match_tab, metrics_tab, compare_tab = st.tabs(["Search catalog", "Match", "Metrics", "Compare"])
with search_tab:
    tab_search(catalog)
with match_tab:
    tab_match(catalog, index)
with metrics_tab:
    tab_metrics(catalog)
with compare_tab:
    tab_compare(catalog)
