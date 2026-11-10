"""Streamlit explorer: see HOW the CMDB matching works, step by step, on real public inventory data.
Run:  .venv/bin/streamlit run app.py      (needs snapshot/ from fetch_snapshot.py, raw/inventory.csv from fetch_inventory.py)
"""
import csv, json
from datetime import date
import pandas as pd
import streamlit as st
import cmdb_enrich as ce          # the matching engine (cascade, cycle match, lifecycle, KEV)

st.set_page_config(page_title="CMDB matching explorer", layout="wide")
today = date.today()

@st.cache_resource                # reference data loaded once per server
def load_ref():
    return ce.Ref(ce.Path("snapshot"))
@st.cache_data(show_spinner="Matching the inventory (~1 s per 300 rows, cached)...")
def enrich_csv(file, n=None):     # enrich a CSV (optionally a random sample of n rows), cached per input
    raw = pd.read_csv(file, keep_default_na=False, dtype=str)
    rows = (raw.sample(n, random_state=1) if n and n < len(raw) else raw).to_dict("records")
    for r in rows: r.setdefault("fw_or_pkg", ""); r.setdefault("ci_type", "software"); r.setdefault("vendor", "")
    return tidy(pd.DataFrame(ce.enrich(load_ref(), rows, today)))
@st.cache_data
def load_full(path, mtime):       # the complete run written by cmdb_enrich.py (mtime = cache key)
    return tidy(pd.read_csv(path, keep_default_na=False, dtype=str))
