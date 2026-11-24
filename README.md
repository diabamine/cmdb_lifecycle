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
| 4. UI | `streamlit_app.py`: one CMDB file is matched against the master catalog (default) and any custom catalogs; tabs Matches, Metrics, Compare catalogs, Explore all read the same results | |
| 5. Metrics | `cmdb/metrics.py`: catalog coverage per source, matching rates | |
| 6. Compare | `cmdb/compare.py`: match the same rows against several catalogs (your own component catalog CSV, single sources) | |

Column contracts for every step live in `cmdb/schema.py`. `tools/` holds test-data helpers (`make_synthetic_cmdb.py`,
`fetch_inventory.py`), the NVD index builder, and `score.py` (accuracy against the synthetic answer key). Tests: `pytest -q`.

## Using the app
1. Upload a CMDB CSV (`product`, `version`; optional `vendor`, `fw_or_pkg`, `ci_type`) in the sidebar. Without an upload the sample `cmdb_extract.csv` is used.
2. It is matched against the master catalog automatically. Optionally upload one or more custom catalogs (`vendor`, `product`, `version`, plus `eol`, `release_date`, `aliases` ...) to see how they compare.
3. **Matches**: one table per catalog with the matched product, `match_method` and `match_confidence` (why it matched), `cycle_match` and `matched_cycle` (how the version was mapped), and the lifecycle fields (release, support end, EOL date, days to EOL, status). Metrics on top, filters, row-by-row explanation and the review queue below.
4. **Metrics**: matched / auto-accepted / review / unmatched rates, share of matched rows with a version identified and an EOL date found, per catalog, plus catalog coverage.
5. **Explore**: type a vendor, product and/or version. The catalog side lists every product and release cycle that fits (a partial version such as `3` shows all 3.x cycles, a version inside the name such as `SQL Server 2019` is detected), with EOL dates and `version_fit` explaining each cycle. The same description filters the rows of your file, with metrics for the filtered rows.
6. **Compare catalogs**: coverage per catalog, agreement counts, side-by-side results and items whose EOL date differs between catalogs.

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
| Apple | 481 | status only for vintage/obsolete models |
| NVIDIA | 25 | Jetson |
| VMware | 6 | public KB tables only |
| Cisco, manual | 0 | need credentials / a CSV in `manual/` |

eol.network's terms forbid republishing its data: keep `data/standard/eolnetwork.csv` and
`snapshot/vendor_lifecycle_eolnetwork.json` out of any public repository. Refreshing it only re-pulls vendors already
pulled (no new free-tier listing slot); new vendors need `--eol-vendors`.

## Known limits
- Cisco: the EoX v5 collector is tested only against a simulated API, never the live one. It needs a Cisco ID enabled for the Support APIs (Smart Net Total Care or Partner Support Service); a free Cisco.com account gets "HTTP 403 Developer Inactive". Run it with `CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -m cmdb.ingest --fetch --only cisco`; a failed run leaves existing Cisco data untouched. Without access there is no scraper fallback yet. NetApp needs a manual CSV in `manual/`.
- Hardware without a collector: Dell, HP/HPE, Sun/Oracle, Alcatel-Lucent Enterprise (Dell and HP answer scripted clients with HTTP 403; the others publish no bulk list). NVIDIA networking (Mellanox) EOL notices are PDFs and are not parsed.
- Apple publishes only "vintage" / "obsolete" model lists, no dates: obsolete = EOL, vintage stays UNKNOWN. iPhone, iPad and Watch come from endoflife.date, with dates.
- IBM hardware matches by machine type/model ("1747HC2") or by a marketing name derived from IBM's title ("Power S922"). Sibling models share a name (S922 = 9009-22A / 22G), so the first one is picked at confidence 93; their dates may differ. About a third of IBM rows have no end-of-support date.
- The VMware matrix is behind a login: only public KB tables are collected.
- Scrapers (Microsoft, Juniper) depend on page layouts and each site's terms.
- Accuracy figures come from a synthetic generator; the public inventory has no answer key.
