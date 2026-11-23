#!/bin/bash
# Refresh every source (needs internet), rebuild the master catalog, then match the public sample inventory.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
$PY -m cmdb.ingest --fetch                                           # steps 1: raw -> data/standard/*.csv (Cisco: set CISCO_CLIENT_ID/SECRET)
$PY -m cmdb.catalog                                                  # step 2: data/master_catalog.csv
$PY tools/fetch_inventory.py                                         # public real-world inventory (winget, Homebrew, Ubuntu)
$PY -m cmdb.matching --cmdb raw/inventory.csv --out out_real/cmdb_enriched.csv   # step 3
echo ALL-DONE
