"""Streamlit front end. Rendering only: every decision comes from the cmdb package.

Flow: one CMDB file is matched against the master catalog (default) and, optionally, against custom catalogs.
Every tab reads those same match results, so the sections always agree with each other.
"""
import csv
import hashlib
import io
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from cmdb.catalog import load_catalog
from cmdb.compare import compare_results, comparison_summary, load_component_catalog, reference_coverage
from cmdb.matching.lookup import filter_results, lookup
from cmdb.matching.matcher import build_index, load_aliases, match_one, match_rows, save_alias, search_scored
from cmdb.metrics import catalog_coverage, lifecycle_metrics, matching_metrics

CATALOG_PATH = Path("data/master_catalog.csv")
ALIASES_PATH = Path("aliases.csv")
SAMPLE_PATH = Path("cmdb_extract.csv")      # used until the user uploads a CMDB file
MASTER = "master"

# Columns of the match table: what was asked, why it matched (method, confidence, version rule), what lifecycle came back.
INPUT_SHOWN = ["vendor", "product", "version"]
MATCH_SHOWN = ["matched_product", "matched_source", "match_method", "match_confidence", "review",
               "cycle_match", "matched_cycle", "lifecycle_status",
               "release_date", "support_end", "eol_date", "extended_end", "days_to_eol", "latest_in_cycle", "newer_cycle", "dq_flags"]

st.set_page_config(page_title="CMDB lifecycle", layout="wide")


def digest(data):
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------- loading and matching (cached)

@st.cache_data
def get_master(mtime):
    return load_catalog(CATALOG_PATH)


def parse_cmdb(data):
    """CMDB extract bytes -> dicts. An extract may lack the vendor column, so default it to ''."""
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    for row in rows:
        row.setdefault("vendor", "")
    return rows


def parse_custom_catalog(data):
    # load_component_catalog takes a path, so spill the upload to a temp file; the file stem names the catalog
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "custom.csv"
        path.write_bytes(data)
        return load_component_catalog(path)


@st.cache_data(show_spinner=False)
def match_cached(_rows, _catalog, input_key, catalog_key, use_aliases):
    # the underscore arguments are not hashed: input_key / catalog_key stand in for them
    aliases = load_aliases(ALIASES_PATH) if use_aliases else None
    return match_rows(_rows, _catalog, aliases=aliases)


@st.cache_resource(show_spinner=False)
def index_cached(_catalog, catalog_key):
    # the index is opaque to the UI: cache it and pass it on
    return build_index(_catalog)


