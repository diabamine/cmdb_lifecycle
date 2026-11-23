"""Step 6: match the same inputs against several reference catalogs and show where they disagree."""
import csv
import re
from collections import Counter
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


_FIELDS = ["matched_product", "match_method", "match_confidence", "lifecycle_status", "eol_date"]


def _agreement(product_ids):
    matched = [p for p in product_ids if p]
    if not matched:
        return "none-matched"
    if len(set(matched)) > 1:
        return "conflict"
    if len(matched) < len(product_ids):
        return "partial"
    return "all"


def compare_references(rows, references, match_fn=None):
    if match_fn is None:
        # lazy so this module is importable (and testable) without the matcher
        from cmdb.matching.matcher import match_rows
        match_fn = match_rows

    results = {name: match_fn(rows, catalog) for name, catalog in references.items()}
    compared = []
    for i, row in enumerate(rows):
        out = dict(row)
        product_ids = []
        for name, matches in results.items():
            m = matches[i]
            for field in _FIELDS:
                out[f"{name}:{field}"] = m.get(field, "")
            # a row without a matched product has no meaningful product_id
            product_ids.append(m.get("product_id", "") if m.get("matched_product") else "")
        out["agree"] = _agreement(product_ids)
        compared.append(out)
    return compared


def _reference_names(compared):
    if not compared:
        return []
    suffix = ":matched_product"
    return [key[: -len(suffix)] for key in compared[0] if key.endswith(suffix)]


def comparison_summary(compared):
    """Rows: agree counts, per-reference match rate, and rows whose eol dates differ."""
    n = len(compared)
    summary = []
    counts = Counter(r["agree"] for r in compared)
    for value in ("all", "partial", "conflict", "none-matched"):
        summary.append({"kind": "agree", "name": value, "count": counts.get(value, 0)})

    names = _reference_names(compared)
    for name in names:
        hits = sum(1 for r in compared if r[f"{name}:matched_product"])
        summary.append({
            "kind": "match_rate", "name": name, "count": hits,
            "pct": round(100 * hits / n, 1) if n else 0.0,
        })

    for r in compared:
        dates = {name: r[f"{name}:eol_date"] for name in names if r[f"{name}:eol_date"]}
        if len(set(dates.values())) > 1:
            detail = ", ".join(f"{k}={v}" for k, v in dates.items())
            label = f"{r.get('vendor', '')} {r.get('product', '')} {r.get('version', '')}".strip()
            summary.append({"kind": "eol_differs", "name": label, "detail": detail})
    return summary
