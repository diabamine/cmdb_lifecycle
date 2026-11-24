"""Public matching API. Cascade: human alias -> exact (normalised) -> fuzzy with vendor signal, then version -> cycle.
Takes and returns plain dicts; never imports streamlit or pandas."""
import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz, process

from cmdb.matching.index import CatalogIndex, build_index
from cmdb.matching.lifecycle import (CYCLE_PROBLEMS, days_to_eol, lifecycle_status, newer_cycle, resolve_cycle,
                                     source_mismatch)
from cmdb.matching.normalize import GENERIC_VENDORS, digit_tokens, name_candidates, norm, norm_vendor, vend_ok
from cmdb.schema import MATCH_COLUMNS

AUTO, REVIEW = 90, 85    # score thresholds: >= AUTO is auto-accepted, >= REVIEW goes to a human
ALIAS_FIELDS = ["vendor", "product", "target_id"]

# Confidence per kind of spelling that matched exactly.
RULE_CONFIDENCE = {"rollup": 92, "rollup-lang": 91, "rule": 95}
GUESSED_KINDS = ("vstrip", "rule", "rollup", "rollup-lang")     # spellings we invented: they must agree with the vendor


# ------------------------------------------------------------------ aliases (human decisions)
def load_aliases(path):
    """{(vendor key, product key): target product_id, or "-" = confirmed not a catalog product}."""
    aliases = {}
    if not Path(path).exists():
        return aliases
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            aliases[(norm_vendor(row["vendor"]), norm(row["product"]))] = row["target_id"]
    return aliases


def save_alias(path, vendor, product, target_id):
    """Append one decision. Later lines win when the file is loaded."""
    path = Path(path)
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(ALIAS_FIELDS)
        writer.writerow([vendor, product, target_id])


# ------------------------------------------------------------------ product lookup
def pick_product(index, ids, vendor_key):
    """Several products share a name: prefer the vendor's own, then official feeds over aggregators."""
    ids = list(ids)
    if len(ids) > 1 and vendor_key:
        same_vendor = [i for i in ids if norm_vendor(index.products[i]["vendor"]) == vendor_key]
        ids = same_vendor or ids
    if len(ids) > 1:
        preferred_aggregator = "redhat" if vendor_key == "redhat" else "endoflife.date"

        def rank(product_id):
            source = index.products[product_id]["source"]
            if source not in ("endoflife.date", "redhat"):
                return 0                      # official vendor feed
            return 1 if source == preferred_aggregator else 2
        ids.sort(key=lambda i: (rank(i), i))
    return ids[0]


def find_alias(query, vendor_key, aliases):
    product_key = norm(query.get("product"))
    return aliases.get((vendor_key, product_key)) or aliases.get(("", product_key))


def find_exact(index, kinds, vendor_key, trace):
    for candidate, kind in kinds.items():
        if candidate not in index.names:
            continue
        ids = index.names[candidate]
        product_id = pick_product(index, ids, vendor_key)
        product = index.products[product_id]
        if kind == "rollup-lang" and product["category"] in ("os", "lang", "device"):
            continue                          # python3-debian is a module, not Debian
        if kind in GUESSED_KINDS and vendor_key and product["vendor"] and not vend_ok(norm_vendor(product["vendor"]), vendor_key):
            continue                          # a guessed spelling must not contradict the vendor (a packager is fine for plain names)
        if trace is not None:
            trace["exact"] = (candidate, sorted(ids))
        confidence = RULE_CONFIDENCE.get(kind, 98 if len(ids) == 1 else 93)
        method = "exact" if kind == "raw" else "exact+" + kind.replace("-lang", "").replace("vstrip", "cleaned")
        return product_id, method, confidence
    return None


def fuzzy_penalty(candidate, key):
    if abs(len(key) - len(candidate)) > 6 and min(len(key), len(candidate)) < 8:
        return 15                             # short-token false friends
    if min(len(key), len(candidate)) <= 6:
        return 8                              # one letter apart is a different product at this length (sonar / sonarr)
    return 0