def tidy(df):                     # '' -> NaN for numbers, dates -> text, so Arrow / pandas behave
    for c in ("match_confidence", "days_to_eol", "kev_cves", "kev_ransomware", "epss_max", "nvd_cves_total", "nvd_cves_version", "nvd_max_cvss", "nvd_critical", "nvd_kev"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.astype({c: str for c in df if c.endswith(("_date", "_end", "_added", "_cve", "_sale"))})
ref = load_ref()
ALIASES = ce.Path("aliases.csv"); RAW_COLS = ["asset_id", "source", "ci_type", "vendor", "product", "version", "fw_or_pkg"]

# ---- which results to analyse: own upload > precomputed full run > live sample
FULL = ce.Path("out_real/cmdb_enriched.csv")
up = st.sidebar.file_uploader("Your own CSV (vendor, product, version [, fw_or_pkg])", type="csv")
if up: df = enrich_csv(up)
elif FULL.exists() and st.sidebar.checkbox("Use the complete precomputed run (out_real/)", True): df = load_full(str(FULL), FULL.stat().st_mtime)
else: df = enrich_csv("raw/inventory.csv", int(st.sidebar.number_input("Sample size (live matching)", 1000, 30000, 6000, step=1000)))
if "source" not in df: df["source"] = "upload"
st.sidebar.caption(f"Analysing {len(df):,} rows")

tab1, tab_cov, tab_rev, tab2, tab3 = st.tabs(["🔎 Match explorer", "🧭 Source coverage", "✅ Review queue", "📊 Inventory results", "📚 Reference catalogue"])

# =============================== TAB 1: type a component, watch the cascade ===============================
with tab1:
    # example picker: real rows from the public inventory (fills the 4 inputs below)
    ex = pd.read_csv("raw/inventory.csv", keep_default_na=False, dtype=str).sample(300, random_state=1).reset_index(drop=True)
    def fill():
        r = ex.loc[st.session_state.ex_i]
        st.session_state.update(v=r["vendor"], p=r["product"], ver=r["version"], pk=r["fw_or_pkg"])
    st.selectbox("Load a real example (winget / Homebrew / Ubuntu)", ex.index, key="ex_i", index=None, on_change=fill,
                 format_func=lambda i: f'{ex.source[i]} | {ex.vendor[i]} | {ex["product"][i]} | {ex.version[i]}')
    with st.form("q"):            # Enter in any field runs the match
        c = st.columns(4)
        vendor = c[0].text_input("Vendor", key="v"); product = c[1].text_input("Product", key="p")
        version = c[2].text_input("Version", key="ver"); pkg = c[3].text_input("Package string (optional)", key="pk")
        st.form_submit_button("Match", type="primary")
    if product:
        row = dict(vendor=vendor, product=product, version=version, fw_or_pkg=pkg, ci_type="software")
        tr = {}; ce.match_product(ref, row, tr); res = ce.enrich(ref, [row], today)[0]

        if tr["alias"]: st.success(f"0 · Alias table: a human decision applies → `{tr['alias']}` ('-' = confirmed not a catalogue product). The steps below were skipped.")
        st.markdown("#### 1 · Normalize: candidate spellings")
        st.write("Vendor key:", f"`{tr.get('vendor_key') or '—'}`")
        st.dataframe(pd.DataFrame([(c, k) for c, k in tr["kinds"].items()], columns=["spelling tried (in order)", "why it exists"]), hide_index=True, width="stretch")
        st.caption("raw = as typed · cleaned = locale, edition words, embedded version removed · vstrip = vendor word removed (exact lookup only) · "
                   "rule = vendor naming habit (.NET, Visual Studio, Java SE) · rollup = Debian/RPM sub-package → parent product · rollup-lang = python3-x style guess "
                   "(never allowed to match an OS or language). Only raw and cleaned spellings are used for fuzzy search.")

        st.markdown("#### 2 · Exact / alias lookup")
        if tr["exact"]: st.success(f'Hit on `{tr["exact"][0]}` → {tr["exact"][1]}  (stops here)')
        else: st.warning("No exact hit → fuzzy search")

        if not tr["exact"]:
            st.markdown("#### 3 · Fuzzy search (token-sort ratio, vendor signal ±)")
            st.caption(f"≥ {ce.AUTO} auto-accept · {ce.REVIEW}–{ce.AUTO} human review · < {ce.REVIEW} rejected. "
                       "Vendor bonus: +4 if the vendor agrees with the product's vendor, −6 if it conflicts.")
            if tr["fuzzy"]:
                st.dataframe(pd.DataFrame(tr["fuzzy"], columns=["reference key", "product", "raw score", "vendor bonus", "final"])
                             .sort_values("final", ascending=False), hide_index=True, width="stretch")
            else: st.info(f"No reference name scored ≥ {ce.REVIEW}.")

        p = None                       # lifecycle product (set below when matched)
        st.markdown("#### 4 · Decision")
        if not res["matched_product"]:
            st.error("UNMATCHED — in-house or unknown product (or a gap in the reference data)")
        else:
            d = st.columns(4)
            d[0].metric("Matched product", res["matched_product"]); d[1].metric("Source", res["matched_source"])
            d[2].metric("Method", res["match_method"]); d[3].metric("Confidence", res["match_confidence"])
            st.write("Review flag:", res["review"] or "✅ none (auto-accepted)", "| data-quality flags:", res["dq_flags"] or "—")

            p = next(q for q in ref.prods.values() if q["label"] == res["matched_product"] and q["source"] == res["matched_source"])
            st.markdown("#### 5 · Version → release cycle")
            st.write(f"`{version or '(empty)'}` → cycle **{res['matched_cycle'] or '?'}**, match type `{res['cycle_match']}` "
                     "(longest dotted prefix: 3.8.10 → 3.8 → 3; epoch `1:` stripped)")
            cyc = pd.DataFrame(p["releases"])[["cycle", "release_date", "end_sale", "eoas", "eol", "eoes", "latest"]]
            cyc.columns = ["cycle", "released", "end of sale", "end of full support", "end of life", "end of extended", "latest"]
            cyc["◀ matched"] = cyc["cycle"].eq(res["matched_cycle"]).map({True: "◀", False: ""})
            st.dataframe(cyc, hide_index=True, width="stretch")

            st.markdown("#### 6 · Lifecycle & exposure")
            m = st.columns(4)
            m[0].metric("Status", res["lifecycle_status"]); m[1].metric("End of life", str(res["eol_date"] or "—"))
            m[2].metric("Days to EOL", str(res["days_to_eol"] or "—")); m[3].metric("Newer cycle", res["newer_cycle"] or "—")
            if p["kev"]:
                st.write(f'**{len(p["kev"])} known-exploited CVEs** (CISA KEV, product-level) · max EPSS {res["epss_max"]}')
                st.dataframe(pd.DataFrame(p["kev"]).sort_values("epss", ascending=False).head(10), hide_index=True, width="stretch")

        st.markdown("#### 7 · NVD — full CVE base (421k CVEs), version-level")
        vp, how = ce.nvd_identify(ref, row, p, ce.name_candidates(row))
        if not vp: st.info("Product not identified in NVD (no vendor:product with CVEs matches this name).")
        else:
            allc, app = ce.nvd_cves(ref, vp, version)
            trust = {"from-lifecycle-product": "high (CPE id of the matched lifecycle product)", "name+vendor": "medium (name + vendor agree)",
                     "name-unique": "low (unique name only, no vendor signal) - verify"}[how]
            st.write(f"NVD product `{vp[0]}:{vp[1]}` · identified by **{how}** → trust {trust}")
            m = st.columns(4); m[0].metric("CVEs for the product", len(allc))
            if app is None: st.warning("Version unusable → only product-level CVEs are known.")
            else:
                m[1].metric("Applicable to this version", len(app)); m[2].metric("Critical (CVSS ≥ 9)", sum(c["cvss"] >= 9 for c in app))
                m[3].metric("In CISA KEV", sum(c["kev"] for c in app))
                st.caption("Applicability = version inside the CPE exact-version / start-end range. NVD entries with no version bounds count as all versions.")
                if app: st.dataframe(pd.DataFrame(app).sort_values(["kev", "cvss"], ascending=False).head(25), hide_index=True, width="stretch")
    else:
        st.info("Type a component above (e.g. vendor `MSFT`, product `Windows Server 2016 Standard`, version `10.0.14393`) or load a real example.")

# =============================== TAB: coverage of every source, in rows ===============================
with tab_cov:
    total = len(df); pct = lambda n: f"{n / max(total, 1):.1%}"
    mt = df.matched_product != ""; auto = mt & (df.review == "")                  # sanitized = matched, confidence >= 90, cycle resolved
    known = df.lifecycle_status.isin(["SUPPORTED", "MAINTENANCE_ONLY", "EXTENDED_SUPPORT_ONLY", "EOL"])
    nvd = df.nvd_cpe != ""; nvd_ok = df.nvd_match.isin(["from-lifecycle-product", "name+vendor"])
    st.subheader("From raw rows to sanitized, enriched rows")
    st.caption("**Matched** = the row is mapped to a reference product. **Sanitized** = matched with confidence ≥ 90 and a resolved release cycle, "
               "so name, vendor and cycle are canonical and need no human review.")
    steps = [("All rows", total), ("Matched to a lifecycle product", mt.sum()), ("… sanitized (auto-accepted)", auto.sum()), ("… of which confirmed by a human (alias table)", df.match_method.isin(["alias", "alias-none"]).sum()),
             ("… with a definite lifecycle status (supported / EOL …)", (mt & known).sum()), ("… canonical vendor assigned", (mt & (df.canonical_vendor != "")).sum()),
             ("Identified in NVD (any method)", nvd.sum()), ("… trusted NVD match (lifecycle CPE or name+vendor)", nvd_ok.sum()),
             ("… with CVEs applicable to the exact version", (nvd & (df.nvd_cves_version > 0)).sum())]
    f = pd.DataFrame(steps, columns=["stage", "rows"]); f["% of all rows"] = f.rows.map(pct)
    a, b = st.columns([3, 2]); a.dataframe(f, hide_index=True, width="stretch"); b.bar_chart(f.set_index("stage")["rows"], horizontal=True)

    st.subheader("Coverage by reference source")
    n_prod = pd.Series([p["source"] for p in ref.prods.values()]).value_counts()
    rows = []
    for src, n_p in n_prod.items():
        s = df.matched_source == src
        rows.append(dict(source=src, kind="lifecycle", reference_products=int(n_p), rows_matched=int(s.sum()), sanitized=int((s & auto).sum()),
                         version_resolved=int((s & known).sum()), unsupported=int((s & df.lifecycle_status.isin(["EOL", "EXTENDED_SUPPORT_ONLY"])).sum())))
    n_nvd = ref.nvd.execute("select count(*) from (select distinct vendor, product from products)").fetchone()[0] if ref.nvd else 0
    rows += [dict(source="NVD (all CVEs)", kind="vulnerability", reference_products=n_nvd, rows_matched=int(nvd.sum()), sanitized=int(nvd_ok.sum()),
                  version_resolved=int((nvd & df.nvd_cves_version.notna()).sum()), unsupported=None),
             dict(source="CISA KEV", kind="exploited CVEs", reference_products=sum(1 for p in ref.prods.values() if p["kev"]), rows_matched=int((df.kev_cves > 0).sum()),
                  sanitized=None, version_resolved=None, unsupported=int(((df.kev_cves > 0) & df.lifecycle_status.isin(["EOL", "EXTENDED_SUPPORT_ONLY"])).sum())),
             dict(source="EPSS", kind="exploit probability", reference_products=None, rows_matched=int(df.epss_max.notna().sum()), sanitized=None, version_resolved=None, unsupported=None)]
    cov = pd.DataFrame(rows); cov["% of all rows"] = cov.rows_matched.map(pct)
    cov = cov.astype({c: "Int64" for c in ("reference_products", "rows_matched", "sanitized", "version_resolved", "unsupported")})   # no 12.0 floats
    st.dataframe(cov, hide_index=True, width="stretch")
    st.caption("`unsupported` = EOL or extended-support-only rows; for KEV it counts unsupported rows whose product has known-exploited CVEs.")

    a, b = st.columns(2)
    a.subheader("Match rate by inventory origin")
    g = df.assign(matched=mt, sanitized=auto, nvd=nvd).groupby("source")[["matched", "sanitized", "nvd"]].mean() * 100
    a.bar_chart(g.rename(columns={"matched": "lifecycle matched %", "sanitized": "sanitized %", "nvd": "NVD identified %"}))
    b.subheader("Which source answered which origin")
    b.dataframe(pd.crosstab(df.source, df.matched_source.replace("", "— unmatched")), width="stretch")

    st.subheader("Source status")
    vf = ce.Path("snapshot/vendor_lifecycle.json"); vc = pd.DataFrame(json.load(open(vf))).vendor.value_counts().to_dict() if vf.exists() else {}
    st.dataframe(pd.DataFrame([
        ("endoflife.date", "API, bulk", n_prod.get("endoflife.date", 0), "loaded"), ("Red Hat lifecycle", "API", n_prod.get("redhat", 0), "loaded"),
        ("Microsoft", "HTML scrape", vc.get("microsoft", 0), "loaded"), ("Juniper", "HTML scrape", vc.get("juniper", 0), "loaded"),
        ("VMware", "public KB tables", vc.get("vmware", 0), "partial: login-only matrix → manual CSV"),
        ("Cisco EoX v5", "API (OAuth)", vc.get("cisco", 0), "not loaded: set CISCO_CLIENT_ID / CISCO_CLIENT_SECRET" if not vc.get("cisco") else "loaded"),
        ("NetApp", "manual CSV", vc.get("netapp", 0), "not loaded: no public source → manual/*.csv"),
        ("NVD", "bulk mirror", n_nvd, "loaded"), ("CISA KEV + EPSS", "bulk", sum(1 for p in ref.prods.values() if p["kev"]), "loaded (products linked)")],
        columns=["source", "collected by", "records / products", "status"]), hide_index=True, width="stretch")

    st.subheader("What is not covered: most frequent unmatched vendors")
    um = df[~mt & ~nvd]
    st.dataframe(um.assign(vendor=um.vendor.replace("", "(empty)")).groupby("vendor").size().sort_values(ascending=False).head(15).rename("unmatched rows"), width="stretch")

# =============================== TAB: review queue -> alias table ===============================
def save_alias(vendor, product, target_id, target_label):
    """Append a human decision to aliases.csv, reload the reference, and re-match only the rows it affects."""
    new = not ALIASES.exists()
    with open(ALIASES, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(["vendor", "product", "target_id", "target_label", "decided_on"])
        w.writerow([vendor, product, target_id, target_label, date.today().isoformat()])
    st.cache_resource.clear(); r2 = load_ref()
    if FULL.exists():                                   # patch the saved full run in place
        full = pd.read_csv(FULL, keep_default_na=False, dtype=str)
        hit = full[full["product"].map(ce.norm) == ce.norm(product)]
        if vendor: hit = hit[hit.vendor.map(ce.norm_vendor) == ce.norm_vendor(vendor)]
        if len(hit):
            out = pd.DataFrame(ce.enrich(r2, hit[RAW_COLS].to_dict("records"), today)).astype(str)
            full.loc[hit.index, full.columns] = out[full.columns].values
            full.to_csv(FULL, index=False)
    return len(hit) if FULL.exists() else 0

with tab_rev:
    st.markdown("Rows the matcher was **not sure about**, grouped by the same spelling and sorted by how many rows one decision fixes. "
                "Every decision is stored in `aliases.csv`, wins over all matching rules on the next run, and so the queue shrinks over time.")
    if st.session_state.get("flash"): st.success(st.session_state.pop("flash"))
    incl = st.checkbox("Also list rows with no match at all (in-house or unknown products)", False)
    qd = df[(df.review != "") & (~df.lifecycle_status.isin(["NO_SOURCE"]))]
    if not incl: qd = qd[qd.matched_product != ""]
    if qd.empty: st.info("Nothing to review.")
    else:
        g = (qd.groupby(["vendor", "product"]).agg(rows=("asset_id", "size"), suggestion=("matched_product", "first"), source=("matched_source", "first"),
             confidence=("match_confidence", "max"), reason=("review", "first")).reset_index().sort_values("rows", ascending=False).reset_index(drop=True))
        st.caption(f"{len(qd):,} rows in {len(g):,} groups")
        ev = st.dataframe(g.head(500), on_select="rerun", selection_mode="single-row", hide_index=True, width="stretch", key="revq")
        if ev.selection.rows:
            row = g.iloc[ev.selection.rows[0]]
            st.markdown(f"#### Decide: `{row['vendor'] or '(no vendor)'}` · `{row['product']}`  ({row['rows']} rows)")
            tr = {}; ce.match_product(ref, dict(vendor=row["vendor"], product=row["product"], version="", fw_or_pkg="", ci_type="software"), tr)
            if tr["fuzzy"]:
                st.caption("Candidates the matcher considered:"); st.dataframe(pd.DataFrame(tr["fuzzy"], columns=["reference key", "product", "raw", "vendor bonus", "final"]).sort_values("final", ascending=False).head(5), hide_index=True, width="stretch")
            ids = {f'{p["label"]}  [{p["source"]}]': pid for pid, p in ref.prods.items()}
            sug = next((pid for pid, p in ref.prods.items() if p["label"] == row["suggestion"] and p["source"] == row["source"]), None)
            c1, c2, c3 = st.columns(3)
            if sug and c1.button(f'Accept: {row["suggestion"]}', type="primary"):
                n = save_alias(row["vendor"], row["product"], sug, row["suggestion"]); st.session_state.flash = f"Saved. {n} rows re-matched."; st.rerun()
            pick = c2.selectbox("…or map to another product", sorted(ids), index=None, key="alias_pick", placeholder="search the reference")
            if c2.button("Save mapping", disabled=pick is None):
                n = save_alias(row["vendor"], row["product"], ids[pick], pick.rsplit("  [", 1)[0]); st.session_state.flash = f"Saved. {n} rows re-matched."; st.rerun()
            if c3.button("Not a catalogue product"):
                n = save_alias(row["vendor"], row["product"], "-", ""); st.session_state.flash = f"Saved as in-house / not a product. {n} rows updated."; st.rerun()
    if ALIASES.exists():
        st.markdown("#### Decisions so far"); st.dataframe(pd.read_csv(ALIASES, dtype=str, keep_default_na=False), hide_index=True, width="stretch")

# =============================== TAB 2: whole-inventory results ===============================
with tab2:
    matched = df["matched_product"] != ""
    k = st.columns(5); nv = df.nvd_cpe != ""
    k[0].metric("Rows", f"{len(df):,}"); k[1].metric("Matched to a product", f"{matched.mean():.1%}")
    k[2].metric("Auto-accepted", f"{(matched & (df.review == '')).mean():.1%}")
    k[3].metric("EOL / extended-only", int(df.lifecycle_status.isin(["EOL", "EXTENDED_SUPPORT_ONLY"]).sum()))
    k[4].metric("EOL + exploited CVEs", int((df.lifecycle_status.isin(["EOL", "EXTENDED_SUPPORT_ONLY"]) & (df.kev_cves > 0)).sum()))
    nk = st.columns(4)
    nk[0].metric("Identified in NVD", f"{nv.mean():.1%}"); nk[1].metric("With CVE applicable to version", int((nv & (df.nvd_cves_version > 0)).sum()))
    nk[2].metric("With critical CVE (≥9)", int((df.nvd_critical > 0).sum())); nk[3].metric("Affected by a KEV CVE", int((df.nvd_kev > 0).sum()))
    st.caption("Low match rate on winget/Homebrew/Ubuntu is expected: the lifecycle catalogues cover ~700 mainstream products, "
               "while these catalogues are long-tail. The gap itself is a real finding (→ AI_USECASES.md #1, #4).")
    a, b = st.columns(2)
    a.subheader("Lifecycle status by source"); a.bar_chart(pd.crosstab(df.source, df.lifecycle_status))
    b.subheader("Match method by source"); b.bar_chart(pd.crosstab(df.source, df.match_method))
    q = st.text_input("Filter product / matched product contains")
    status = st.multiselect("Lifecycle status", sorted(df.lifecycle_status.unique()))
    v = df[df["product"].str.contains(q, case=False) | df["matched_product"].str.contains(q, case=False)] if q else df
    if status: v = v[v.lifecycle_status.isin(status)]
    st.dataframe(v[["source", "vendor", "product", "version", "matched_product", "match_method", "match_confidence",
                    "matched_cycle", "lifecycle_status", "eol_date", "nvd_cpe", "nvd_match", "nvd_cves_total", "nvd_cves_version", "nvd_max_cvss", "review"]].head(2000), hide_index=True, width="stretch")
    st.caption(f"{len(v):,} rows match the filter (showing max 2000)")

# =============================== TAB 3: search the reference catalogue ===============================
with tab3:
    s = st.text_input("Search the reference catalogue (name, label, alias or vendor)")
    cat = pd.DataFrame([dict(product=p["label"], source=p["source"], category=p["category"], vendor=p["vendor"],
                             cycles=len(p["releases"]), kev_cves=len(p["kev"])) for p in ref.prods.values()])
    if s:
        n = ce.norm(s); hit = {p for k, ids in ref.index.items() if n in k for p in ids}
        cat = cat[cat["product"].isin({ref.prods[i]["label"] for i in hit}) | cat.vendor.str.contains(s, case=False)]
    st.dataframe(cat, hide_index=True, width="stretch")
