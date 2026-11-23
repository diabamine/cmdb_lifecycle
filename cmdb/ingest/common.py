"""Helpers shared by the ingest modules: HTTP with retries, date parsing, the vendor-feed file, manifest."""
import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cmdb.schema import STANDARD_COLUMNS

# Some vendor sites block bare clients, so scrapers send a browser-like agent.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SNAPSHOT_UA = "cmdb-lifecycle-snapshot/0.2"

# All scraped vendors (microsoft, juniper, ...) share this one raw file, as before the refactor.
VENDOR_FEED = "vendor_lifecycle.json"


class CollectorError(RuntimeError):
    """A collector could not do its job (download failed, page layout changed). The caller keeps the old raw data."""


def get(url, headers=None, data=None, tries=3, timeout=60):
    """GET (or POST when data is given) returning bytes. Retries, then raises CollectorError."""
    for attempt in range(tries):
        request = urllib.request.Request(url, headers=headers or {"User-Agent": BROWSER_UA}, data=data)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            # Rate limiting / overload deserves a longer pause than a transient glitch.
            wait = 10 * (attempt + 1) if err.code in (403, 429, 503) else 3
            problem = f"HTTP {err.code}"
        except Exception as err:
            wait = 5
            problem = str(err)
        if attempt == tries - 1:
            raise CollectorError(f"{url[:90]}: {problem}")
        print(f"  ! {problem} {url[:80]}, retry in {wait}s", file=sys.stderr)
        time.sleep(wait)


def get_text(url, tries=3):
    """Like get() but returns "" on failure: for scrapers where one missing page must not abort the whole run."""
    try:
        return get(url, tries=tries).decode("utf-8", "ignore")
    except CollectorError as err:
        print(f"  ! {err}", file=sys.stderr)
        return ""


ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def day(value):
    """'2026-01-31' or '2026-01-31T00:00:00Z' -> '2026-01-31'; anything else -> ''."""
    if isinstance(value, str) and ISO_DAY.match(value):
        return value[:10]
    return ""


def iso(text):
    """'10/30/2026', '1/12/2022 6:59:59 AM', 'April 2, 2020', '2026-01-31' -> '2026-01-31'; anything else -> ''."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def month_end(text):
    """'January 2032' -> '2032-01-31'. A vendor saying 'available through January 2032' means the end of that month."""
    try:
        parsed = datetime.strptime((text or "").strip(), "%B %Y")
    except ValueError:
        return ""
    last_day = calendar.monthrange(parsed.year, parsed.month)[1]
    return parsed.replace(day=last_day).strftime("%Y-%m-%d")


def clean(text):
    """HTML fragment -> plain text with collapsed whitespace."""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def tables(page):
    """HTML -> list of tables; each table is a list of rows; each row a list of cell texts."""
    result = []
    for table in re.findall(r"<table.*?</table>", page, flags=re.S):
        rows = []
        for row in re.findall(r"<tr.*?</tr>", table, flags=re.S):
            cells = re.findall(r"<t[dh][^>]*>.*?</t[dh]>", row, flags=re.S)
            rows.append([clean(cell) for cell in cells])
        result.append(rows)
    return result


# ---------------------------------------------------------------- raw files

def write_atomic(path: Path, content: bytes):
    """Write via a temp file so a crash never leaves a half-written raw file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def save_download(raw_dir: Path, name: str, url: str, tries=5):
    """Download url into raw_dir/name and record it in manifest.json. Nothing is written when the download fails."""
    content = get(url, headers={"User-Agent": SNAPSHOT_UA}, tries=tries, timeout=120)
    write_atomic(raw_dir / name, content)
    update_manifest(raw_dir, name, url, content)


