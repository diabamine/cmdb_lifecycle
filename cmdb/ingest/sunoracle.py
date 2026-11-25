"""Sun Microsystems / Oracle hardware: end-of-service-life dates from Park Place Technologies.

Oracle's own lists (My Oracle Support Doc IDs 1450710.1 / 1525631.1) need a login, so this reads the public third-party
table at https://www.parkplacetechnologies.com/eosl/sun-oracle/ (two pages, ~200 models: Ultra, Enterprise, Sun Fire,
Netra, SPARC, StorEdge ...). Columns are Model and EOSL Date, dates are "11 / 30 / 2025". The site's robots.txt asks for a
10 second crawl delay, which fetch() keeps. Models are stored under vendor "oracle" (how the catalog already names Sun
hardware) with "Sun Microsystems <model>" as an alias.

  snapshot/vendor_lifecycle_sunoracle.json   vendor-feed records, the input of standardize()
  python -m cmdb.ingest --fetch --only sunoracle

Not authoritative: the dates are Park Place's, not Oracle's. Verify with Oracle before acting.
"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from cmdb.ingest.common import CollectorError, get, read_json_list, rec, tables, vendor_rows, write_atomic

BASE = "https://www.parkplacetechnologies.com/eosl/sun-oracle/"
SOURCE = "parkplace"
VENDOR = "oracle"
FEED_FILE = "vendor_lifecycle_sunoracle.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"
PAUSE_SECONDS = 10                      # Crawl-delay in the site's robots.txt
MAX_PAGES = 5


def parse_date(text: str) -> str:
    """'11 / 30 / 2025' -> '2025-11-30'; anything else -> ''."""
    try:
        return datetime.strptime(re.sub(r"\s*/\s*", "/", (text or "").strip()), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def clean_model(text: str) -> str:
    """Drop the non-ASCII dash and spacing noise some model names carry ('Sun Netra T2000 \\x96 4 Core')."""
    return re.sub(r"\s+", " ", re.sub(r"[\x80-\x9f–—]+", "-", text or "")).strip()


def parse_page(page: str) -> list[dict]:
    """HTML of one page -> vendor-feed records (rows without a parsable date are dropped)."""
    records = []
    for table in tables(page):
        if not table or [h.lower() for h in table[0][:2]] != ["model", "eosl date"]:
            continue
        for row in table[1:]:
            model, eosl = clean_model(row[0]), parse_date(row[1] if len(row) > 1 else "")
            if not model or not eosl:
                continue
            aliases = [f"Sun Microsystems {model}"] if not model.lower().startswith("sun ") else [f"Sun Microsystems {model[4:]}"]
            records.append(rec(VENDOR, model, model, SOURCE, BASE, category="hardware", aliases=aliases, eol=eosl))
    return records


def fetch(raw_dir: Path) -> None:
    """Fetch every page of the table into the feed file. Raises CollectorError (previous records kept) on a failure."""
    records = []
    for number in range(1, MAX_PAGES + 1):
        if number > 1:
            time.sleep(PAUSE_SECONDS)
        url = BASE if number == 1 else f"{BASE}page/{number}/"
        try:
            page = get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, tries=3, timeout=30).decode("utf-8", "ignore")
        except CollectorError:
            if number == 1:
                raise
            break                                        # past the last page
        seen = {(r["product"], r["eol"]) for r in records}
        found = [r for r in parse_page(page) if (r["product"], r["eol"]) not in seen]
        if number > 1 and not found:
            break                                        # an empty page, or the site repeating an earlier one
        records.extend(found)
    if len(records) < 100:
        raise CollectorError(f"sunoracle: only {len(records)} rows, expected ~200 (layout changed?)")
    write_atomic(raw_dir / FEED_FILE, json.dumps(records, indent=0).encode())
    print(f"sunoracle: {len(records)} records")


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
