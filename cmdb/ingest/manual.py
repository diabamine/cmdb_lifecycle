"""Manual CSV exports (NetApp, VMware portal, anything without a public source): manual/*.csv next to snapshot/.

Columns: vendor,product,cycle,release_date,end_sale,end_support_full,eol[,aliases (;-separated),url,category]
"""
import csv
from pathlib import Path

from cmdb.ingest.common import (VENDOR_FEED, is_manual, iso, read_json_list, rec, replace_vendor_records,
                                vendor_rows)


def fetch(raw_dir: Path) -> None:
    """Not a network fetch: copies manual/*.csv into the shared raw vendor feed."""
    folder = raw_dir.parent / "manual"
    records = []
    for path in sorted(folder.glob("*.csv")):
        with open(path, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if not row.get("product"):
                    continue
                aliases = [a for a in (row.get("aliases") or "").split(";") if a]
                records.append(rec(
                    row.get("vendor", path.stem), row["product"], row.get("cycle") or row["product"],
                    f"manual:{path.name}", row.get("url", ""), row.get("category", ""), aliases,
                    iso(row.get("release_date")), iso(row.get("end_sale")), iso(row.get("end_support_full")),
                    iso(row.get("eol"))))
    replace_vendor_records(raw_dir, "manual", records, owns=is_manual)


def standardize(raw_dir: Path) -> list[dict]:
    records = [r for r in read_json_list(raw_dir / VENDOR_FEED) if is_manual(r)]
    return vendor_rows(records)
