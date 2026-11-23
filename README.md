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
| `fetch_vendors.py` | Microsoft, Juniper, VMware (public KB), IBM hardware (official bulk CSV), Apple (status only), NVIDIA Jetson, Cisco EoX v5 (needs credentials), `manual/*.csv` |
| `fetch_eolnetwork.py` | Bulk hardware lifecycle from the eol.network API (free account + key; see its docstring). Writes `snapshot/vendor_lifecycle_eolnetwork.json`, loaded by the engine; kept out of git (terms forbid republishing) |
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
- Cisco: the EoX v5 collector is tested only against a simulated API, never the live one. It needs a Cisco ID enabled for the Support APIs (Smart Net Total Care or Partner Support Service); a free Cisco.com account gets "HTTP 403 Developer Inactive". Run it with `CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -I fetch_vendors.py --only cisco`; a failed run leaves existing Cisco data untouched. Without access there is no scraper fallback yet. NetApp needs a manual CSV in `manual/`.
- Hardware without a collector: Dell, HP/HPE, Sun/Oracle, Alcatel-Lucent Enterprise (Dell and HP answer scripted clients with HTTP 403; the others publish no bulk list). NVIDIA networking (Mellanox) EOL notices are PDFs and are not parsed.
- Apple publishes only "vintage" / "obsolete" model lists, no dates: obsolete = EOL, vintage stays UNKNOWN. iPhone, iPad and Watch come from endoflife.date, with dates.
- IBM hardware matches by machine type/model ("1747HC2") or by a marketing name derived from IBM's title ("Power S922"). Sibling models share a name (S922 = 9009-22A / 22G), so the first one is picked at confidence 93; their dates may differ. About a third of IBM rows have no end-of-support date.
- The VMware matrix is behind a login: only public KB tables are collected.
- Scrapers (Microsoft, Juniper) depend on page layouts and each site's terms.
- Accuracy figures come from a synthetic generator; the public inventory has no answer key.