def update_manifest(raw_dir: Path, name: str, url: str, content: bytes):
    path = raw_dir / "manifest.json"
    manifest = json.loads(path.read_text()) if path.exists() else {"files": {}}
    manifest["created_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["files"][name] = {"url": url, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    write_atomic(path, json.dumps(manifest, indent=1).encode())


# ---------------------------------------------------------------- vendor feed (shared raw file of the scraped vendors)

def rec(vendor, product, cycle, source, url, category="", aliases=(), release="", end_sale="", end_full="", eol="",
        end_security="", end_extended="", status=""):
    """One raw vendor-feed record. status = 'vintage' | 'obsolete' for vendors that publish a status but no date (Apple)."""
    return dict(vendor=vendor, product=product, label_alias=list(aliases), cycle=cycle, release_date=release,
                end_sale=end_sale, end_support_full=end_full, eol=eol, end_security_support=end_security,
                end_extended=end_extended, status=status, category=category, source=source, url=url)


def read_json_list(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text())


def is_manual(record: dict) -> bool:
    return record["source"].startswith("manual:")


def feed_records(raw_dir: Path, vendor: str) -> list:
    """The scraped records of one vendor. manual/*.csv rows with the same vendor belong to the manual module."""
    return [r for r in read_json_list(raw_dir / VENDOR_FEED) if r["vendor"] == vendor and not is_manual(r)]


def replace_vendor_records(raw_dir: Path, vendor: str, records: list, owns=None):
    """Swap one vendor's records inside the shared feed, keeping everybody else's.

    owns(record) says which existing records this collector replaces (default: same vendor).
    """
    path = raw_dir / VENDOR_FEED
    if owns is None:
        owns = lambda r: r["vendor"] == vendor and not is_manual(r)
    kept = [r for r in read_json_list(path) if not owns(r)]
    write_atomic(path, json.dumps(kept + records, indent=0).encode())
    print(f"{vendor}: {len(records)} records -> {path}")


def vendor_rows(records: list, source=None):
    """Raw vendor-feed records -> STANDARD rows. source=None keeps each record's own source (manual:<file>)."""
    groups = {}
    for r in records:
        groups.setdefault((r["vendor"], r["product"]), []).append(r)
    rows = []
    for (vendor, product), recs in groups.items():
        aliases = [product, *(a for r in recs for a in r.get("label_alias", []))]
        aliases.append(f"{vendor} {product}")
        aliases.append(product.removeprefix("Microsoft "))        # "Office 2016" == "Microsoft Office 2016"
        if vendor == "microsoft":
            aliases.append("Microsoft " + product.removeprefix("Microsoft "))
        alias_text = join_aliases(aliases, exclude=product)
        for r in recs:
            rows.append(standard_row(
                product_id=f"v:{vendor}:{product}", source=source or r["source"], vendor=vendor, product=product,
                aliases=alias_text, category=r.get("category", ""), version=r["cycle"],
                release_date=day(r.get("release_date")), end_sale=day(r.get("end_sale")),
                end_support=day(r.get("end_support_full")), eol=day(r.get("eol")),
                end_extended=day(r.get("end_extended")), is_eol="true" if r.get("status") == "obsolete" else "",
                url=r.get("url", ""), origin=r["source"] if source else ""))
    return make_ids_unique(rows)


def make_ids_unique(rows: list[dict]) -> list[dict]:
    """A source can list the same product+cycle twice (different pages or notices): keep both rows, suffix later ids."""
    seen = {}
    for row in rows:
        count = seen.get(row["id"], 0) + 1
        seen[row["id"]] = count
        if count > 1:
            row["id"] = f"{row['id']}#{count}"
    return rows


def join_aliases(names, exclude=""):
    """'|'-separated aliases without blanks, duplicates (case-insensitive) or the product's own name."""
    seen = {exclude.lower()}
    kept = []
    for name in names:
        name = (name or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            kept.append(name)
    return "|".join(kept)


def standard_row(**fields):
    """Build a STANDARD row from keyword fields; missing columns become ''. Raises on unknown names (typo guard)."""
    row = {column: "" for column in STANDARD_COLUMNS}
    for name, value in fields.items():
        if name not in row:
            raise KeyError(name)
        row[name] = value
    row["id"] = f"{row['product_id']}@{row['version']}"
    return row