def find_fuzzy(index, kinds, vendor_key, trace):
    best = None
    for candidate, kind in kinds.items():
        if kind not in ("raw", "cleaned"):
            continue                          # fuzzy never runs on rule/rollup guesses
        hits = process.extract(candidate, index.keys, scorer=fuzz.token_sort_ratio, limit=3, score_cutoff=REVIEW)
        for key, raw_score, _ in hits:
            if digit_tokens(candidate) and digit_tokens(key) and digit_tokens(candidate) != digit_tokens(key):
                continue                      # different model / version number
            product_id = pick_product(index, index.names[key], vendor_key)
            product = index.products[product_id]
            bonus = 0
            if vendor_key and product["vendor"]:
                bonus = 4 if vend_ok(norm_vendor(product["vendor"]), vendor_key) else -6
            score = raw_score - fuzzy_penalty(candidate, key)
            if trace is not None:
                trace["fuzzy"].append((key, product["name"], raw_score, bonus, score + bonus))
            if not best or score + bonus > best[1]:
                best = (product_id, score + bonus)
    if best and best[1] >= REVIEW:            # penalties can push a candidate back under the review line
        return best[0], "fuzzy", round(min(best[1], 97), 1)
    return None


def find_product(index, query, aliases, trace=None):
    """-> (product_id or None, method, confidence). method 'alias-none' = a human said it is not a catalog product."""
    kinds = name_candidates(query)
    vendor_key = norm_vendor(query.get("vendor"))
    if vendor_key in GENERIC_VENDORS:
        vendor_key = ""                       # "Ubuntu Developers" says nothing about the product
    if trace is not None:
        trace.update(candidates=list(kinds), kinds=kinds, vendor_key=vendor_key, alias=None, exact=None, fuzzy=[])

    target = find_alias(query, vendor_key, aliases)       # human decisions win
    if target == "-" or target in index.products:
        if trace is not None:
            trace["alias"] = target
        return (None, "alias-none", 100) if target == "-" else (target, "alias", 100)

    found = find_exact(index, kinds, vendor_key, trace) or find_fuzzy(index, kinds, vendor_key, trace)
    return found or (None, "none", 0)


# ------------------------------------------------------------------ one row
def iso(value):
    return value.isoformat() if value else ""


def vendor_flags(query, product):
    flags = []
    query_vendor = query.get("vendor") or ""
    query_key = norm_vendor(query_vendor)
    product_key = norm_vendor(product["vendor"])
    if not query_vendor:
        flags.append("vendor_missing")
    elif query_key in GENERIC_VENDORS:
        flags.append("vendor_generic")
    elif product_key and not vend_ok(query_key, product_key):
        flags.append(f"vendor_conflict({query_vendor}!={product['vendor']})")
    if query_vendor and query_vendor != product["vendor"] and product_key and vend_ok(query_key, product_key):
        flags.append("vendor_nonstandard_spelling")
    return flags


def match_one(query, catalog_index, trace=None, aliases=None, today=None):
    """Match one CMDB row. Returns the input columns plus MATCH_COLUMNS."""
    today = today or date.today()
    result = dict(query)
    result.update({column: "" for column in MATCH_COLUMNS})
    result.update(match_method="none", match_confidence=0)

    product_id, method, confidence = find_product(catalog_index, query, aliases or {}, trace)
    if not product_id:
        is_hardware = query.get("ci_type") == "hardware"
        result["lifecycle_status"] = "NO_SOURCE" if is_hardware else "UNMATCHED"
        result["review"] = "hardware: model not in any vendor feed" if is_hardware else "no match: in-house/unknown product?"
        if method == "alias-none":            # a human confirmed: not a catalog product
            result.update(lifecycle_status="NO_PRODUCT", review="", match_method="alias-none", match_confidence=100)
        return result

    product = catalog_index.products[product_id]
    result.update(product_id=product_id, matched_product=product["name"], matched_source=product["source"],
                  matched_vendor=product["vendor"], category=product["category"],
                  match_method=method, match_confidence=confidence)
    flags = vendor_flags(query, product)

    release, cycle_how = resolve_cycle(product, query)
    result["cycle_match"] = cycle_how
    if confidence < AUTO:
        result["review"] = "low-confidence product match"
    elif cycle_how in CYCLE_PROBLEMS + ("major-only", "major-ambiguous"):
        result["review"] = f"product ok, version issue: {cycle_how}"

    if release:
        result.update(matched_cycle=release["cycle"], release_date=iso(release["release_date"]),
                      end_sale=iso(release["end_sale"]), support_end=iso(release["end_support"]),
                      eol_date=iso(release["eol"]), extended_end=iso(release["end_extended"]),
                      latest_in_cycle=release["latest"], lifecycle_status=lifecycle_status(release, today),
                      newer_cycle=newer_cycle(product, release, today))
        days = days_to_eol(release, today)
        result["days_to_eol"] = "" if days is None else days
        mismatch = source_mismatch(product, release, catalog_index)
        if mismatch:
            flags.append(mismatch)
    else:
        result["lifecycle_status"] = "VERSION_UNKNOWN"
    result["dq_flags"] = ";".join(flags)
    return result


