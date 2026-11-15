# Running the CMDB Lifecycle Matcher Offline

This guide explains which data files are necessary for the app to run on an offline CMDB dataset.

## Data Files Overview

All reference data is in the `snapshot/` directory. The app gracefully handles missing optional files.

### ✅ REQUIRED (Core Functionality)
These two files MUST be present for the matcher to work:

| File | Size | Purpose | Created by |
|------|------|---------|-----------|
| `snapshot/endoflife_full.json` | 3.0M | 700+ mainstream products (OS, databases, frameworks, languages) | `fetch_snapshot.py --eol` |
| `snapshot/redhat_lifecycle.json` | 2.0M | Red Hat products | `fetch_snapshot.py --redhat` |

**Without these:** The app will fail to load—it can't match any products.

### 🔄 RECOMMENDED (Better Coverage)
These files improve matching accuracy and coverage:

| File | Size | Purpose | Created by | Impact if missing |
|------|------|---------|-----------|-------------------|
| `snapshot/vendor_lifecycle.json` | 988K | Microsoft, Juniper, Cisco, VMware, NetApp | `fetch_vendors.py` | ~200 products unavailable; Windows/Juniper/Cisco hardware won't match |
| `snapshot/cisa_kev.json` | 1.8M | CISA known-exploited vulnerabilities (by product) | `fetch_snapshot.py --kev` | Can't flag exploited CVEs; `kev_cves` / `kev_ransomware` columns empty |
| `snapshot/epss_scores.csv.gz` | 2.8M | Exploit probability scores for CVEs (paired with KEV) | `fetch_snapshot.py --epss` | Can't show EPSS scores; `epss_max` column empty |

**Without these:** Matching works, but with reduced vendor coverage and no exploit risk data.

### 📊 OPTIONAL (Version-Level CVE Details)
These enhance vulnerability analysis:

| File | Size | Purpose | Created by | Impact if missing |
|------|------|---------|-----------|-------------------|
| `snapshot/nvd.sqlite` | 302M | Full NVD index (421k CVEs), indexed by vendor:product:version | `fetch_snapshot.py --nvd` + `build_nvd_index.py` | Can't show version-specific CVEs; `nvd_*` columns empty |
| `snapshot/nvd/` | included | Staging files for NVD import | `build_nvd_index.py` | Not needed once nvd.sqlite is built |

**Without these:** No version-level CVE details, but product-level EOL/EOS still works.

## Minimum Setup for Offline Use

To run the Streamlit app with just the core functionality:

```bash
# Minimal snapshot/ directory
snapshot/
├── endoflife_full.json      # ✅ REQUIRED
├── redhat_lifecycle.json    # ✅ REQUIRED
└── (aliases.csv)            # Created by the app, optional at startup
```

**Size: ~5MB** — runs everywhere.

**Enrich CSV command (no extra features):**
```bash
python3 -I cmdb_enrich.py --snapshot snapshot --cmdb your_data.csv --out output
# Produces: matched_product, category, lifecycle_status, eol_date, days_to_eol, etc.
```

## Recommended Setup (Best Experience)

```bash
snapshot/
├── endoflife_full.json           # ✅ REQUIRED
├── redhat_lifecycle.json         # ✅ REQUIRED
├── vendor_lifecycle.json         # 🔄 RECOMMENDED
├── cisa_kev.json                 # 🔄 RECOMMENDED
├── epss_scores.csv.gz            # 🔄 RECOMMENDED
└── (aliases.csv)                 # Created by the app
```

**Size: ~9.6MB** — adds vendor coverage + exploit risk.

## Full Setup (All Features)

```bash
snapshot/
├── endoflife_full.json           # ✅ REQUIRED
├── redhat_lifecycle.json         # ✅ REQUIRED
├── vendor_lifecycle.json         # 🔄 RECOMMENDED
├── cisa_kev.json                 # 🔄 RECOMMENDED
├── epss_scores.csv.gz            # 🔄 RECOMMENDED
├── nvd.sqlite                    # 📊 OPTIONAL (302M)
└── (aliases.csv)                 # Created by the app
```

**Size: ~312MB** — includes version-level CVE details.

## How to Build the Snapshot Offline

If you have internet access on one machine, build the snapshot once and transfer it:

```bash
# On internet-connected machine:
python3 -I fetch_snapshot.py --out snapshot          # 5MB (core only)
python3 -I fetch_vendors.py --out snapshot           # +1MB (vendors)
python3 -I fetch_snapshot.py --out snapshot --nvd    # +305MB (NVD)
python3 -I build_nvd_index.py --snapshot snapshot    # Builds nvd.sqlite

# Transfer snapshot/ folder to offline environment
scp -r snapshot/ offline-server:/path/to/cmdb_lifecycle/
```

## Running the App with Your Own CMDB

```bash
# Streamlit web UI (recommended for exploration + cleanup)
.venv/bin/streamlit run app.py

# Then upload your CSV via the "Your own CSV" uploader

# Or CLI for batch enrichment
python3 -I cmdb_enrich.py --snapshot snapshot --cmdb your_inventory.csv --out output
# Produces: cmdb_enriched.csv, review_queue.csv
```

## Output Columns

**Always available (with core files only):**
```
matched_product, matched_source, category, canonical_vendor, match_method, 
match_confidence, matched_cycle, lifecycle_status, eol_date, days_to_eol, 
support_end, extended_end, newer_cycle, review, dq_flags
```

**With vendor data (+vendor_lifecycle.json):**
- Matches ~200 additional products (Microsoft, Juniper, Cisco, VMware, NetApp)

**With exploit data (+cisa_kev.json + epss_scores.csv.gz):**
```
kev_cves, kev_ransomware, kev_latest_added, epss_max
```

**With NVD (+nvd.sqlite):**
```
nvd_cpe, nvd_match, nvd_cves_total, nvd_cves_version, nvd_max_cvss, 
nvd_critical, nvd_kev, nvd_latest_cve
```

## Troubleshooting

### "ModuleNotFoundError: No module named X"
```bash
pip install -r requirements.txt
```

### "FileNotFoundError: snapshot/endoflife_full.json"
You're missing the REQUIRED core files. The app can't run without them.

### "No rows matched" / "UNMATCHED" for everything
- Check your CSV has columns: `vendor`, `product`, `version`
- Verify the product names are real products (not internal names)
- Check `review_queue.csv` for suggestions

### "NVD identified 0 rows" or CVE columns empty
This is OK if you skipped nvd.sqlite (it's 302MB and optional).

---

**Summary for your offline environment:**
- **Minimum:** Copy `endoflife_full.json` + `redhat_lifecycle.json` (~5MB)
- **Better:** Add `vendor_lifecycle.json` + `cisa_kev.json` + `epss_scores.csv.gz` (~9.6MB total)
- **Complete:** Include `nvd.sqlite` for version-level CVEs (~312MB total)
