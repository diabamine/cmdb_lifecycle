"""EOSL.ai: source-backed hardware end-of-sale / end-of-service-life dates, down to the part number.

Public JSON API (robots.txt allows crawling, no key):
  https://eosl.ai/api/v1/families.json            index: one entry per product family, with its `detail` url
  https://eosl.ai/api/v1/family/<slug>.json       one family: SKU groups with dates, part numbers, bulletin url

  python -m cmdb.ingest --fetch --only eoslai
  python -m cmdb.ingest --fetch --only eoslai --eosl-vendors Cisco Dell     # a subset, index names as printed

Why it is built this way:
  * One detail request per family, a few per second (WORKERS x PAUSE). About 3,800 families: expect ~15 minutes.
  * Per vendor all-or-nothing: a vendor with any failed family keeps its previous records; the others are merged.
  * One record per SKU group; the part numbers become aliases, so "C9300-48P" can match its family.
  * Dates: the exact day when the bulletin gave one, else the end of the month EOSL.ai publishes.
    `eoslComputed` groups (EOSL.ai derived the date from the vendor's support policy, not a bulletin) get the source
    "eosl.ai:computed", which ends up in the `origin` column so they can be told apart.
  * A group with no dates stays dateless (status unknown); it is only marked obsolete when the vendor itself says
    support has ended.
Attribution: EOSL.ai, derived from vendor end-of-life bulletins. Not authoritative: verify dates with the vendor.
"""
import calendar
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cmdb.ingest.common import SNAPSHOT_UA, CollectorError, get, read_json_list, vendor_rows, write_atomic

BASE = "https://eosl.ai"
SOURCE = "eosl.ai"
SOURCE_COMPUTED = "eosl.ai:computed"
FEED_FILE = "vendor_lifecycle_eoslai.json"
WORKERS = 4
PAUSE_SECONDS = 0.5          # per worker, so ~8 requests per second at most

# EOSL.ai vendor name -> vendor name used elsewhere in the catalog; anything else is lower-cased
VENDOR_NAMES = {"Dell": "dell", "HPE": "hpe", "Cisco": "cisco", "Juniper": "juniper", "IBM": "ibm",
                "Brocade": "brocade", "Palo Alto Networks": "paloalto", "Extreme Networks": "extreme"}


def vendor_name(name: str) -> str:
    return VENDOR_NAMES.get(name) or name.lower()


def group_day(group: dict, key: str) -> str:
    """Exact bulletin date if present, else the end of the published 'YYYY-MM'; '' when there is none."""
    exact = (group.get("exactDates") or {}).get(key) or {}
    if isinstance(exact.get("date"), str) and len(exact["date"]) >= 10:
        return exact["date"][:10]
    month = group.get(key)
    if isinstance(month, str) and len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit() \
            and 1 <= int(month[5:]) <= 12:
        year, number = int(month[:4]), int(month[5:])
        return f"{year}-{number:02d}-{calendar.monthrange(year, number)[1]:02d}"
    return ""


def family_product(family: dict) -> str:
    line, series = (family.get("line") or "").strip(), (family.get("series") or "").strip()
    if line and series and line.lower() not in series.lower():
        return f"{line} {series}"
    return series or line


def to_records(family: dict) -> list[dict]:
    """One EOSL.ai family record -> vendor-feed records, one per SKU group."""
    vendor, product = vendor_name(family.get("vendor") or ""), family_product(family)
    if not vendor or not product:
        return []
    records = []
    for group in family.get("skuGroups") or []:
        parts = [s["partNumber"] for s in group.get("skus") or [] if isinstance(s, dict) and s.get("partNumber")]
        eol, end_sale = group_day(group, "endOfServiceLife"), group_day(group, "endOfSale")
        ended_without_date = bool(group.get("vendorStatesSupportEnded")) and not eol
        record = dict(
            vendor=vendor, product=product, label_alias=list(dict.fromkeys([family.get("series") or "", *parts])),
            cycle=group.get("label") or product, release_date="", end_sale=end_sale,
            end_support_full=group_day(group, "endOfSwMaintenance"), eol=eol,
            end_security_support=group_day(group, "endOfSecuritySupport"), end_extended="",
            status="obsolete" if ended_without_date else "", category="hardware",
            source=SOURCE_COMPUTED if group.get("eoslComputed") and eol else SOURCE,
            url=group.get("sourceUrl") or family.get("url") or BASE, sku_count=len(parts))
        replacement = group.get("replacement")
        if replacement:
            record["replacement"] = replacement
        records.append(record)
    return records


def fetch_json(url: str):
    time.sleep(PAUSE_SECONDS)
    return json.loads(get(url, headers={"User-Agent": SNAPSHOT_UA}, timeout=120))


def fetch(raw_dir: Path, vendors=None) -> None:
    """Pull the family index and every family of the wanted vendors (default: all) into the feed file.

    Raises CollectorError at the end if a vendor failed; vendors that completed stay merged.
    """
    try:
        index = json.loads(get(f"{BASE}/api/v1/families.json", headers={"User-Agent": SNAPSHOT_UA}, timeout=120))
        families = index["families"]
    except (CollectorError, ValueError, KeyError, TypeError) as err:
        raise CollectorError(f"eosl.ai: cannot read the family index: {err}") from err
    known = sorted({f["vendor"] for f in families if f.get("vendor")})
    wanted = list(vendors or known)
    unknown = [v for v in wanted if v not in known]
    if unknown:
        raise CollectorError(f"eosl.ai: unknown vendor(s) {', '.join(unknown)}; known: {', '.join(known)}")

    path = raw_dir / FEED_FILE
    feed, failed = read_json_list(path), []
    for name in wanted:
        todo = [f for f in families if f.get("vendor") == name and f.get("detail")]

        def pull(entry):
            try:
                return to_records(fetch_json(entry["detail"]))
            except (CollectorError, ValueError, TypeError) as err:
                print(f"  ! {entry.get('slug')}: {err}")
                return None

        print(f"eosl.ai {name}: {len(todo)} families", flush=True)
        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(pull, todo))
        if any(r is None for r in results):
            print(f"eosl.ai {name}: FAILED, keeping previous records")
            failed.append(name)
            continue
        records = [r for batch in results for r in batch]
        if not records:
            print(f"eosl.ai {name}: no records (layout changed?), keeping previous records")
            failed.append(name)
            continue
        vendor = vendor_name(name)
        feed = [r for r in feed if r["vendor"] != vendor] + records
        write_atomic(path, json.dumps(feed, indent=0).encode())
        print(f"eosl.ai {name}: {len(records)} records")
    if failed:
        raise CollectorError(f"eosl.ai: failed vendors: {', '.join(failed)}")


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
