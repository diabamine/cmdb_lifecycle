"""Citrix: product lifecycle matrix from https://www.citrix.com/support/product-lifecycle/product-matrix.html.

The matrix page loads one HTML fragment per product from /fragments/table-fragments/<name>. A fragment is either
  - a "data-table": one <h3> header per column, each followed by an <ol> with that column's cells, or
  - a "table": rows of <div> cells, the first row being the header (<strong> cells).
Both are read into rows keyed by header text, then mapped by header name (EOL, EOM, EOES ...), not by position.

  snapshot/vendor_lifecycle_citrix.json   vendor-feed records, the input of standardize()
  python -m cmdb.ingest --fetch --only citrix

A fragment that fails or parses to nothing keeps its previous records; the others are still merged.
Citrix dates: EOM = end of maintenance, EOL = end of life (end of support), EOES = end of extended support.
"""
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path

from cmdb.ingest.common import SNAPSHOT_UA, CollectorError, get, read_json_list, rec, vendor_rows, write_atomic

BASE = "https://www.citrix.com"
MATRIX_URL = f"{BASE}/support/product-lifecycle/product-matrix.html"
SOURCE = "citrix"
VENDOR = "citrix"
FEED_FILE = "vendor_lifecycle_citrix.json"
PAUSE_SECONDS = 1

# fragment name (lowercase: the paths are case-sensitive) -> product-line label used when a row has no product name
FRAGMENTS = [
    "citrix-virtual-apps", "a/xenapp", "citrix-app-layering", "citrix-endpoint-management", "citrix-secure-access",
    "citrix-uberagent", "a/federated-authentication-service", "netscaler-adc-formerly-citrix-adc", "a/netscaler-console",
]

ROLES = {
    "product": ("product/component name", "component name", "product"),
    "version": ("version/model", "version", "fas version"),
    "release": ("release date", "ga date"),
    "end_sale": ("eos", "nsc"),
    "end_support": ("eom", "eom date"),
    "eol": ("eol", "eol date"),
    "extended": ("eoes",),
}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = re.compile(r"^(\d{1,2})[- ]([A-Za-z]{3})[a-z]*[- ,]+(\d{2}|\d{4})\b")
NUMERIC = re.compile(r"^(\d{1,2})-(\d{2})-(\d{4})\b")        # Secure Access: 13-01-2027 (day-month-year)


def parse_date(text: str) -> str:
    """'15-Aug-22' / '17-Aug-2029' -> ISO day; 'N/A', 'For eligible customers*', '' -> ''."""
    numeric = NUMERIC.match((text or "").strip())
    if numeric:
        return f"{numeric.group(3)}-{numeric.group(2)}-{int(numeric.group(1)):02d}"
    match = DATE.match((text or "").strip())
    month = MONTHS.get(match.group(2).lower()) if match else None
    if not month:
        return ""
    year = int(match.group(3))
    year += 2000 if year < 100 else 0
    return f"{year}-{month:02d}-{int(match.group(1)):02d}"