def to_csv_bytes(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------- sidebar: inputs

def choose_input():
    st.sidebar.header("1. CMDB file")
    upload = st.sidebar.file_uploader("CSV with product, version (optional: vendor, fw_or_pkg, ci_type)", type="csv")
    if upload:
        name, data = upload.name, upload.getvalue()
    elif SAMPLE_PATH.exists():
        name, data = SAMPLE_PATH.name, SAMPLE_PATH.read_bytes()
        st.sidebar.caption(f"No file uploaded: showing the sample `{name}`.")
    else:
        st.info("Upload a CMDB CSV in the sidebar to start.")
        st.stop()
    rows = parse_cmdb(data)
    if not rows or "product" not in rows[0]:
        st.error(f"`{name}` needs a `product` column.")
        st.stop()
    return name, rows, digest(data)


def choose_catalogs(master):
    """Master catalog always; custom catalogs are extra references to compare against it."""
    st.sidebar.header("2. Catalogs")
    st.sidebar.markdown("**Master catalog** (default)")
    uploads = st.sidebar.file_uploader("Custom catalog(s) to compare with the master (vendor, product, version, eol ...)",
                                       type="csv", accept_multiple_files=True)
    catalogs = {MASTER: (master, str(CATALOG_PATH.stat().st_mtime))}
    for upload in uploads:
        data = upload.getvalue()
        catalogs[Path(upload.name).stem] = (parse_custom_catalog(data), digest(data))
    return catalogs


def run_matches(rows, input_key, catalogs):
    runs = {}
    aliases_key = str(ALIASES_PATH.stat().st_mtime) if ALIASES_PATH.exists() else ""
    for name, (catalog, key) in catalogs.items():
        is_master = name == MASTER
        with st.spinner(f"Matching {len(rows):,} rows against {name} ({len(catalog):,} catalog rows)..."):
            runs[name] = match_cached(rows, catalog, input_key, key + (aliases_key if is_master else ""), is_master)
    return runs


# ---------------------------------------------------------------- tabs

def headline_metrics(results):
    """Four numbers every section can show for one set of results."""
    quality = matching_metrics(results)
    lifecycle = lifecycle_metrics(results)
    cols = st.columns(5)
    cols[0].metric("Rows", f"{quality['rows']:,}")
    cols[1].metric("Matched", f"{quality['match_rate']:.1f}%", help="input rows linked to a catalog product")
    cols[2].metric("Auto-accepted", f"{quality['auto_accept_rate']:.1f}%", help="matched without needing a human review")
    cols[3].metric("Version identified", f"{lifecycle['version_identified_rate']:.1f}%", help="of matched rows, a release cycle was found")
    cols[4].metric("EOL date found", f"{lifecycle['eol_date_rate']:.1f}%", help="of matched rows, the catalog gave an end-of-life date")


def show_match_table(table):
    """Matched items with their reasoning (method, confidence, version rule) and the lifecycle fields retrieved."""
    table = table.copy()
    # days_to_eol mixes '' and numbers: make it numeric so the table renders (and sorts) properly
    table["days_to_eol"] = pd.to_numeric(table["days_to_eol"], errors="coerce")
    shown = [c for c in INPUT_SHOWN if c in table] + MATCH_SHOWN
    st.dataframe(table[shown], hide_index=True,
                 column_config={"match_confidence": st.column_config.ProgressColumn("match_confidence", min_value=0, max_value=100, format="%d")})


def tab_matches(rows, runs, catalogs):
    name = st.radio("Show matches against", list(runs), horizontal=True) if len(runs) > 1 else MASTER
    results = runs[name]
    headline_metrics(results)

    table = pd.DataFrame(results)
    filters = st.columns(3)
    methods = filters[0].multiselect("Match method", sorted(table["match_method"].unique()))
    statuses = filters[1].multiselect("Lifecycle status", sorted(table["lifecycle_status"].unique()))
    review_only = filters[2].checkbox("Needs review only")
    if methods:
        table = table[table["match_method"].isin(methods)]
    if statuses:
        table = table[table["lifecycle_status"].isin(statuses)]
    if review_only:
        table = table[table["review"] != ""]
    show_match_table(table)
    st.caption(f"{len(table):,} of {len(results):,} rows. method = how the product was found (alias, exact, exact+cleaned, fuzzy ...); "
               "cycle_match = how the version was mapped to a release cycle.")
    st.download_button("Download matches CSV", to_csv_bytes(results), f"matches_{name}.csv", "text/csv")

    explain_row(rows, catalogs[name], name == MASTER)
    if name == MASTER:
        review_queue(results)


def explain_row(rows, catalog_entry, use_aliases):
    """Step-by-step reasoning for one input row, straight from the matcher's trace."""
    catalog, key = catalog_entry
    with st.expander("Explain one row"):
        choice = st.selectbox("Row", range(len(rows)), key="explain_row",
                              format_func=lambda i: f"{i}: {rows[i].get('vendor', '')} {rows[i]['product']} {rows[i].get('version', '')}".strip())
        aliases = load_aliases(ALIASES_PATH) if use_aliases else None
        trace = {}
        match_one(rows[choice], index_cached(catalog, key), trace, aliases=aliases)
        for step, detail in trace.items():
            st.write(f"**{step}**: {detail}")


def review_queue(results):
    """Let a human confirm a suggested match once; aliases.csv makes it stick on the next run."""
    pending = [r for r in results if r.get("review") and r.get("product_id")]
    with st.expander(f"Review queue ({len(pending)})"):
        for n, row in enumerate(pending[:20]):
            label = f"{row['vendor']} {row['product']} {row['version']} -> {row['matched_product']} ({row['match_confidence']})"
            left, right = st.columns([4, 1])
            left.write(f"{label}: {row['review']}")
            if right.button("Save alias", key=f"alias_{n}"):
                save_alias(ALIASES_PATH, row["vendor"], row["product"], row["product_id"])
                st.success("Alias saved: it applies on the next run (change the input or reload).")


def tab_metrics(runs, catalogs):
    st.subheader("Matching and lifecycle metrics per catalog")
    rows = []
    for name, results in runs.items():
        quality = matching_metrics(results)
        lifecycle = lifecycle_metrics(results)
        rows.append({"catalog": name, "rows": quality["rows"], "matched %": quality["match_rate"],
                     "auto-accepted %": quality["auto_accept_rate"], "review %": quality["review_queue_rate"],
                     "unmatched %": quality["unmatched_rate"], "version identified %": lifecycle["version_identified_rate"],
                     "EOL date found %": lifecycle["eol_date_rate"], "rows past EOL": lifecycle["past_eol_rows"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True)

    name = st.radio("Details for", list(runs), horizontal=True, key="metrics_catalog") if len(runs) > 1 else MASTER
    results = runs[name]
    quality = matching_metrics(results)
    lifecycle = lifecycle_metrics(results)
    left, right = st.columns(2)
    left.markdown("**Match method**")
    left.bar_chart(pd.Series(quality["by_match_method"], name="rows"))
    right.markdown("**Lifecycle status**")
    right.bar_chart(pd.Series(quality["by_lifecycle_status"], name="rows"))
    left.markdown("**How the version was mapped to a release cycle**")
    left.bar_chart(pd.Series(lifecycle["by_cycle_match"], name="rows"))
    right.markdown("**Most frequent unmatched vendors**")
    right.dataframe(pd.DataFrame(quality["unmatched_top_vendors"], columns=["vendor", "rows"]), hide_index=True)

    st.subheader("Catalog coverage")
    catalog_rows = []
    for catalog_name, (catalog, _) in catalogs.items():
        for row in catalog_coverage(catalog):
            catalog_rows.append({"catalog": catalog_name, **row})
    st.dataframe(pd.DataFrame(catalog_rows), hide_index=True)


def tab_compare(rows, runs, catalogs):
    if len(runs) < 2:
        st.info("Add a custom catalog in the sidebar to compare it with the master catalog on the same CMDB file.")
        return
    compared = compare_results(rows, runs)
    coverage = reference_coverage(compared)
    for entry in coverage:      # size of each catalog next to how much of the CMDB it covers
        totals = catalog_coverage(catalogs[entry["reference"]][0])[-1]
        entry.update(products=totals["products"], cycles=totals["cycles"], pct_eol_date=totals["pct_eol_date"])
    summary = comparison_summary(compared)

    st.subheader("Coverage per catalog")
    st.dataframe(pd.DataFrame(coverage), hide_index=True)
    st.bar_chart(pd.DataFrame(coverage).set_index("reference")[["auto_accept_pct", "review_pct", "unmatched_pct"]])

    st.subheader("Agreement between catalogs")
    agree = {row["name"]: row["count"] for row in summary if row["kind"] == "agree"}
    cols = st.columns(4)
    cols[0].metric("All agree", agree["all"], help="every catalog matched the same product")
    cols[1].metric("Partial", agree["partial"], help="some catalogs matched it, others did not")
    cols[2].metric("Conflict", agree["conflict"], help="catalogs matched different products")
    cols[3].metric("Nobody matched", agree["none-matched"])

    st.subheader("Matched rows side by side")
    names = list(runs)
    filters = st.columns(3)
    text = filters[0].text_input("Search input or matched product", key="compare_text")
    agree_filter = filters[1].multiselect("Agreement", ["all", "partial", "conflict", "none-matched"], key="compare_agree")
    unmatched_in = filters[2].selectbox("Only rows NOT matched by", ["(any)"] + names, key="compare_unmatched")

    table = pd.DataFrame(compared)
    if text.strip():
        search_columns = ["product"] + [f"{n}:matched_product" for n in names]
        haystack = table[search_columns].astype(str).apply(" ".join, axis=1).str.lower()
        table = table[haystack.str.contains(text.strip().lower(), regex=False)]
    if agree_filter:
        table = table[table["agree"].isin(agree_filter)]
    if unmatched_in != "(any)":
        table = table[table[f"{unmatched_in}:matched_product"] == ""]

    shown = [c for c in INPUT_SHOWN if c in table] + ["agree"]
    for n in names:
        shown += [f"{n}:{field}" for field in ("matched_product", "match_method", "match_confidence", "lifecycle_status", "eol_date")]
    st.dataframe(table[shown], hide_index=True)
    st.caption(f"{len(table):,} of {len(compared):,} rows")
    st.download_button("Download comparison CSV", to_csv_bytes(compared), "comparison.csv", "text/csv")

    differing = pd.DataFrame([row for row in summary if row["kind"] == "eol_differs"])
    if not differing.empty:
        with st.expander(f"Same item, different EOL date ({len(differing)})"):
            st.dataframe(differing[["name", "detail"]], hide_index=True)


LOOKUP_SHOWN = ["product", "vendor", "source", "product_match", "product_confidence", "vendor_match", "cycle", "version_fit",
                "release_date", "support_end", "eol_date", "extended_end", "days_to_eol", "lifecycle_status", "latest_in_cycle"]


def show_lookup(index, vendor, product, version):
    """Catalog side of the search: every product and release cycle that fits the typed description."""
    result = lookup(index, vendor, product, version)
    rows = result["rows"]
    if result["detected_version"] and not version.strip():
        st.caption(f"Version `{result['detected_version']}` detected in the product name.")
    if not rows:
        st.info("Nothing in this catalog fits that description.")
        return
    with_cycle = [r for r in rows if r["cycle"]]
    with_eol = [r for r in with_cycle if r["eol_date"]]
    cols = st.columns(3)
    cols[0].metric("Products found", len({r["product_id"] for r in rows}))
    cols[1].metric("Cycles fitting", len(with_cycle), help="release cycles that fit the version you typed (all cycles when none typed)")
    cols[2].metric("With an EOL date", f"{100 * len(with_eol) / max(len(with_cycle), 1):.0f}%")
    table = pd.DataFrame(rows)[LOOKUP_SHOWN]
    for column in ("release_date", "support_end", "eol_date", "extended_end"):
        table[column] = table[column].astype(str).replace({"None": ""})      # dates and missing dates in one text column
    st.dataframe(table, hide_index=True,
                 column_config={"product_confidence": st.column_config.ProgressColumn("product_confidence", min_value=0, max_value=100, format="%d")})
    st.caption("version_fit: exact = same cycle | cycle-prefix = your version is a patch of this cycle | "
               "version-prefix = your version is partial, so every cycle that starts with it is listed | any = no version typed.")


def tab_explore(rows, runs, catalogs):
    """Search by vendor, product and version: what the catalog says, and which rows of the uploaded file fit."""
    name = st.radio("Catalog", list(runs), horizontal=True, key="explore_catalog") if len(runs) > 1 else MASTER
    catalog, key = catalogs[name]
    results = runs[name]

    cols = st.columns(3)
    vendor = cols[0].text_input("Vendor", key="explore_vendor")
    product = cols[1].text_input("Product (a version in the name is detected)", key="explore_product")
    version = cols[2].text_input("Version (partial is fine: 3 or 3.11)", key="explore_version")
    searching = bool(vendor.strip() or product.strip() or version.strip())

    if product.strip():
        st.subheader("In the catalog")
        show_lookup(index_cached(catalog, key), vendor, product, version)
    elif searching:
        st.caption("Type a product name to look it up in the catalog. Vendor and version still filter the file below.")

    st.subheader("In your CMDB file")
    if searching:
        # filter_results returns the same dict objects, so identity tells us which positions survived (needed for the drill-down)
        kept = {id(r) for r in filter_results(results, vendor, product, version)}
        positions = [i for i, r in enumerate(results) if id(r) in kept]
    else:
        positions = list(range(len(results)))
    found = [results[i] for i in positions]
    st.caption(f"{len(found):,} of {len(results):,} input rows")
    if not found:
        st.info("No input row fits that description.")
        return
    headline_metrics(found)       # metrics follow the filter, so they describe exactly the rows shown
    show_match_table(pd.DataFrame(found))

    chosen = st.selectbox("Look closer at one row", positions, key="explore_row",
                          format_func=lambda i: f"{i}: {rows[i].get('vendor', '')} {rows[i]['product']} {rows[i].get('version', '')}".strip())
    result = results[chosen]
    st.markdown("**Candidate products in the catalog for this row** (what the matcher chose among)")
    candidates = search_scored(index_cached(catalog, key), result["product"])
    if candidates:
        st.dataframe(pd.DataFrame(candidates)[["product", "vendor", "source", "matched_on", "method", "confidence", "verdict"]], hide_index=True)
    else:
        st.caption("No similar product in this catalog.")
    if result["product_id"]:
        st.markdown(f"**Release cycles of {result['matched_product']}** (matched cycle: `{result['matched_cycle'] or 'none'}`, "
                    f"version rule: `{result['cycle_match'] or 'n/a'}`)")
        cycles = [r for r in catalog if r["product_id"] == result["product_id"]]
        st.dataframe(pd.DataFrame(cycles).drop(columns=["id", "product_id", "aliases"]), hide_index=True)

    with st.expander("Browse the catalog itself"):
        browse_catalog(catalog)


def browse_catalog(catalog):
    columns = st.multiselect("Columns", list(catalog[0]), ["product_id", "vendor", "product", "source", "category"])
    products = pd.DataFrame(catalog).drop_duplicates("product_id")
    st.dataframe(products[columns], hide_index=True)
    st.caption(f"{len(products):,} products in this catalog.")


# ---------------------------------------------------------------- page

if not CATALOG_PATH.exists():
    st.error("Master catalog not found. Run `python -m cmdb.ingest` then `python -m cmdb.catalog`.")
    st.stop()

master = get_master(CATALOG_PATH.stat().st_mtime)
input_name, rows, input_key = choose_input()
catalogs = choose_catalogs(master)
runs = run_matches(rows, input_key, catalogs)

st.title("CMDB lifecycle matching")
st.caption(f"`{input_name}`: {len(rows):,} rows matched against {', '.join(catalogs)}.")
matches_tab, metrics_tab, compare_tab, explore_tab = st.tabs(["Matches", "Metrics", "Compare catalogs", "Explore"])
with matches_tab:
    tab_matches(rows, runs, catalogs)
with metrics_tab:
    tab_metrics(runs, catalogs)
with compare_tab:
    tab_compare(rows, runs, catalogs)
with explore_tab:
    tab_explore(rows, runs, catalogs)
