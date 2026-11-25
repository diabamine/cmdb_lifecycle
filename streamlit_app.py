"""CMDB lifecycle: match a CMDB file against a lifecycle catalog and see what is end of life.

The app only reads files. It knows nothing about how catalogs are built (`python -m cmdb.ingest`, `python -m cmdb.catalog`).
Wording and table shaping live in cmdb/view.py, file reading in cmdb/inputs.py, matching in cmdb/matching.

Page layout
  sidebar   your CMDB file and your catalogs
  top       the catalog to look at, and four headline numbers
  tabs      Results (every row, click one to see why) | Coverage (score per catalog) | Look up (type one item)
"""
import hashlib
from pathlib import Path

import pandas as pd
import streamlit as st

from cmdb import view
from cmdb.inputs import load_catalog, parse_catalog, parse_cmdb
from cmdb.matching.lookup import lookup
from cmdb.matching.matcher import build_index, match_one, match_rows
from cmdb.metrics import catalog_coverage, lifecycle_metrics, matching_metrics

CATALOG_PATH = Path("data/master_catalog.csv")      # default catalog, used when the file exists
SAMPLE_PATH = Path("cmdb_extract.csv")              # default CMDB file, used until one is uploaded
DEFAULT = "master"

# technical columns, shown on request next to the plain ones
TECHNICAL = ["match_method", "cycle_match", "matched_cycle", "matched_source", "support_end", "extended_end",
             "latest_in_cycle", "newer_cycle", "dq_flags"]
PERCENT = st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d")

st.set_page_config(page_title="CMDB lifecycle", layout="wide")


# ---------------------------------------------------------------- loading and matching (cached)

def digest(data):
    return hashlib.md5(data).hexdigest()


@st.cache_data
def read_default_catalog(mtime):
    return load_catalog(CATALOG_PATH)


@st.cache_data(show_spinner=False)
def match_cached(_rows, _catalog, input_key, catalog_key):
    # underscore arguments are not hashed: the two keys stand in for them
    return match_rows(_rows, _catalog)


@st.cache_resource(show_spinner=False)
def index_cached(_catalog, catalog_key):
    return build_index(_catalog)


