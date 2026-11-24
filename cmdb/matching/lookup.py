"""Interactive lookup: vendor + product + version typed by a person -> every catalog cycle that fits the description.

Unlike match_one (one best answer for a CMDB row), a lookup keeps the ambiguity visible: several candidate products,
and several release cycles when the version is only partial ("3" fits 3.8, 3.11, 3.12 ...).
Takes and returns plain dicts."""
import re
from datetime import date

from cmdb.matching.index import CatalogIndex
from cmdb.matching.lifecycle import VERSION_RE, days_to_eol, lifecycle_status
from cmdb.matching.matcher import REVIEW, search_scored
from cmdb.matching.normalize import norm, norm_vendor, vend_ok

# "sql server 2019", "python 3.11", "windows server 2012 r2": a number after at least one word of name
EMBEDDED_VERSION = re.compile(r"^(?P<name>.*?[a-z].*?)\s+v?(?P<version>\d+(?:\.\d+)*(?: ?r2)?)\b")


def split_version(product_text):
    """'Python 3.11' -> ('python', '3.11'); a name without a version -> (name, '')."""
    text = norm(product_text)
    found = EMBEDDED_VERSION.match(text)
    if not found:
        return text, ""
    return found.group("name"), found.group("version")


def cycles_for_version(releases, version):
    """Releases that fit a version description -> [(release, how)], newest first.

    how: exact          cycle equals the version (3.11 for 3.11)
         cycle-prefix   the cycle is a shorter form of the version (3.11 for 3.11.4)
         version-prefix the version is a shorter form of the cycle: a partial version fits several cycles (3 -> 3.8, 3.11)
         any            no version given, every cycle is listed"""
    newest_first = sorted(releases, key=lambda r: r["release_date"] or date.min, reverse=True)
    flat = re.sub(r"[\s-]", "", version.lower())
    if not flat:
        return [(r, "any") for r in newest_first]
    # a cycle spelled like the whole text ('2012 r2', '8.6 lts') wins: it must not also fit the plain '2012' cycle
    same_name = [(r, "exact") for r in newest_first if re.sub(r"[\s-]", "", r["cycle"].lower()) == flat]
    if same_name:
        return same_name
    wanted = VERSION_RE.search(norm(version))
    if not wanted:
        return []

    parts = wanted.group(0)
    found = []
    for release in newest_first:
        cycle = release["cycle"].lower().removesuffix(".x")
        digits = VERSION_RE.search(cycle)
        cycle_number = digits.group(0) if digits else cycle
        if cycle_number == parts:
            found.append((release, "exact"))
        elif parts.startswith(cycle_number + "."):
            found.append((release, "cycle-prefix"))
        elif cycle_number.startswith(parts + "."):
            found.append((release, "version-prefix"))
    # a more specific cycle hides its parent: 3.11.4 fits cycle 3.11, and also a catch-all cycle 3
    if any(how == "exact" for _, how in found):
        found = [(r, how) for r, how in found if how != "cycle-prefix"]
    elif any(how == "cycle-prefix" for _, how in found):
        longest = max(len(r["cycle"]) for r, how in found if how == "cycle-prefix")
        found = [(r, how) for r, how in found if how != "cycle-prefix" or len(r["cycle"]) == longest]
    return found


def candidate_products(index, vendor, name, name_without_version):
    """Products the typed name could mean, vendor-aware, best first. Tries the full name and the name minus its version."""
    hits = {}
    for query in {name, name_without_version}:
        for hit in search_scored(index, query, limit=10):
            if hit["product_id"] not in hits or hits[hit["product_id"]]["confidence"] < hit["confidence"]:
                hits[hit["product_id"]] = hit

    vendor_key = norm_vendor(vendor)
    ranked = []
    for hit in hits.values():
        product_vendor = norm_vendor(hit["vendor"])
        if not vendor_key or not product_vendor:
            hit["vendor_match"] = ""
        elif vend_ok(vendor_key, product_vendor):
            hit["vendor_match"] = "yes"
            hit["confidence"] = min(hit["confidence"] + 4, 100)
        else:
            hit["vendor_match"] = "conflict"
            hit["confidence"] -= 6
        if hit["confidence"] >= REVIEW:
            ranked.append(hit)
    return sorted(ranked, key=lambda h: (-h["confidence"], h["product"]))


def lookup(index: CatalogIndex, vendor="", product="", version="", today=None, max_products=5):
    """-> {"detected_version": str, "version_used": str, "rows": [one dict per (product, cycle) that fits]}.

    The version box wins; when it is empty, a version found inside the product name is used."""
    today = today or date.today()
    name_without_version, detected = split_version(product)
    version_used = version.strip() or detected
    rows = []
    unfit = []
    for hit in candidate_products(index, vendor, norm(product), name_without_version)[:max_products]:
        releases = index.products[hit["product_id"]]["releases"]
        fitting = cycles_for_version(releases, version_used)
        if not fitting:
            unfit.append({**base_row(hit), "cycle": "", "version_fit": "version not in catalog"})
            continue
        for release, how in fitting:
            row = {**base_row(hit), "cycle": release["cycle"], "version_fit": how,
                   "release_date": release["release_date"], "support_end": release["end_support"],
                   "eol_date": release["eol"], "extended_end": release["end_extended"],
                   "latest_in_cycle": release["latest"], "lifecycle_status": lifecycle_status(release, today)}
            row["days_to_eol"] = days_to_eol(release, today)
            rows.append(row)
    # Products that do not know the version are noise next to ones that do (Windows Server 2003 for "2012 R2");
    # they are kept only when nothing fits, so the person sees the product exists but not that version.
    if not rows:
        rows = unfit
    return {"detected_version": detected, "version_used": version_used, "rows": rows}


def base_row(hit):
    return {"product_id": hit["product_id"], "product": hit["product"], "vendor": hit["vendor"], "source": hit["source"],
            "product_match": hit["method"], "product_confidence": hit["confidence"], "vendor_match": hit["vendor_match"]}


def filter_results(results, vendor="", product="", version=""):
    """Rows of a match run that fit the typed vendor / product / version (all optional, all must fit).

    Product and vendor are 'contains' tests on both the input text and the matched catalog name. The version also
    counts when it sits in the input product name ('SQL Server 2019 Standard') or in the matched cycle."""
    name, detected = split_version(product)
    wanted = norm(version) or detected
    vendor_key = norm_vendor(vendor)        # "Red Hat" and "redhat" are the same vendor
    kept = []
    for row in results:
        row_product = norm(row.get("product"))
        if vendor_key and vendor_key not in norm_vendor(row.get("vendor")) and vendor_key not in norm_vendor(row.get("matched_vendor")):
            continue
        if name and name not in row_product and name not in norm(row.get("matched_product")):
            continue
        if wanted and not version_fits_row(row, row_product, wanted):
            continue
        kept.append(row)
    return kept


def version_fits_row(row, row_product, wanted):
    row_version = norm(row.get("version"))
    cycle = norm(row.get("matched_cycle"))
    if row_version.startswith(wanted) or (row_version and wanted.startswith(row_version)):
        return True
    if cycle and (cycle == wanted or cycle.startswith(wanted + ".") or wanted.startswith(cycle + ".")):
        return True
    return f" {wanted} " in f" {row_product} "       # version written inside the product name
