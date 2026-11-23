"""Version -> release cycle, and the lifecycle facts (status, days to EOL, newer cycle) derived from a cycle."""
import re
from datetime import date

from cmdb.matching.normalize import norm

VERSION_RE = re.compile(r"\d+(?:\.\d+)*")
CYCLE_PROBLEMS = ("cycle-not-found", "no-version")


def _flat(text):
    return re.sub(r"[\s-]", "", text.lower())


def match_cycle(product, version, package=""):
    """Find the release cycle for a version string -> (release or None, how).
    how: name | exact | prefix | major-ambiguous | no-version | cycle-not-found."""
    version = re.sub(r"^\d+:", "", version or "")        # drop deb/rpm epoch: 1:2.3 -> 2.3
    text = norm(version)
    found = VERSION_RE.search(text)
    if not found and package:
        found = VERSION_RE.search(re.sub(r"^.*?-(?=\d)", "", package))

    cycles = {_flat(r["cycle"]): r for r in product["releases"]}
    for key, release in list(cycles.items()):
        if key.endswith(".x"):
            cycles[key[:-2]] = release                    # '7.x' also answers to '7'

    if _flat(text) in cycles:
        return cycles[_flat(text)], "name"                # whole string is a cycle name ('2012 r2', '8.6')
    if not found:
        return None, "no-version"

    parts = found.group(0).split(".")
    for n in range(len(parts), 0, -1):                    # 3.11.4 -> 3.11 -> 3
        release = cycles.get(".".join(parts[:n]))
        if release:
            return release, "exact" if n == len(parts) else "prefix"

    same_major = [r for key, r in cycles.items() if key.startswith(parts[0] + ".") and r["release_date"]]
    if same_major:
        newest = max(same_major, key=lambda r: r["release_date"])
        return newest, "major-ambiguous"                  # '4' -> newest 4.x, flagged for review
    return None, "cycle-not-found"


def resolve_cycle(product, query):
    """match_cycle plus the fallbacks for rows whose version column is unusable -> (release or None, how)."""
    releases = product["releases"]
    release, how = match_cycle(product, query.get("version"), query.get("fw_or_pkg") or "")

    if how in CYCLE_PROBLEMS:
        # product "Windows Server 2016" with a build number as version: take the version from the name
        embedded = re.search(r"\s(v?\d[\w.]*(?: r2)?)", " " + norm(query.get("product")))
        if embedded:
            from_name, from_name_how = match_cycle(product, embedded.group(1), "")
            if from_name:
                release, how = from_name, "from-name:" + from_name_how

    if how in CYCLE_PROBLEMS and len(releases) == 1:
        release, how = releases[0], "single-cycle"        # hardware model
    if how in CYCLE_PROBLEMS and product["source"] not in ("endoflife.date", "redhat") and len(releases) > 1:
        # vendor feed, service pack unknown (e.g. Exchange 2013): the latest-ending cycle is the best guess
        release, how = max(releases, key=lambda r: r["eol"] or date.min), "product-level"
    return release, how


def lifecycle_status(release, today):
    eol = release["eol"]
    if eol:
        if eol < today:
            extended = release["end_extended"]
            return "EXTENDED_SUPPORT_ONLY" if extended and extended >= today else "EOL"
        if release["end_support"] and release["end_support"] < today:
            return "MAINTENANCE_ONLY"
        return "SUPPORTED"
    if release["is_eol"]:
        return "EOL"
    if release["maintained"]:
        return "SUPPORTED"
    return "UNKNOWN"


def days_to_eol(release, today):
    """Negative once past EOL; None when the cycle has no EOL date."""
    if not release["eol"]:
        return None
    return (release["eol"] - today).days


def newer_cycle(product, release, today):
    """Newest cycle released after this one that is still serviced, or ''."""
    if not release["release_date"]:
        return ""
    newer = []
    for other in product["releases"]:
        if not other["release_date"] or other["release_date"] <= release["release_date"]:
            continue
        still_serviced = other["maintained"] or (other["eol"] and other["eol"] >= today)
        if still_serviced:
            newer.append(other)
    if not newer:
        return ""
    return max(newer, key=lambda r: r["release_date"])["cycle"]


def source_mismatch(product, release, index):
    """Flag when another source lists the same product name with a different EOL date for this cycle -> flag text or ''."""
    for other_id in sorted(index.names.get(norm(product["name"]), [])):
        if other_id == product["id"]:
            continue
        twin = index.products[other_id]
        twin_release = next((r for r in twin["releases"] if r["cycle"] == release["cycle"]), None)
        if twin_release and twin_release["eol"] and release["eol"] and twin_release["eol"] != release["eol"]:
            return f"source_mismatch_eol({twin['source']}={twin_release['eol']})"
        return ""            # the old engine only ever compared against the first twin
    return ""
