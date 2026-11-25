"""Read catalog and CMDB files. The only data-loading layer the UI needs: no ingestion, no catalog building.

  load_catalog(path)               a catalog already in STANDARD_COLUMNS (data/master_catalog.csv)
  parse_catalog(data, name)        a user's own catalog: loose headers (vendor, product, version, eol ...) -> standard rows
  load_component_catalog(path)     same, from a file
  parse_cmdb(data)                 a CMDB extract (needs a `product` column) -> list of dicts
"""
import csv
import io
import re
from pathlib import Path

from cmdb.schema import STANDARD_COLUMNS

# accepted header spellings -> standard key (compared lowercase, spaces/dashes as "_")
_HEADERS = {
    "vendor": "vendor", "manufacturer": "vendor", "publisher": "vendor",
    "product": "product", "name": "product", "component": "product", "product_name": "product",
    "version": "version", "release": "version", "cycle": "version",
    "aliases": "aliases", "alias": "aliases", "also_known_as": "aliases",
    "category": "category", "type": "category",
    "eol": "eol", "end_of_life": "eol", "eol_date": "eol",
    "release_date": "release_date", "released": "release_date",
    "end_support": "end_support", "end_of_support": "end_support",
    "end_sale": "end_sale", "end_extended": "end_extended",
    "latest": "latest", "url": "url",
}


def _header_key(name):
    return _HEADERS.get(re.sub(r"[\s-]+", "_", name.strip().lower()), "")


def load_component_catalog(path):
    path = Path(path)
    out = []
    seen = set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mapping = {h: _header_key(h) for h in (reader.fieldnames or [])}
        for line in reader:
            row = dict.fromkeys(STANDARD_COLUMNS, "")
            for header, value in line.items():
                key = mapping.get(header, "")
                if key and value:
                    row[key] = value.strip()
            if not row["product"]:
                continue
            row["aliases"] = "|".join(a.strip() for a in re.split(r"[|;]", row["aliases"]) if a.strip())
            row["source"] = path.stem
            row["product_id"] = f"user:{path.stem}:{row['vendor']}:{row['product']}"
            row["id"] = f"{row['product_id']}@{row['version']}"
            # keep ids unique when the file repeats a vendor/product/version
            base, n = row["id"], 1
            while row["id"] in seen:
                n += 1
                row["id"] = f"{base}#{n}"
            seen.add(row["id"])
            out.append(row)
    return out


def load_catalog(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_catalog(data, name):
    """Catalog bytes or text -> standard rows. `name` (a file name or stem) names the catalog in source and ids."""
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    stem = Path(name).stem
    out, seen = [], set()
    reader = csv.DictReader(io.StringIO(text, newline=""))
    mapping = {h: _header_key(h) for h in (reader.fieldnames or [])}
    for line in reader:
        row = dict.fromkeys(STANDARD_COLUMNS, "")
        for header, value in line.items():
            key = mapping.get(header, "")
            if key and value:
                row[key] = value.strip()
        if not row["product"]:
            continue
        row["aliases"] = "|".join(a.strip() for a in re.split(r"[|;]", row["aliases"]) if a.strip())
        row["source"] = stem
        row["product_id"] = f"user:{stem}:{row['vendor']}:{row['product']}"
        row["id"] = f"{row['product_id']}@{row['version']}"
        # keep ids unique when the file repeats a vendor/product/version
        base, n = row["id"], 1
        while row["id"] in seen:
            n += 1
            row["id"] = f"{base}#{n}"
        seen.add(row["id"])
        out.append(row)
    return out


def load_component_catalog(path):
    path = Path(path)
    return parse_catalog(path.read_bytes(), path.name)


def parse_cmdb(data):
    """CMDB extract bytes -> dicts. An extract may lack the vendor column, so default it to ''."""
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    for row in rows:
        row.setdefault("vendor", "")
    return rows
