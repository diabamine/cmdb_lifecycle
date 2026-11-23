"""Microsoft: scrape learn.microsoft.com/lifecycle/products/<slug> pages (URLs from the official sitemap), ~800 pages."""
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cmdb.ingest.common import (CollectorError, clean, get_text, iso, feed_records, rec, replace_vendor_records,
                                tables, vendor_rows)

VENDOR = "microsoft"
BASE = "https://learn.microsoft.com"
SOURCE = "learn.microsoft.com/lifecycle"


def fetch(raw_dir: Path) -> None:
    sitemap = get_text(f"{BASE}/_sitemaps/lifecycle_en-us_1.xml")
    urls = sorted({u for u in re.findall(r"<loc>([^<]+)</loc>", sitemap) if "/lifecycle/products/" in u})
    if not urls:
        raise CollectorError("microsoft: sitemap empty or unreachable")
    print(f"microsoft: {len(urls)} product pages")
    with ThreadPoolExecutor(6) as pool:
        records = [r for page_records in pool.map(scrape_page, urls) for r in page_records]
    if not records:
        raise CollectorError("microsoft: no lifecycle tables found (page layout changed?)")
    replace_vendor_records(raw_dir, VENDOR, records)


def scrape_page(url: str) -> list[dict]:
    page = get_text(url)
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S)
    name = clean(heading.group(1)) if heading else url.rsplit("/", 1)[-1]
    return parse_tables(tables(page), name, url)


def parse_tables(page_tables: list, name: str, url: str) -> list[dict]:
    out = []
    for table in page_tables:
        if not table or not any("start date" in cell.lower() for cell in table[0]):
            continue
        header = [cell.lower() for cell in table[0]]

        def column(*keys):
            for i, cell in enumerate(header):
                if any(key in cell for key in keys):
                    return i
            return None

        i_start, i_main, i_ext = column("start"), column("mainstream"), column("extended")
        i_retire = column("retire", "end date")
        for row in table[1:]:
            if len(row) < 2:
                continue

            def date_at(i):
                return iso(row[i]) if i is not None and i < len(row) else ""

            end_full = date_at(i_main)
            # Products with a single end date use the "retirement" column; otherwise EOL falls back to mainstream end.
            retire = date_at(i_retire) if i_retire not in (i_main, i_ext) else ""
            eol = date_at(i_ext) or retire or end_full
            out.append(rec(VENDOR, name, row[0], SOURCE, url, "os-or-app", release=date_at(i_start),
                           end_full=end_full, eol=eol))
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
