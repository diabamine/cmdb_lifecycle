"""Juniper: scrape the HTML end-of-life tables on support.juniper.net/support/eol/product/<family>/ (hardware)."""
import re
import time
from pathlib import Path

from cmdb.ingest.common import (CollectorError, get_text, iso, feed_records, rec, replace_vendor_records, tables,
                                vendor_rows)

VENDOR = "juniper"
BASE = "https://support.juniper.net/support/eol"
# Used when the index page cannot be parsed: families seen on the site.
FALLBACK_FAMILIES = ["ex_series", "qfx_series", "mx_series", "srx_series", "ptx_series", "acx_series"]


def fetch(raw_dir: Path) -> None:
    index = get_text(f"{BASE}/")
    families = sorted(set(re.findall(r"/support/eol/product/([a-z0-9_]+)", index))) or FALLBACK_FAMILIES
    print("juniper families:", families)
    records = []
    for family in families:
        url = f"{BASE}/product/{family}/"
        records.extend(parse_tables(tables(get_text(url)), url))
        time.sleep(0.5)                      # be polite: one page per half second
    if not records:
        raise CollectorError("juniper: no EOL tables found (page layout changed?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def parse_tables(page_tables: list, url: str) -> list[dict]:
    out = []
    for table in page_tables:
        if not table:
            continue
        header = [cell.lower() for cell in table[0]]

        def column(key):
            for i, cell in enumerate(header):
                if key in cell:
                    return i
            return None

        i_sku = column("sku") if column("sku") is not None else column("product")
        i_bulletin = column("technical support bulletin")
        i_announced, i_last_order = column("eol announced"), column("last order")
        i_engineering, i_support = column("end of engineering"), column("end of support")
        if i_sku is None or i_support is None:
            continue

        def date_at(row, i):
            return iso(row[i]) if i is not None else ""

        for row in table[1:]:
            if len(row) <= i_support:
                continue
            skus = [s.strip() for s in row[i_sku].split(",") if s.strip()]
            if i_bulletin is not None:
                label = re.sub(r"^TSB\d+\s*-\s*End of Life Announcement\s*[:\-]?\s*", "", row[i_bulletin],
                               flags=re.I).strip()
            else:
                label = skus[0] if skus else ""
            if not label:
                continue
            out.append(rec(VENDOR, label, label, "support.juniper.net/eol", url, "hardware", aliases=skus,
                           release=date_at(row, i_announced), end_sale=date_at(row, i_last_order),
                           end_full=date_at(row, i_engineering), eol=iso(row[i_support])))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
