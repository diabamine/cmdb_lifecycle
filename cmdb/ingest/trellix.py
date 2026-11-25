"""Trellix: end-of-life tables from https://www.trellix.com/support/end-of-life-products/.

The page fills its two tables from CSV files, which are the cleanest thing to read:
  /en/data/eol-software-table.csv   Trellix Product, Version Number, End-of-Life
  /en/data/eol-hardware-table.csv   Trellix Product, Model Number, End-of-Sale, End-of-Life, End-of-Support
Dates are m/d/yy; "—" means none announced. A version cell can list several versions ("4.6, 4.4"): one record each.
The server answers scripted clients slowly or not at all, so requests use a Firefox User-Agent and several tries.

  snapshot/vendor_lifecycle_trellix.json   vendor-feed records, the input of standardize()
  python -m cmdb.ingest --fetch --only trellix

Not authoritative: the page says a product that is not listed is obsolete. Verify dates with Trellix before acting.
"""
import csv
import io
import json
from datetime import datetime
from pathlib import Path

from cmdb.ingest.common import CollectorError, get, read_json_list, rec, vendor_rows, write_atomic

BASE = "https://www.trellix.com"
PAGE_URL = f"{BASE}/support/end-of-life-products/"
SOURCE = "trellix"
VENDOR = "trellix"
FEED_FILE = "vendor_lifecycle_trellix.json"
# trellix.com stalls requests from the generic Chrome-style agent but answers a Firefox one
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"
TABLES = {"software": "/en/data/eol-software-table.csv", "hardware": "/en/data/eol-hardware-table.csv"}


def parse_date(text: str) -> str:
    """'6/30/26' or '12/31/2026' -> '2026-06-30'; '—' and '' -> ''."""
    text = (text or "").strip()
    for pattern in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_csv(text: str, kind: str) -> list[dict]:
    """One CSV file -> vendor-feed records (rows without any date are dropped)."""
    records = []
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        product = (row.get("Trellix Product") or "").strip()
        if kind == "software":
            names = [v.strip() for v in (row.get("Version Number") or "").split(",") if v.strip()]
            dates = dict(eol=parse_date(row.get("End-of-Life")))
        else:
            names = [(row.get("Model Number") or "").strip()]
            support = parse_date(row.get("End-of-Support"))
            dates = dict(end_sale=parse_date(row.get("End-of-Sale")), end_full=support,
                         eol=parse_date(row.get("End-of-Life")) or support)
        if not product or not any(dates.values()):
            continue
        for name in filter(None, names):
            records.append(rec(VENDOR, product, name, SOURCE, PAGE_URL, category=kind,
                               aliases=[name] if kind == "hardware" else [], **dates))
    return records


def fetch(raw_dir: Path) -> None:
    """Download both CSV tables into the feed file. Raises CollectorError (previous records kept) on any failure."""
    records = []
    for kind, path in TABLES.items():
        try:
            text = get(BASE + path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en"},
                       tries=4, timeout=30).decode("utf-8", "ignore")
        except CollectorError as err:
            raise CollectorError(f"trellix {kind}: {err}") from err
        found = parse_csv(text, kind)
        if not found:
            raise CollectorError(f"trellix {kind}: no records (layout changed?)")
        print(f"trellix {kind}: {len(found)} records")
        records.extend(found)
    write_atomic(raw_dir / FEED_FILE, json.dumps(records, indent=0).encode())


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