class _Fragment(HTMLParser):
    """Collects either layout of a fragment into {"columns": [(header, [cells])]} or {"rows": [[cells]]}."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted        # "data-table" | "table": the page may nest one inside a wrapper of the other
        self.kind = None            # "data-table" | "table"
        self.depth = 0              # div depth inside the table block
        self.in_main = False
        self.columns = []           # data-table: [header, [cells]]
        self.rows = []              # table: [[cell, ...]]
        self.text = []
        self.target = None          # "h3" | "li" | "cell"
        self.list_depth = 0

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "") or ""
        if tag == "main":
            self.in_main = True
        if not self.in_main:
            return
        if tag == "div" and self.kind is None and classes == self.wanted:
            self.kind, self.depth = classes, 1
            return
        if self.kind is None:
            return
        if tag == "div":
            self.depth += 1
            if self.kind == "table":
                if self.depth == 2:
                    self.rows.append([])
                if self.depth == 3:
                    self.target, self.text = "cell", []
        elif self.kind == "data-table":
            if tag == "h3":
                self.target, self.text = "h3", []
            elif tag == "ol":
                self.list_depth += 1
            elif tag == "li" and self.list_depth >= 2:
                self.target, self.text = "li", []

    def handle_endtag(self, tag):
        if not self.in_main or self.kind is None:
            return
        value = re.sub(r"\s+", " ", "".join(self.text)).strip()
        if tag == "h3" and self.target == "h3":
            self.columns.append([value, []])
            self.target = None
        elif tag == "li" and self.target == "li" and self.columns:
            self.columns[-1][1].append(value)
            self.target = None
        elif tag == "ol":
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "div":
            if self.target == "cell" and self.depth == 3 and self.rows:
                self.rows[-1].append(value)
                self.target = None
            self.depth -= 1

    def handle_data(self, data):
        if self.target:
            self.text.append(data)


def read_fragment(page: str) -> list[dict]:
    """HTML of one fragment -> [{header: cell}] (headers lowercased, no trailing '*')."""
    parser = _Fragment("data-table" if 'class="data-table"' in page else "table")
    parser.feed(page)
    if parser.kind == "data-table":
        headers = [re.sub(r"\*+$", "", h.lower()).strip() for h, _ in parser.columns]
        height = max((len(cells) for _, cells in parser.columns), default=0)
        return [{h: (cells[i] if i < len(cells) else "") for h, (_, cells) in zip(headers, parser.columns)}
                for i in range(height)]
    if parser.kind == "table" and parser.rows:
        headers = [re.sub(r"\*+$", "", h.lower()).strip() for h in parser.rows[0]]
        return [{h: cell for h, cell in zip(headers, row) if h} for row in parser.rows[1:] if any(row)]
    return []


def parse_fragment(page: str, name: str) -> list[dict]:
    """HTML of one fragment -> vendor-feed records."""
    url = f"{BASE}/fragments/table-fragments/{name}"
    records = []
    for row in read_fragment(page):
        def pick(role):
            return next((row[h] for h in ROLES[role] if parse_date(row.get(h, "")) or role in ("product", "version")
                         and row.get(h)), "")

        product, version = pick("product").rstrip("* ").strip(), pick("version")
        if not product or not (version or product):
            continue
        language = row.get("language", "")
        cycle = version or product
        launched = re.search(r"\(GA:\s*([^)]*)\)", cycle)           # NetScaler: "14.1(GA: 08-Aug-23)"
        if launched:
            cycle = cycle[:launched.start()].strip()
        dates = dict(release=parse_date(pick("release")) or (parse_date(launched.group(1)) if launched else ""), end_sale=parse_date(pick("end_sale")),
                     end_full=parse_date(pick("end_support")), eol=parse_date(pick("eol")),
                     end_extended=parse_date(pick("extended")))
        if not any(dates.values()):
            continue
        if language and language.upper() != "EN":
            continue                    # NetScaler lists one row per language build with the same dates
        records.append(rec(VENDOR, product, cycle, SOURCE, MATRIX_URL, category="software",
                           aliases=[f"Citrix {product}"] if not product.lower().startswith("citrix") else [],
                           release=dates["release"], end_sale=dates["end_sale"], end_full=dates["end_full"],
                           eol=dates["eol"], end_extended=dates["end_extended"]))
        records[-1]["fragment"] = url
    return records


def fetch(raw_dir: Path) -> None:
    """Fetch every fragment into the feed file. Raises CollectorError if any fragment failed."""
    path = raw_dir / FEED_FILE
    records, failed = [], []
    for n, name in enumerate(FRAGMENTS):
        if n:
            time.sleep(PAUSE_SECONDS)
        try:
            page = get(f"{BASE}/fragments/table-fragments/{name}", headers={"User-Agent": SNAPSHOT_UA}).decode("utf-8", "ignore")
            found = parse_fragment(page, name)
            if not found:
                raise CollectorError("fragment parsed to no records (layout changed?)")
        except CollectorError as err:
            print(f"citrix {name}: FAILED, keeping previous records: {err}")
            failed.append(name)
            continue
        records.extend(r for r in found)
        print(f"citrix {name}: {len(found)} records")
    old = read_json_list(path)
    done = {r["fragment"] for r in records}
    kept = [r for r in old if r.get("fragment") not in done]
    if records:
        write_atomic(path, json.dumps(kept + records, indent=0).encode())
    if failed:
        raise CollectorError(f"citrix: failed fragments: {', '.join(failed)}")


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