def csv_bytes(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------- sidebar: the two inputs

def load_cmdb():
    st.sidebar.header("Your data")
    upload = st.sidebar.file_uploader("CMDB file (CSV)", type="csv",
                                      help="Needs a `product` column. Better results with `version`, `vendor`, `fw_or_pkg` (a package name) and `ci_type`.")
    if upload:
        name, data = upload.name, upload.getvalue()
    elif SAMPLE_PATH.exists():
        name, data = SAMPLE_PATH.name, SAMPLE_PATH.read_bytes()
        st.sidebar.caption(f"Showing the sample file `{name}`. Upload yours above.")
    else:
        st.info("Start by uploading your CMDB file (CSV) in the sidebar.")
        st.stop()
    rows = parse_cmdb(data)
    if not rows or "product" not in rows[0]:
        st.sidebar.error(f"`{name}` has no `product` column.")
        st.stop()
    st.sidebar.success(f"{len(rows):,} rows in `{name}`")
    return name, rows, digest(data)


def load_catalogs():
    upload = st.sidebar.file_uploader("Add your own catalog(s) (CSV)", type="csv", accept_multiple_files=True,
                                      help="Columns: `vendor`, `product`, `version`, and optionally `eol`, `release_date`, `aliases`. "
                                           "It is matched next to the default catalog so you can compare them.")
    catalogs = {}
    if CATALOG_PATH.exists():
        catalogs[DEFAULT] = (read_default_catalog(CATALOG_PATH.stat().st_mtime), str(CATALOG_PATH.stat().st_mtime))
    for file in upload:
        data = file.getvalue()
        catalogs[Path(file.name).stem] = (parse_catalog(data, file.name), digest(data))
    if not catalogs:
        st.info(f"Upload a catalog (CSV) in the sidebar. The app uses `{CATALOG_PATH}` automatically when that file exists.")
        st.stop()
    empty = [name for name, (catalog, _) in catalogs.items() if not catalog]
    if empty:
        st.sidebar.error(f"No usable rows in {', '.join(empty)}: a catalog needs a `product` column.")
        st.stop()
    for name, (catalog, _) in catalogs.items():
        st.sidebar.caption(f"**{name}**: {len({r['product_id'] for r in catalog}):,} products, {len(catalog):,} release cycles")
    return catalogs


# ---------------------------------------------------------------- headline

def show_headline(results):
    h = view.headline(results)
    cols = st.columns(4)
    for col, (label, key, help_text) in zip(cols, [
            ("Matched", "matched", "CMDB rows linked to a product of the catalog"),
            ("With an end-of-life date", "dated", "rows for which the catalog gives a date"),
            ("Already end of life", "past_eol", "rows past end of life, or on extended support only"),
            ("To check", "to_check", "matched, but the match or the version deserves a human look")]):
        count, pct = h[key]
        col.metric(label, f"{pct:.0f}%", f"{count:,} of {h['rows']:,} rows", delta_color="off", help=help_text)


# ---------------------------------------------------------------- tab 1: results

def tab_results(rows, results, catalog, key, catalog_name):
    filters = st.columns([3, 3, 1.4])
    text = filters[0].text_input("Search", placeholder="vendor, product, version or matched product", key="results_text")
    statuses = filters[1].multiselect("Lifecycle", view.status_options(results), key="results_status", placeholder="All")
    technical = filters[2].toggle("Technical columns", key="results_technical")
    review_only = filters[2].checkbox("Only rows to check", key="results_review")

    shown = view.filter_results(results, text, statuses, review_only)
    table = pd.DataFrame(view.results_table(shown))
    if technical:
        table = pd.concat([table, pd.DataFrame(shown)[TECHNICAL].reset_index(drop=True)], axis=1)
    st.caption(f"{len(shown):,} of {len(results):,} rows. Click a row to see why it matched.")
    event = st.dataframe(table, hide_index=True, on_select="rerun", selection_mode="single-row", key="results_table",
                         column_config={"Confidence": PERCENT, "Days left": st.column_config.NumberColumn(format="%d")})
    st.download_button("Download all results (CSV)", csv_bytes(results), f"matches_{catalog_name}.csv", "text/csv")

    selected = event.selection.rows
    if not selected or selected[0] >= len(shown):
        return
    result = shown[selected[0]]
    st.subheader(f"{result['vendor']} {result['product']} {result.get('version', '')}".strip())
    for line in view.explain(result):
        st.markdown(f"- {line}")
    if result.get("product_id"):
        with st.expander(f"All release cycles of {result['matched_product']}"):
            cycles = pd.DataFrame([r for r in catalog if r["product_id"] == result["product_id"]])
            st.dataframe(cycles[["version", "release_date", "end_sale", "end_support", "eol", "end_extended", "latest"]]
                         .rename(columns=lambda c: c.replace("_", " ")), hide_index=True)
    with st.expander("Matching steps (technical)"):
        trace = {}
        match_one(rows[next(i for i, r in enumerate(results) if r is result)], index_cached(catalog, key), trace)
        for step, detail in trace.items():
            st.write(f"**{step}**: {detail}")


# ---------------------------------------------------------------- tab 2: coverage

def tab_coverage(runs, catalogs, selected):
    st.markdown("How much of your CMDB file each catalog covers. Add a catalog in the sidebar to compare it with the default one.")
    scores = []
    for name, results in runs.items():
        h = view.headline(results)
        lifecycle = lifecycle_metrics(results)
        scores.append({"Catalog": name, "CMDB rows": h["rows"], "Matched %": h["matched"][1], "End-of-life date %": h["dated"][1],
                       "Already end of life": h["past_eol"][0], "To check %": h["to_check"][1],
                       "Not matched %": matching_metrics(results)["unmatched_rate"],
                       "Version identified % (of matched)": lifecycle["version_identified_rate"]})
    percent_columns = {c: st.column_config.ProgressColumn(c, min_value=0, max_value=100, format="%.0f%%")
                       for c in ("Matched %", "End-of-life date %", "To check %", "Not matched %", "Version identified % (of matched)")}
    st.dataframe(pd.DataFrame(scores), hide_index=True, column_config=percent_columns)
    st.download_button("Download coverage table (CSV)", csv_bytes(scores), "coverage.csv", "text/csv")

    st.subheader(f"Not matched by {selected}")
    missing = view.top_unmatched(runs[selected])
    if missing:
        st.caption("The most frequent products this catalog does not know: the first ones to add or fix.")
        st.dataframe(pd.DataFrame(missing), hide_index=True)
    else:
        st.success("Every row found a product in this catalog.")

    with st.expander("What the catalogs contain"):
        contents = [{"catalog": name, **row} for name, (catalog, _) in catalogs.items() for row in catalog_coverage(catalog)]
        st.dataframe(pd.DataFrame(contents), hide_index=True)


# ---------------------------------------------------------------- tab 3: look up one item

def tab_lookup(catalogs, selected):
    st.markdown("Type a product the way your CMDB writes it and see how the catalog answers. Leave it empty to browse the catalog.")
    cols = st.columns(3)
    vendor = cols[0].text_input("Vendor", key="one_vendor", placeholder="optional")
    product = cols[1].text_input("Product", key="one_product", placeholder="e.g. Windows Server 2016, PowerEdge R640")
    version = cols[2].text_input("Version", key="one_version", placeholder="e.g. 3.11.2, a partial version is fine")
    package = st.text_input("Package name (optional)", key="one_package", placeholder="e.g. openssh-server-8.7p1")

    catalog, key = catalogs[selected]
    if not product.strip():
        return browse(catalog)

    query = {"vendor": vendor.strip(), "product": product.strip(), "version": version.strip(), "fw_or_pkg": package.strip()}
    answers = []
    for name, (other, other_key) in catalogs.items():
        answers.append(match_one(query, index_cached(other, other_key)) | {"catalog": name})
    table = pd.DataFrame(view.results_table(answers)).drop(columns=["Vendor", "Product", "Version"])
    table.insert(0, "Catalog", [a["catalog"] for a in answers])
    st.subheader("Best match")
    st.dataframe(table, hide_index=True, column_config={"Confidence": PERCENT})
    best = next((a for a in answers if a["catalog"] == selected), answers[0])
    for line in view.explain(best):
        st.markdown(f"- {line}")

    st.subheader(f"Release cycles in {selected}")
    found = lookup(index_cached(catalog, key), vendor, product, version)
    if found["detected_version"] and not version.strip():
        st.caption(f"Version `{found['detected_version']}` was read from the product name.")
    if not found["rows"]:
        st.info("Nothing in this catalog fits that description.")
        return
    cycles = pd.DataFrame(found["rows"])
    for column in ("release_date", "support_end", "eol_date", "extended_end"):
        cycles[column] = cycles[column].astype(str).replace({"None": ""})      # dates and missing dates in one text column
    cycles["lifecycle_status"] = cycles["lifecycle_status"].map(view.status_label)
    cycles = cycles[["product", "vendor", "source", "cycle", "release_date", "support_end", "eol_date", "lifecycle_status", "product_confidence"]]
    cycles.columns = ["Product", "Vendor", "Source", "Cycle", "Released", "Support ends", "End of life", "Lifecycle", "Confidence"]
    st.dataframe(cycles, hide_index=True, column_config={"Confidence": PERCENT})


def browse(catalog):
    text = st.text_input("Search the catalog", key="browse_text", placeholder="product, vendor or source")
    products = pd.DataFrame(catalog)
    cycles = products.groupby("product_id").size().rename("Cycles")
    products = products.drop_duplicates("product_id").set_index("product_id").join(cycles)
    table = products[["product", "vendor", "source", "category", "Cycles"]].rename(columns=str.capitalize)
    if text.strip():
        words = text.lower().split()
        haystack = table[["Product", "Vendor", "Source"]].astype(str).apply(" ".join, axis=1).str.lower()
        table = table[haystack.apply(lambda h: all(w in h for w in words))]
    st.caption(f"{len(table):,} of {len(products):,} products")
    st.dataframe(table.head(2000), hide_index=True)
    if len(table) > 2000:
        st.caption("Showing the first 2,000: narrow the search.")


# ---------------------------------------------------------------- page

input_name, rows, input_key = load_cmdb()
catalogs = load_catalogs()
runs = {}
for name, (catalog, key) in catalogs.items():
    with st.spinner(f"Matching {len(rows):,} rows against {name}..."):
        runs[name] = match_cached(rows, catalog, input_key, key)

st.title("CMDB lifecycle")
selected = st.segmented_control("Catalog", list(catalogs), default=list(catalogs)[0], key="catalog") if len(catalogs) > 1 else list(catalogs)[0]
selected = selected or list(catalogs)[0]
show_headline(runs[selected])

results_tab, coverage_tab, lookup_tab = st.tabs(["Results", "Coverage", "Look up"])
with results_tab:
    tab_results(rows, runs[selected], catalogs[selected][0], catalogs[selected][1], selected)
with coverage_tab:
    tab_coverage(runs, catalogs, selected)
with lookup_tab:
    tab_lookup(catalogs, selected)
