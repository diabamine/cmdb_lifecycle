# CMDB Lifecycle Matcher

Match dirty CMDB rows (vendor, product, version) to canonical products, then attach end-of-life dates and known
vulnerabilities. Everything runs **offline** against a snapshot that is fetched once.

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run_all.sh                       # fetch every source, build the NVD index, enrich the public sample inventory (needs internet)
.venv/bin/streamlit run app.py     # explorer, source coverage, review queue
```

## How it fits together
| File | Role |
|---|---|
| `fetch_snapshot.py` | endoflife.date, Red Hat lifecycle, CISA KEV, EPSS, full NVD CVE base |
| `build_nvd_index.py` | reduces NVD to a compact SQLite index (CVEs, affected-version ranges, products) |
| `fetch_vendors.py` | Microsoft, Juniper, VMware (public KB), Cisco EoX v5 (needs credentials), `manual/*.csv` |
| `fetch_inventory.py` | public real-world inventory (winget, Homebrew, Ubuntu) used as test data |
| `make_synthetic_cmdb.py` | dirty synthetic CMDB with an answer key, for scoring |
| `cmdb_enrich.py` | the matching engine: alias, exact, fuzzy, version to cycle, lifecycle status, NVD |
| `app.py` | Streamlit app: match explorer, coverage by source, review queue that writes `aliases.csv` |
| `recap.html`, `AI_USECASES.md` | project recap and ideas for AI-assisted steps |

Run the engine on any CSV with `vendor, product, version` (optional `ci_type`, `fw_or_pkg`):
```bash
.venv/bin/python -I cmdb_enrich.py --snapshot snapshot --cmdb your.csv --out out_yours
```

## Known limits
- Cisco collector is written but untested against the live API; NetApp needs a manual CSV in `manual/`.
- The VMware matrix is behind a login: only public KB tables are collected.
- Scrapers (Microsoft, Juniper) depend on page layouts and each site's terms.
- Accuracy figures come from a synthetic generator; the public inventory has no answer key.
