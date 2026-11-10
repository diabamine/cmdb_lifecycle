#!/bin/bash
# Refresh EVERY source (needs internet), rebuild the offline indexes, then enrich the whole inventory.
set -e
cd "$(dirname "$0")"
python3 -I fetch_snapshot.py --out snapshot --nvd                  # endoflife.date, Red Hat, CISA KEV, EPSS, full NVD CVE base
python3 -I build_nvd_index.py snapshot/nvd snapshot/nvd.sqlite     # NVD -> compact SQLite
python3 -I fetch_vendors.py --out snapshot                         # Microsoft, Juniper, VMware KB, manual/*.csv (Cisco: set CISCO_CLIENT_ID/SECRET, then --only cisco)
python3 -I fetch_inventory.py                                      # public real-world inventory (winget, Homebrew, Ubuntu)
.venv/bin/python -I cmdb_enrich.py --snapshot snapshot --cmdb raw/inventory.csv --out out_real   # full enrichment (~2 min)
echo ALL-DONE
