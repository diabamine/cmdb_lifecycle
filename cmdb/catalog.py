"""Step 2: concatenate data/standard/*.csv into one cleaned master catalog (same columns as STANDARD_COLUMNS).

No matching logic lives here: this only fixes data quality so that matching can trust the catalog.
Run:  python -m cmdb.catalog [--standard data/standard] [--out data/master_catalog.csv]
"""
import argparse
import csv
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from cmdb.schema import STANDARD_COLUMNS

DATE_COLUMNS = ["release_date", "end_sale", "end_support", "eol", "end_extended"]
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y"]

# Paid Extended Security Update rows are not release cycles (same rule as the old engine).
ESU_PATTERN = re.compile(r"extended security|\besu\b", re.I)

# Vendor canonicalisation: the same rules the old engine's norm_vendor used, so catalog vendors
# compare equal to normalised CMDB vendors. Lowercase ascii, punctuation removed, company suffixes
# and filler words dropped, a few abbreviations expanded, spaces removed ("Red Hat, Inc." -> "redhat").
CORP_WORDS = {
    "inc", "corp", "corporation", "ltd", "llc", "bv", "nv", "foundation", "the", "software", "systems",
    "company", "co", "gmbh", "sa", "by", "group", "global", "development", "project", "organization",
    "international", "networks", "technologies", "technology", "incorporated", "limited", "plc", "ag",
}
VENDOR_SYNONYMS = {
    "msft": "microsoft", "rh": "redhat", "orcl": "oracle", "pgdg": "postgresql", "asf": "apache",
    "hpe": "hewlett packard enterprise", "hp": "hewlett packard",
}


def collapse_spaces(text):
    return re.sub(r"\s+", " ", text or "").strip()


def clean_date(value):
    """Return an ISO date string, or "" when the value is empty or not a real date."""
    value = collapse_spaces(value)
    if not value:
        return ""
    # Timestamps such as 2024-05-01T00:00:00Z: only the date part matters
    for candidate in (value, value[:10]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def clean_vendor(vendor):
    vendor = collapse_spaces(vendor)
    ascii_text = unicodedata.normalize("NFKD", vendor).encode("ascii", "ignore").decode().lower()
    # Any punctuation (including ".") is a separator
    words = re.sub(r"[^a-z0-9]+", " ", ascii_text).split()
    words = [VENDOR_SYNONYMS.get(w, w) for w in words if w not in CORP_WORDS]
    key = " ".join(words).replace("red hat", "redhat").replace(" ", "")
    # A vendor made only of filler words (e.g. "The Software Foundation") keeps its plain lowercase form
    return key or vendor.lower()


def clean_aliases(aliases, product):
    names = {collapse_spaces(a) for a in (aliases or "").split("|")}
    names.discard("")
    names.discard(product)
    return "|".join(sorted(names))


def clean_row(raw):
    row = {col: collapse_spaces(raw.get(col)) for col in STANDARD_COLUMNS}
    row["vendor"] = clean_vendor(row["vendor"])
    row["aliases"] = clean_aliases(raw.get("aliases"), row["product"])
    for col in DATE_COLUMNS:
        row[col] = clean_date(row[col])
    row["is_eol"] = "true" if row["is_eol"].lower() == "true" else ""
    if not row["product_id"]:
        row["product_id"] = f"{row['source']}:{row['vendor']}:{row['product']}"
    return row


def is_esu(row):
    return bool(ESU_PATTERN.search(row["version"]))


def fold_esu_rows(rows):
    """Move ESU end dates into end_extended of the product's regular cycles and drop the ESU rows.

    A product that only has ESU rows keeps them, otherwise it would vanish from the catalog.
    """
    esu_dates = {}  # product_id -> latest ESU end date
    has_regular = set()
    for row in rows:
        if is_esu(row):
            date = row["eol"] or row["end_extended"] or row["end_support"]
            esu_dates[row["product_id"]] = max(esu_dates.get(row["product_id"], ""), date)
        else:
            has_regular.add(row["product_id"])

    kept = []
    for row in rows:
        if is_esu(row) and row["product_id"] in has_regular:
            continue
        if not is_esu(row) and not row["end_extended"]:
            # The vendor's own end_extended wins over the folded ESU date
            row["end_extended"] = esu_dates.get(row["product_id"], "")
        kept.append(row)
    return kept


def filled_fields(row):
    return sum(1 for value in row.values() if value)


def dedupe(rows):
    """Keep one row per (product_id, version): the one with the most filled fields (first wins ties)."""
    best = {}
    for row in rows:
        key = (row["product_id"], row["version"])
        if key not in best or filled_fields(row) > filled_fields(best[key]):
            best[key] = row
    return list(best.values())


def assign_ids(rows):
    """id is product_id@version; a numeric suffix keeps it unique if two different keys collide."""
    seen = set()
    for row in rows:
        row_id = f"{row['product_id']}@{row['version']}"
        unique_id, n = row_id, 2
        while unique_id in seen:
            unique_id = f"{row_id}#{n}"
            n += 1
        seen.add(unique_id)
        row["id"] = unique_id


def read_standard_files(standard_dir):
    rows = []
    for path in sorted(Path(standard_dir).glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def build_catalog(standard_dir):
    rows = [clean_row(raw) for raw in read_standard_files(standard_dir)]
    rows = [row for row in rows if row["product"]]
    rows = fold_esu_rows(rows)
    rows = dedupe(rows)
    # Sorted output keeps git diffs small between rebuilds
    rows.sort(key=lambda r: (r["source"], r["product_id"], r["version"]))
    assign_ids(rows)
    return rows


def write_catalog(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STANDARD_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_catalog(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser(description="Build the master catalog from data/standard/*.csv")
    parser.add_argument("--standard", default="data/standard")
    parser.add_argument("--out", default="data/master_catalog.csv")
    args = parser.parse_args()
    rows = build_catalog(Path(args.standard))
    write_catalog(rows, args.out)
    products = len({r["product_id"] for r in rows})
    print(f"{len(rows)} cycles, {products} products -> {args.out}")


if __name__ == "__main__":
    main()
