"""Step 5: catalog coverage and matching quality numbers, plus a run log for trends."""
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _pct(part, whole):
    if not whole:
        return 0.0
    return round(100 * part / whole, 1)


def _coverage_row(source, rows):
    products = {r["product_id"] for r in rows}
    # a flag-only product has is_eol but no cycle that carries a date
    dated = {r["product_id"] for r in rows if r.get("eol")}
    flagged = {r["product_id"] for r in rows if r.get("is_eol") == "true"}
    n = len(rows)
    return {
        "source": source,
        "products": len(products),
        "cycles": n,
        "pct_eol_date": _pct(sum(1 for r in rows if r.get("eol")), n),
        "pct_release_date": _pct(sum(1 for r in rows if r.get("release_date")), n),
        "pct_vendor": _pct(sum(1 for r in rows if r.get("vendor")), n),
        "pct_hardware": _pct(sum(1 for r in rows if r.get("category") == "hardware"), n),
        "products_flag_only": len(flagged - dated),
    }


def catalog_coverage(catalog):
    """One row per source plus an ALL row."""
    by_source = {}
    for row in catalog:
        by_source.setdefault(row.get("source", ""), []).append(row)
    out = [_coverage_row(name, rows) for name, rows in sorted(by_source.items())]
    out.append(_coverage_row("ALL", catalog))
    return out


def source_freshness(manifest_path):
    """Age in days of the raw snapshot, from snapshot/manifest.json. Returns [] if unreadable."""
    try:
        manifest = json.loads(Path(manifest_path).read_text())
        created = datetime.fromisoformat(manifest["created_utc"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    age = (datetime.now(timezone.utc) - created).days
    files = manifest.get("files", {})
    return [
        {"file": name, "url": info.get("url", ""), "snapshot_utc": manifest["created_utc"], "age_days": age}
        for name, info in sorted(files.items())
    ]


def _norm(text):
    # "-" and spacing differences must not count as a wrong product
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _counts(results, key):
    return dict(Counter(r.get(key, "") for r in results))


def _truth_scores(results, truth):
    """Same definitions as score() in cmdb_enrich.py."""
    truth_by_id = {t["asset_id"]: t for t in truth}
    s = Counter()
    for r in results:
        t = truth_by_id.get(r.get("asset_id"))
        if t is None:
            continue
        got = bool(r.get("matched_product"))
        auto = got and r.get("review", "") == ""
        if not t["truth_product"]:
            s["unknown"] += 1
            s["unknown_false_match"] += got
            continue
        correct = got and _norm(r["matched_product"]) == _norm(t["truth_product"])
        s["known"] += 1
        s["matched"] += got
        s["correct"] += correct
        s["auto"] += auto
        s["auto_correct"] += auto and correct
        s["cycle_correct"] += correct and r.get("matched_cycle", "") == t["truth_cycle"]
    return {
        "known_rows": s["known"],
        "product_recall": _pct(s["correct"], s["known"]),
        "product_precision": _pct(s["correct"], s["matched"]),
        "cycle_accuracy": _pct(s["cycle_correct"], s["known"]),
        "auto_accept_precision": _pct(s["auto_correct"], s["auto"]),
        "unknown_false_match": s["unknown_false_match"],
        "unknown_rows": s["unknown"],
    }


def matching_metrics(results, truth=None):
    n = len(results)
    matched = [r for r in results if r.get("matched_product")]
    auto = [r for r in matched if r.get("review", "") == ""]
    unmatched = [r for r in results if not r.get("matched_product")]
    metrics = {
        "rows": n,
        "match_rate": _pct(len(matched), n),
        "auto_accept_rate": _pct(len(auto), n),
        "review_queue_rate": _pct(len(matched) - len(auto), n),
        "unmatched_rate": _pct(len(unmatched), n),
        "by_match_method": _counts(results, "match_method"),
        "by_lifecycle_status": _counts(results, "lifecycle_status"),
        "unmatched_top_vendors": Counter(r.get("vendor", "") for r in unmatched).most_common(10),
    }
    if any("source" in r for r in results):
        metrics["by_source"] = _counts(results, "source")
    if truth:
        metrics["truth"] = _truth_scores(results, truth)
    return metrics


def append_run_log(path, metrics, label):
    """One JSON line per run so the UI can plot coverage / match rate over time."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "metrics": metrics,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
