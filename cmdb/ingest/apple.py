"""Apple: public service-status page lists model names under "vintage" / "obsolete" with NO dates.

obsolete -> status 'obsolete' (standardized to is_eol=true); vintage -> unknown (no flag).
"""
import re
from pathlib import Path

from cmdb.ingest.common import (CollectorError, clean, get_text, feed_records, rec, replace_vendor_records,
                                vendor_rows)

VENDOR = "apple"
URL = "https://support.apple.com/en-us/102772"
# iPhone/iPad/Watch are in endoflife.date WITH dates: a date-less record here would outrank them.
SKIP_FAMILIES = {"iphone", "ipad", "watch"}


def fetch(raw_dir: Path) -> None:
    records = parse_page(get_text(URL))
    if len(records) < 100:
        raise CollectorError(f"apple: only {len(records)} models parsed, expected hundreds (page layout changed?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def parse_page(page: str) -> list[dict]:
    out = []
    for section in re.split(r"(?=<h2[ >])", page):
        # heading ids: macvintage, macobsolete, ipodobsolete, tvobs ...
        match = re.match(r'<h2 id="(.+?)(vintage|obsolete|obs)"', section)
        if not match or match.group(1) in SKIP_FAMILIES:
            continue
        status = "vintage" if match.group(2) == "vintage" else "obsolete"
        for item in re.findall(r"<li[^>]*>.*?</li>", section, flags=re.S):
            name = clean(item)
            if name:
                out.append(rec(VENDOR, name, name, "support.apple.com/102772", URL, "hardware", status=status))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
