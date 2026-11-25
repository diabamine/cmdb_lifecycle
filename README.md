# CMDB Lifecycle Matcher

Match dirty CMDB rows (vendor, product, version) to canonical products, then attach end-of-life dates and known
vulnerabilities. Everything runs **offline** against a snapshot that is fetched once.

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run streamlit_app.py     # the committed data/master_catalog.csv is ready to use

# rebuild without network (committed raw files in snapshot/):
.venv/bin/python -m cmdb.ingest        # snapshot/ -> data/standard/*.csv
.venv/bin/python -m cmdb.catalog       # data/standard/*.csv -> data/master_catalog.csv

# refresh from the sources (needs internet; a source that fails keeps its previous data):
.venv/bin/python -m cmdb.ingest --fetch && .venv/bin/python -m cmdb.catalog
./run_all.sh                           # same, plus the public sample inventory and a matching run
```

## Pipeline
| Step | Code | Output |
|---|---|---|
| 1. Ingest (API + scraping) | `cmdb/ingest/<source>.py`, `python -m cmdb.ingest [--fetch] [--only ...]` | raw json in `snapshot/`, standardized `data/standard/<source>.csv` |
| 2. Master catalog | `cmdb/catalog.py`, `python -m cmdb.catalog` | `data/master_catalog.csv` (one row per product release cycle) |
| 3. Matching | `cmdb/matching/` (alias, exact on normalized names, fuzzy, cycle, lifecycle), `python -m cmdb.matching --cmdb in.csv --out out.csv` | matched rows; `cmdb/exposure.py` optionally adds KEV / EPSS / NVD columns |
| 4. UI | `streamlit_app.py`: loads a catalog and a CMDB file (`cmdb/inputs.py`), matches, shows coverage and a one-item lookup (`cmdb/view.py` holds the wording). Independent of steps 1-2 | |
| 5. Metrics | `cmdb/metrics.py`: catalog coverage per source, matching rates | |
| 6. Compare | `cmdb/compare.py`: match the same rows against several catalogs (your own component catalog CSV, single sources) | |

Column contracts for every step live in `cmdb/schema.py`. `tools/` holds test-data helpers (`make_synthetic_cmdb.py`,
`fetch_inventory.py`), the NVD index builder, and `score.py` (accuracy against the synthetic answer key). Tests: `pytest -q`.

## Using the app
`streamlit run streamlit_app.py`. The app only reads files: it never fetches, ingests or builds a catalog (that is
`python -m cmdb.ingest` then `python -m cmdb.catalog`, a separate step).

1. **Sidebar, your data**: upload your CMDB file (CSV with a `product` column; `version`, `vendor`, `fw_or_pkg`, `ci_type` improve the result). Without an upload the sample `cmdb_extract.csv` is used. `data/master_catalog.csv` is the default catalog when it exists; add your own catalog CSV(s) (`vendor`, `product`, `version`, optionally `eol`, `release_date`, `aliases`) to match against them too. A catalog you upload is enough on its own.
2. **Top of the page**: pick the catalog to look at (when there are several) and read four numbers: matched, with an end-of-life date, already end of life, to check.
3. **Results**: every CMDB row with what it matched, its lifecycle and end-of-life date. Search, filter by lifecycle, show only rows to check, download as CSV. Click a row for a plain-language explanation (why it matched, which release cycle, what the dates say), the product's other release cycles and the technical matching steps.
4. **Coverage**: one score table with a row per catalog (matched, end-of-life date found, already end of life, to check, not matched), the most frequent unmatched products, and what each catalog contains.
5. **Look up**: type vendor, product and version (a version inside the product name is detected; a package name is optional) and see the best match in every loaded catalog, why, and the release cycles that fit. Leave it empty to browse and search the catalog itself.

Code map for maintainers: `streamlit_app.py` is layout only (one function per tab); every label and sentence the user reads is in `cmdb/view.py`; reading files is `cmdb/inputs.py`; matching is `cmdb/matching/`. Tests: `tests/test_view.py` (wording, no Streamlit) and `tests/test_app_smoke.py` (the app end to end).

## Master catalog contents
Last refreshed 2026-11-24 (`snapshot/manifest.json` has the fetch time): 21,349 release cycles, 10,221 products.

| Source | Cycles | Notes |
|---|---|---|
| endoflife.date | 9,476 | OS, languages, databases, frameworks |
| eol.network | 5,135 | hardware; local use only, see below |
| IBM | 2,423 | hardware, bulk CSV |
| Red Hat | 1,580 | |
| Microsoft | 1,606 | scraped; paid ESU dates are folded into `end_extended` |
| Juniper | 617 | scraped |
| eosl.date | 11,111 | hardware (Apple, Brocade, Cisco, Dell EMC, HPE, IBM, Juniper, NetApp); scraped, one page per vendor |
| Apple | 481 | status only for vintage/obsolete models |
| Trellix | 1,195 | software versions and appliance models; read from the two CSV files behind trellix.com/support/end-of-life-products |
| Citrix | 36 | citrix.com product lifecycle matrix (CVAD, XenApp, NetScaler, FAS, App Layering, Secure Access, uberAgent) |
| NVIDIA | 25 | Jetson |
| VMware | 6 | public KB tables only |
| Cisco, manual | 0 | need credentials / a CSV in `manual/` |

eol.network's terms forbid republishing its data: keep `data/standard/eolnetwork.csv` and
`snapshot/vendor_lifecycle_eolnetwork.json` out of any public repository. Refreshing it only re-pulls vendors already
pulled (no new free-tier listing slot); new vendors need `--eol-vendors`.

## Known limits
- Cisco: the EoX v5 collector is tested only against a simulated API, never the live one. It needs a Cisco ID enabled for the Support APIs (Smart Net Total Care or Partner Support Service); a free Cisco.com account gets "HTTP 403 Developer Inactive". Run it with `CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -m cmdb.ingest --fetch --only cisco`; a failed run leaves existing Cisco data untouched. Without access there is no scraper fallback yet. NetApp needs a manual CSV in `manual/`.
- eosl.date (`python -m cmdb.ingest --fetch --only eosldate`) is a third-party aggregate scraped from `https://eosl.date/vendor/<slug>/`; it covers Cisco, Dell, HPE and NetApp without credentials, but dates are not authoritative and the scraper depends on the page layout. Hardware without a vendor collector: Dell, HP/HPE, Sun/Oracle, Alcatel-Lucent Enterprise (Dell and HP answer scripted clients with HTTP 403; the others publish no bulk list). NVIDIA networking (Mellanox) EOL notices are PDFs and are not parsed.
- Apple publishes only "vintage" / "obsolete" model lists, no dates: obsolete = EOL, vintage stays UNKNOWN. iPhone, iPad and Watch come from endoflife.date, with dates.
- IBM hardware matches by machine type/model ("1747HC2") or by a marketing name derived from IBM's title ("Power S922"). Sibling models share a name (S922 = 9009-22A / 22G), so the first one is picked at confidence 93; their dates may differ. About a third of IBM rows have no end-of-support date.
- The VMware matrix is behind a login: only public KB tables are collected.
- Scrapers (Microsoft, Juniper) depend on page layouts and each site's terms.
- Accuracy figures come from a synthetic generator; the public inventory has no answer key.
