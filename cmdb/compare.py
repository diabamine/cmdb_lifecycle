"""Step 6: match the same inputs against several reference catalogs and show where they disagree."""
import csv
import re
from collections import Counter
from pathlib import Path

from cmdb.matching.normalize import norm
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


_FIELDS = ["matched_product", "match_method", "match_confidence", "review", "lifecycle_status", "eol_date"]


def _agreement(product_names):
    # Compared by normalised name, not product_id: ids carry the catalog they come from ("user:file:..." vs "eol:..."),
    # so the same product in two catalogs would always look like a conflict.
    matched = [name for name in product_names if name]
    if not matched:
        return "none-matched"
    if len(set(matched)) > 1:
        return "conflict"
    if len(matched) < len(product_names):
        return "partial"
    return "all"


def compare_references(rows, references, match_fn=None):
    if match_fn is None:
        # lazy so this module is importable (and testable) without the matcher
        from cmdb.matching.matcher import match_rows
        match_fn = match_rows

    results = {name: match_fn(rows, catalog) for name, catalog in references.items()}
    return compare_results(rows, results)


def compare_results(rows, results):
    """Same as compare_references, for matches already computed: results = {reference name: match_rows output}."""
    compared = []
    for i, row in enumerate(rows):
        out = dict(row)
        product_names = []
        for name, matches in results.items():
            m = matches[i]
            for field in _FIELDS:
                out[f"{name}:{field}"] = m.get(field, "")
            product_names.append(norm(m.get("matched_product", "")))
        out["agree"] = _agreement(product_names)
        compared.append(out)
    return compared


def _reference_names(compared):
    if not compared:
        return []
    suffix = ":matched_product"
    return [key[: -len(suffix)] for key in compared[0] if key.endswith(suffix)]


def comparison_summary(compared):
    """Rows: agree counts and rows whose eol dates differ between references."""
    n = len(compared)
    summary = []
    counts = Counter(r["agree"] for r in compared)
    for value in ("all", "partial", "conflict", "none-matched"):
        summary.append({"kind": "agree", "name": value, "count": counts.get(value, 0)})

    names = _reference_names(compared)
    for r in compared:
        dates = {name: r[f"{name}:eol_date"] for name in names if r[f"{name}:eol_date"]}
        if len(set(dates.values())) > 1:
            detail = ", ".join(f"{k}={v}" for k, v in dates.items())
            label = f"{r.get('vendor', '')} {r.get('product', '')} {r.get('version', '')}".strip()
            summary.append({"kind": "eol_differs", "name": label, "detail": detail})
    return summary


def reference_coverage(compared):
    """One row per reference: how many input rows it matched, auto-accepted, sends to review, or leaves unmatched (in %)."""
    total = len(compared)
    coverage = []
    for name in _reference_names(compared):
        matched = [r for r in compared if r[f"{name}:matched_product"]]
        auto = [r for r in matched if not r[f"{name}:review"]]
        coverage.append({
            "reference": name,
            "rows": total,
            "matched": len(matched),
            "matched_pct": _pct(len(matched), total),
            "auto_accept_pct": _pct(len(auto), total),
            "review_pct": _pct(len(matched) - len(auto), total),
            "unmatched_pct": _pct(total - len(matched), total),
        })
    return coverage


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0
