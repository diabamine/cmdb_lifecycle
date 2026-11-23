"""VMware: the lifecycle matrix is behind the Broadcom login, so use the public KB "End of General Support" tables."""
import re
from pathlib import Path

from cmdb.ingest.common import (CollectorError, get_text, iso, feed_records, rec, replace_vendor_records, tables,
                                vendor_rows)

VENDOR = "vmware"
# "End of General Support for vSphere 7.0 / 8.0 / 6.5-6.7-7.0": public articles.
KB_ARTICLES = [415405, 459135, 326984]


def fetch(raw_dir: Path) -> None:
    records = []
    for kb in KB_ARTICLES:
        url = f"https://knowledge.broadcom.com/external/article/{kb}"
        records.extend(parse_tables(tables(get_text(url)), kb, url))
    if not records:
        raise CollectorError("vmware: no KB tables found (page layout changed?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def parse_tables(page_tables: list, kb: int, url: str) -> list[dict]:
    out = []
    for table in page_tables:
        if not table or "product version" not in table[0][0].lower():
            continue
        for row in table[1:]:
            # 'ESXi 7.x' -> product ESXi, cycle 7.x
            match = re.match(r"(.+?)\s+(\d[\w.]*)$", row[0]) if len(row) >= 3 else None
            if match:
                out.append(rec(VENDOR, match.group(1), match.group(2), f"knowledge.broadcom.com/{kb}", url,
                               "virtualization", release=iso(row[1]), end_full=iso(row[2]), eol=iso(row[2])))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
