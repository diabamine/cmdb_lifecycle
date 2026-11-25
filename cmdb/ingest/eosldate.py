"""eosl.date: end-of-life / end-of-support tables for hardware vendors, scraped from https://eosl.date/vendor/<slug>/.

One public page per vendor (robots.txt allows crawling), a few seconds apart. The pages hold several tables per product
line with different columns, so each table is read by its header names, not by position.

  snapshot/vendor_lifecycle_eosldate.json   vendor-feed records, the input of standardize()
  python -m cmdb.ingest --fetch --only eosldate

A vendor that fails (or parses to nothing, e.g. layout change) keeps its previous records; the others are still merged.
Attribution: eosl.date. Not authoritative: verify dates with the vendor before acting.
"""
import json
import re
import time
from pathlib import Path

from cmdb.ingest.common import SNAPSHOT_UA, CollectorError, get, read_json_list, tables, vendor_rows, write_atomic

BASE = "https://eosl.date"
SOURCE = "eosl.date"
FEED_FILE = "vendor_lifecycle_eosldate.json"
PAUSE_SECONDS = 2

# eosl.date slug -> vendor name used in the master catalog
VENDORS = {
    "apple": "apple", "brocade": "brocade", "cisco": "cisco", "dell-emc": "dell", "hpe": "hpe", "ibm": "ibm",
    "juniper-networks": "juniper", "netapp": "netapp",
}

# header text (lowercased) -> role; the first header found for a role wins
ROLES = {
    "model": ("model number", "model name", "product name"),
    "version": ("version number", "version"),
    "release": ("release date", "launch date"),
    "end_sale": ("end of sale date", "end of life date", "last order"),
    "eol": ("end of support", "end of support date", "end of support (eosl)", "end of service date", "end of service", "end ofsupport"),
}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = re.compile(r"^(?:(\d{4})-(\d{2})-(\d{2})|([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4}))")


def parse_date(text: str) -> str:
    """'2030-04-30', 'Oct. 01, 2019', 'Jul. 31, 2028 Support continues for 1 year' -> ISO day; 'Not Announced', '' -> ''."""
    match = DATE.match((text or "").strip())
    if not match:
        return ""
    if match.group(1):
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    month = MONTHS.get(match.group(4).lower())
    return f"{match.group(6)}-{month:02d}-{int(match.group(5)):02d}" if month else ""


def parse_page(page: str, vendor: str, slug: str) -> list[dict]:
    """HTML of one vendor page -> vendor-feed records (tables without a Product column or without dates are ignored)."""
    url = f"{BASE}/vendor/{slug}/"
    records = []
    for table in tables(page):
        if not table or table[0][:1] != ["Product"]:
            continue
        header = [h.lower() for h in table[0]]
        column = {role: next((header.index(h) for h in names if h in header), None) for role, names in ROLES.items()}
        if column["eol"] is None and column["end_sale"] is None:
            continue
        line = header.index("product line") if "product line" in header else None

        def cell(row, role):
            return row[column[role]] if column[role] is not None else ""

        for row in table[1:]:
            if len(row) != len(header):
                continue                                         # ad slots and other non-data rows
            model, version = cell(row, "model"), cell(row, "version")
            product = row[0] or model or version                 # Apple's macOS rows have an empty Product cell
            if not product:
                continue
            aliases = [a for a in (model, version, row[line] if line is not None else "") if a and a != product]
            records.append(dict(
                vendor=vendor, product=product, label_alias=list(dict.fromkeys(aliases)), cycle=version or model or product,
                release_date=parse_date(cell(row, "release")), end_sale=parse_date(cell(row, "end_sale")),
                end_support_full="", eol=parse_date(cell(row, "eol")), end_security_support="", end_extended="",
                status="", category="software" if column["version"] is not None else "hardware",
                source=SOURCE, url=url))
    return records


def fetch(raw_dir: Path, vendors=None) -> None:
    """Scrape the vendor pages (default: all of VENDORS) into the feed file. Raises CollectorError if any vendor failed."""
    path = raw_dir / FEED_FILE
    feed = read_json_list(path)
    failed = []
    for n, slug in enumerate(vendors or VENDORS):
        if slug not in VENDORS:
            raise CollectorError(f"eosl.date: unknown vendor slug {slug!r}; known: {', '.join(VENDORS)}")
        if n:
            time.sleep(PAUSE_SECONDS)
        vendor = VENDORS[slug]
        try:
            page = get(f"{BASE}/vendor/{slug}/", headers={"User-Agent": SNAPSHOT_UA}, timeout=120).decode("utf-8", "ignore")
            records = parse_page(page, vendor, slug)
            if not records:
                raise CollectorError("page parsed to no records (layout changed?)")
        except CollectorError as err:
            print(f"eosl.date {slug}: FAILED, keeping previous records: {err}")
            failed.append(slug)
            continue
        feed = [r for r in feed if r["vendor"] != vendor] + records
        write_atomic(path, json.dumps(feed, indent=0).encode())
        print(f"eosl.date {slug}: {len(records)} records")
    if failed:
        raise CollectorError(f"eosl.date: failed vendors: {', '.join(failed)}")


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
