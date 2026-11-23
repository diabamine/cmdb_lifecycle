# CMDB Lifecycle Matcher

Match dirty CMDB rows (vendor, product, version) to canonical products, then attach end-of-life dates and known
vulnerabilities. Everything runs **offline** against a snapshot that is fetched once.

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m cmdb.ingest        # offline: committed raw files in snapshot/ -> data/standard/*.csv
.venv/bin/python -m cmdb.catalog       # data/standard/*.csv -> data/master_catalog.csv
.venv/bin/streamlit run streamlit_app.py
./run_all.sh                           # full refresh incl. network fetch (needs internet)
```

## Pipeline
| Step | Code | Output |
|---|---|---|
| 1. Ingest (API + scraping) | `cmdb/ingest/<source>.py`, `python -m cmdb.ingest [--fetch] [--only ...]` | raw json in `snapshot/`, standardized `data/standard/<source>.csv` |
| 2. Master catalog | `cmdb/catalog.py`, `python -m cmdb.catalog` | `data/master_catalog.csv` (one row per product release cycle) |
| 3. Matching | `cmdb/matching/` (alias, exact on normalized names, fuzzy, cycle, lifecycle), `python -m cmdb.matching --cmdb in.csv --out out.csv` | matched rows; `cmdb/exposure.py` optionally adds KEV / EPSS / NVD columns |
| 4. UI | `streamlit_app.py`: search catalog, match text or CSV, metrics, compare | |
| 5. Metrics | `cmdb/metrics.py`: catalog coverage per source, matching rates | |
| 6. Compare | `cmdb/compare.py`: match the same rows against several catalogs (your own component catalog CSV, single sources) | |

Column contracts for every step live in `cmdb/schema.py`. `tools/` holds test-data helpers (`make_synthetic_cmdb.py`,
`fetch_inventory.py`), the NVD index builder, and `score.py` (accuracy against the synthetic answer key). Tests: `pytest -q`.

## Known limits
- Cisco: the EoX v5 collector is tested only against a simulated API, never the live one. It needs a Cisco ID enabled for the Support APIs (Smart Net Total Care or Partner Support Service); a free Cisco.com account gets "HTTP 403 Developer Inactive". Run it with `CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -m cmdb.ingest --fetch --only cisco`; a failed run leaves existing Cisco data untouched. Without access there is no scraper fallback yet. NetApp needs a manual CSV in `manual/`.
- Hardware without a collector: Dell, HP/HPE, Sun/Oracle, Alcatel-Lucent Enterprise (Dell and HP answer scripted clients with HTTP 403; the others publish no bulk list). NVIDIA networking (Mellanox) EOL notices are PDFs and are not parsed.
- Apple publishes only "vintage" / "obsolete" model lists, no dates: obsolete = EOL, vintage stays UNKNOWN. iPhone, iPad and Watch come from endoflife.date, with dates.
- IBM hardware matches by machine type/model ("1747HC2") or by a marketing name derived from IBM's title ("Power S922"). Sibling models share a name (S922 = 9009-22A / 22G), so the first one is picked at confidence 93; their dates may differ. About a third of IBM rows have no end-of-support date.
- The VMware matrix is behind a login: only public KB tables are collected.
- Scrapers (Microsoft, Juniper) depend on page layouts and each site's terms.
- Accuracy figures come from a synthetic generator; the public inventory has no answer key.
