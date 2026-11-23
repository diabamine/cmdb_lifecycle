"""NVIDIA Jetson: lifecycle page tables with month-precision dates (-> last day of the month).

Networking (Mellanox) EOL notices are PDFs and are not collected (stdlib only, no PDF parser).
"""
from pathlib import Path

from cmdb.ingest.common import (CollectorError, get_text, month_end, feed_records, rec, replace_vendor_records,
                                tables, vendor_rows)

VENDOR = "nvidia"
URL = "https://developer.nvidia.com/embedded/lifecycle"
SOURCE = "developer.nvidia.com/embedded/lifecycle"


def fetch(raw_dir: Path) -> None:
    records = parse_tables(tables(get_text(URL)))
    if len(records) < 10:
        raise CollectorError(f"nvidia: only {len(records)} Jetson rows, expected ~30 (page layout changed?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def parse_tables(page_tables: list) -> list[dict]:
    out = []
    for table in page_tables:
        if not table or len(table[0]) != 2:
            continue
        head = table[0][1].lower()
        # skips the developer-kit table, which only has names
        if "available through" not in head and "date of eol" not in head:
            continue
        is_eol_date = "date of eol" in head          # EOL date vs 'available through' (= last ship month)
        for row in table[1:]:
            date = month_end(row[1]) if len(row) == 2 else ""
            if not date:
                continue
            out.append(rec(VENDOR, row[0], row[0], SOURCE, URL, "hardware", end_sale=date,
                           eol=date if is_eol_date else ""))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
