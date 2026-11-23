"""IBM: official bulk lifecycle CSV (no login). Hardware only (~2.4k machine types); software would add ~11,800 rows.

GA -> release_date, Withdrawn from Marketing -> end_sale, Transition to Extended/End of Support -> eol,
Completion of Extended -> end_extended. Machine type+model and "(1747-HC2)" become aliases.
"""
import csv
import io
import re
from pathlib import Path

from cmdb.ingest.common import (CollectorError, get, iso, feed_records, rec, replace_vendor_records, vendor_rows)

VENDOR = "ibm"
CSV_URL = "https://www.ibm.com/support/pages/space/product-lifecycle/ibm_product_lifecycle_list.csv"
LIFECYCLE_PAGE = "https://www.ibm.com/support/pages/product-lifecycle-list"
SOURCE = "ibm.com/support/pages/product-lifecycle-list"
TYPES = {"HW"}
REQUIRED_COLUMNS = {"Type", "IBM Product", "MTM", "General Availability"}


def fetch(raw_dir: Path) -> None:
    text = get(CSV_URL).decode("utf-8", "ignore")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not REQUIRED_COLUMNS <= set(rows[0]):
        raise CollectorError("ibm: lifecycle CSV missing or its header changed")
    records = parse_rows(rows)
    if sum(r["category"] == "hardware" for r in records) < 1000:
        raise CollectorError(f"ibm: only {len(records)} rows, expected thousands (format change?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def parse_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["Type"] not in TYPES:
            continue
        name = re.sub(r"\s*-\s*Withdrawal notification\s*$", "", r["IBM Product"]).strip()
        mtm = r["MTM"].strip()
        mtm = "" if mtm in ("", "N/A") else mtm                       # machine type + model, e.g. 1747HC2
        in_title = re.search(r"\((\d{4}-[A-Z0-9]{3})\)", name)        # "(1747-HC2)" in the title
        vrm = r["VRM"].strip()
        vrm = "" if vrm in ("", "N/A") else vrm
        extended_end = iso(r["Completion of Extended, Sustained, Extension availability"])
        support_end = iso(r["Transition to Extended/Sustained, End of Support"])
        hardware = r["Type"] == "HW"
        # "IBM Power System S922 Server (9009-22A)" -> "IBM Power System S922 Server" -> "Power System S922 Server"
        base = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
        short = re.sub(r"^IBM\s+", "", base)
        lean = re.sub(r"\s+", " ", re.sub(r"\b(?:System|Systems|Server)\b", " ", short)).strip()   # "Power S922": how a CMDB spells it
        names = {mtm, in_title.group(1) if in_title else "", base, short, lean} - {"", name}
        cycle = (mtm or name) if hardware else (vrm or name)
        out.append(rec(VENDOR, name, cycle, SOURCE, r.get("More Info URL") or LIFECYCLE_PAGE,
                       "hardware" if hardware else "software",
                       aliases=sorted(a for a in names if len(a) >= 5 or a == mtm),
                       release=iso(r["General Availability"]), end_sale=iso(r["Withdrawn from Marketing"]),
                       eol=support_end or extended_end, end_extended=extended_end))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
