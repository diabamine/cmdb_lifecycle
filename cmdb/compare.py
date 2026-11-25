"""Step 6: match the same inputs against several reference catalogs and show where they disagree."""
from collections import Counter

from cmdb.inputs import load_component_catalog  # noqa: F401  (the reader lives in cmdb.inputs; kept importable from here)
from cmdb.matching.normalize import norm


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