# ------------------------------------------------------------------ many rows, search
def match_rows(rows, catalog, aliases=None, today=None):
    """Match every row. `catalog` is a list of catalog rows or an index from build_index (reuse it across calls)."""
    index = catalog if isinstance(catalog, CatalogIndex) else build_index(catalog)
    today = today or date.today()
    return [match_one(row, index, aliases=aliases, today=today) for row in rows]


def search_catalog(catalog, text, limit=None):
    """Catalog rows (all cycles) of products whose name, alias or vendor contains the text, best matches first."""
    words = norm(text).split()
    if not words:
        return []
    rows_by_product = defaultdict(list)
    for row in catalog:
        rows_by_product[row["product_id"]].append(row)

    scored = []
    for rows in rows_by_product.values():
        first = rows[0]
        name = norm(first["product"])
        haystack = " ".join([name, norm(first.get("vendor")), norm(first.get("aliases"))])
        if not all(word in haystack for word in words):
            continue
        if name == norm(text):
            rank = 0
        elif name.startswith(words[0]):
            rank = 1
        elif all(word in name for word in words):
            rank = 2
        else:
            rank = 3
        scored.append((rank, name, rows))

    scored.sort(key=lambda item: (item[0], item[1]))
    found = []
    for _, _, rows in scored:
        found.extend(rows)
        if limit is not None and len(found) >= limit:
            break
    return found[:limit]


def verdict(confidence):
    """What the matcher would do with this score for a CMDB row."""
    if confidence >= AUTO:
        return "auto-accept"
    if confidence >= REVIEW:
        return "review"
    return "weak"


def search_scored(index, text, limit=20):
    """Catalog products a free-text query could mean, each with HOW it matched and a 0-100 confidence.

    method: exact    = the normalised query is a product name or alias (same confidence as the matcher)
            contains = every query word appears in a catalog name
            fuzzy    = similar spelling (same scorer and penalties as the matcher's fuzzy step)
    Best hit per product, best score first. Searches names and aliases only, not vendors."""
    query = norm(text)
    if not query:
        return []
    words = query.split()
    best = {}                                 # product_id -> hit

    def keep(product_id, key, method, confidence):
        known = best.get(product_id)
        if known and known["confidence"] >= confidence:
            return
        product = index.products[product_id]
        best[product_id] = {"product_id": product_id, "product": product["name"], "vendor": product["vendor"],
                            "source": product["source"], "category": product["category"], "matched_on": key,
                            "method": method, "confidence": round(confidence, 1), "verdict": verdict(confidence)}

    exact_ids = index.names.get(query, ())
    for product_id in exact_ids:
        keep(product_id, query, "exact", 98 if len(exact_ids) == 1 else 93)

    # Scoring every key that contains the words, then the fuzzy neighbours, lets one product keep its best method.
    for key in index.keys:
        if key == query or not all(word in key for word in words):
            continue
        score = fuzz.token_sort_ratio(query, key) - fuzzy_penalty(query, key)
        for product_id in index.names[key]:
            keep(product_id, key, "contains", min(max(score, 0), 97))

    for key, score, _ in process.extract(query, index.keys, scorer=fuzz.token_sort_ratio, limit=limit * 3, score_cutoff=70):
        if digit_tokens(query) and digit_tokens(key) and digit_tokens(query) != digit_tokens(key):
            continue                          # different model / version number
        for product_id in index.names[key]:
            keep(product_id, key, "fuzzy", min(score - fuzzy_penalty(query, key), 97))

    hits = sorted(best.values(), key=lambda hit: (-hit["confidence"], hit["product"]))
    return hits[:limit]
