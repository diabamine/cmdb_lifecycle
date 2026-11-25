"""Wording and table shaping for the UI. No Streamlit here, so it is easy to read, change and test.

Everything a user sees as a label, a status name or a "why did it match" sentence lives in this file.
"""

# lifecycle_status (as the matcher writes it) -> what the user reads, plus a sort rank (worst first)
STATUS = {
    "EOL": ("End of life", 0),
    "EXTENDED_SUPPORT_ONLY": ("Extended support only", 1),
    "MAINTENANCE_ONLY": ("Maintenance only", 2),
    "SUPPORTED": ("Supported", 3),
    "UNKNOWN": ("No date in catalog", 4),
    "VERSION_UNKNOWN": ("Version not in catalog", 5),
    "UNMATCHED": ("Not matched", 6),
    "NO_SOURCE": ("Hardware, no source", 7),
    "NO_PRODUCT": ("Not a catalog product", 8),
}
PAST_EOL = ("EOL", "EXTENDED_SUPPORT_ONLY")

METHOD_TEXT = {
    "alias": "You confirmed this match earlier (saved alias).",
    "exact": "The product name matches exactly.",
    "exact+cleaned": "The name matches once vendor words, versions and punctuation are ignored.",
    "exact+rule": "The name matches through a built-in naming rule (for example a package mapped to its upstream product).",
    "exact+rollup": "The name matches a product family (for example a model rolled up to its series).",
    "fuzzy": "The name is similar but not identical: worth a check.",
    "alias-none": "You marked this item as not being a catalog product.",
    "none": "No product in this catalog looks like this one.",
}
CYCLE_TEXT = {
    "name": "The version is a release cycle of its own ({cycle}).",
    "exact": "The version is release cycle {cycle}.",
    "prefix": "The version is a patch release inside cycle {cycle}.",
    "major-ambiguous": "Only the major version is known, so the newest cycle of that major version ({cycle}) is shown: check it.",
    "no-version": "The row has no usable version, so no release cycle was chosen.",
    "cycle-not-found": "The catalog has no release cycle for that version.",
    "single-cycle": "The product has a single release cycle (typical for a hardware model), so it was used.",
    "product-level": "The exact version is unknown, so the product's latest end date was used.",
    "from-name": "The version was read from the product name (cycle {cycle}).",
}

RESULT_COLUMNS = ["Vendor", "Product", "Version", "Matched to", "Lifecycle", "End of life", "Days left", "Confidence", "To check"]


def status_label(status):
    return STATUS.get(status, (status or "", 99))[0]


def status_options(results):
    """Lifecycle labels present in the results, worst first, for a filter."""
    present = {r.get("lifecycle_status") or "" for r in results}
    return [status_label(s) for s in sorted(present, key=lambda s: STATUS.get(s, ("", 99))[1])]


def label_to_status(label):
    return next((status for status, (text, _) in STATUS.items() if text == label), label)


def results_table(results):
    """Match results -> rows with plain column names, in the same order (position i is results[i])."""
    rows = []
    for r in results:
        days = r.get("days_to_eol")
        rows.append({
            "Vendor": r.get("vendor", ""), "Product": r.get("product", ""), "Version": r.get("version", ""),
            "Matched to": r.get("matched_product", ""), "Lifecycle": status_label(r.get("lifecycle_status")),
            "End of life": r.get("eol_date") or "", "Days left": days if days != "" else None,
            "Confidence": r.get("match_confidence", 0) if r.get("matched_product") else None,
            "To check": r.get("review", ""),
        })
    return rows


def filter_results(results, text="", statuses=(), review_only=False):
    """Positions-preserving filter: returns the matching results (same dict objects)."""
    words = (text or "").lower().split()
    wanted = {label_to_status(s) for s in statuses}
    kept = []
    for r in results:
        haystack = " ".join(str(r.get(k, "")) for k in ("vendor", "product", "version", "fw_or_pkg", "matched_product")).lower()
        if words and not all(w in haystack for w in words):
            continue
        if wanted and r.get("lifecycle_status") not in wanted:
            continue
        if review_only and not r.get("review"):
            continue
        kept.append(r)
    return kept


def headline(results):
    """The four numbers every view leads with."""
    n = len(results)
    matched = [r for r in results if r.get("matched_product")]
    dated = [r for r in matched if r.get("eol_date")]
    past = [r for r in matched if r.get("lifecycle_status") in PAST_EOL]
    check = [r for r in matched if r.get("review")]
    pct = lambda part, whole: 100 * part / whole if whole else 0.0
    return {
        "rows": n,
        "matched": (len(matched), pct(len(matched), n)),
        "dated": (len(dated), pct(len(dated), n)),
        "past_eol": (len(past), pct(len(past), n)),
        "to_check": (len(check), pct(len(check), n)),
    }


def explain(result):
    """A few plain sentences: what it matched, why, and what the lifecycle says."""
    lines = []
    method = result.get("match_method") or "none"
    if not result.get("matched_product"):
        lines.append(METHOD_TEXT.get(method, METHOD_TEXT["none"]))
        if result.get("review"):
            lines.append(f"To check: {result['review']}.")
        return lines
    lines.append(f"Matched to **{result['matched_product']}** ({result.get('matched_source', '')}), "
                 f"confidence {result.get('match_confidence', 0):.0f}/100. " + METHOD_TEXT.get(method, ""))
    how = result.get("cycle_match") or ""
    cycle = result.get("matched_cycle") or ""
    text = CYCLE_TEXT.get(how.split(":")[0])
    if text:
        lines.append(text.format(cycle=cycle))
    eol, days = result.get("eol_date"), result.get("days_to_eol")
    if eol:
        when = "" if days in ("", None) else (f" ({-days:,} days ago)" if days < 0 else f" (in {days:,} days)")
        lines.append(f"Lifecycle: **{status_label(result.get('lifecycle_status'))}**, end of life {eol}{when}.")
    else:
        lines.append(f"Lifecycle: **{status_label(result.get('lifecycle_status'))}**. The catalog gives no end-of-life date for this cycle.")
    if result.get("newer_cycle"):
        lines.append(f"A newer supported cycle exists: {result['newer_cycle']}.")
    if result.get("review"):
        lines.append(f"To check: {result['review']}.")
    if result.get("dq_flags"):
        lines.append(f"Data notes: {result['dq_flags'].replace(';', ', ')}.")
    return lines


def top_unmatched(results, limit=15):
    """Most frequent products the catalog could not match: what to add to the catalog first."""
    counts = {}
    for r in results:
        if not r.get("matched_product"):
            key = (r.get("vendor", ""), r.get("product", ""))
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: -item[1])[:limit]
    return [{"Vendor": vendor, "Product": product, "Rows": n} for (vendor, product), n in ranked]
