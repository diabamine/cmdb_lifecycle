"""endoflife.date: one bulk JSON with every product and its release cycles."""
import json
from pathlib import Path

from cmdb.ingest.common import day, join_aliases, save_download, standard_row

URL = "https://endoflife.date/api/v1/products/full"
RAW_FILE = "endoflife_full.json"


def fetch(raw_dir: Path) -> None:
    save_download(raw_dir, RAW_FILE, URL)


def standardize(raw_dir: Path) -> list[dict]:
    data = json.loads((raw_dir / RAW_FILE).read_text())
    rows = []
    for product in data["result"]:
        rows.extend(product_rows(product))
    return rows


def product_rows(product: dict) -> list[dict]:
    name = product["name"]
    spaced_name = name.replace("-", " ")
    vendor = ""
    names = [spaced_name, *product.get("aliases", [])]
    for ident in product.get("identifiers", []):
        if ident["type"] == "purl":
            names.append(ident["id"].split("/")[-1].replace("-", " "))
        if ident["type"] == "cpe" and ident["id"].startswith("cpe:2.3"):
            parts = ident["id"].split(":")
            vendor = parts[3]                    # the last cpe wins, as in the old loader
            names.append(parts[4].replace("_", " "))
    if vendor:
        names.append(f"{vendor} {spaced_name}")   # "oracle java" style spelling

    label = product.get("label") or name
    aliases = join_aliases([name, *names], exclude=label)
    rows = []
    for release in product["releases"]:
        eol = day(release.get("eolFrom"))
        rows.append(standard_row(
            product_id=f"eol:{name}", source="endoflife.date", vendor=vendor, product=label, aliases=aliases,
            category=product.get("category", ""), version=str(release["name"]),
            release_date=day(release.get("releaseDate")),
            end_support=day(release.get("eoasFrom")),     # end of active support
            eol=eol,
            end_extended=day(release.get("eoesFrom")),
            # isEol without a date: the source says "dead" but not since when
            is_eol="true" if release.get("isEol") and not eol else "",
            latest=(release.get("latest") or {}).get("name") or "",
            url=(product.get("links") or {}).get("html", "")))
    return rows
