"""Red Hat product life cycle API: phases per version (GA, full support, maintenance, extended)."""
import json
from pathlib import Path

from cmdb.ingest.common import day, join_aliases, make_ids_unique, save_download, standard_row

URL = "https://access.redhat.com/product-life-cycles/api/v1/products"
RAW_FILE = "redhat_lifecycle.json"


def fetch(raw_dir: Path) -> None:
    save_download(raw_dir, RAW_FILE, URL)


def standardize(raw_dir: Path) -> list[dict]:
    data = json.loads((raw_dir / RAW_FILE).read_text())
    rows = []
    for product in data["data"]:
        rows.extend(product_rows(product))
    return make_ids_unique(rows)


def product_rows(product: dict) -> list[dict]:
    name = product["name"]
    # The API names phases freely; "all_phases" tells which are normal vs extended support.
    phase_type = {phase["name"]: phase["ptype"] for phase in product["all_phases"]}
    aliases = join_aliases(product.get("former_names", []), exclude=name)
    rows = []
    for version in product["versions"]:
        phases = version.get("phases", [])
        ga = next((p for p in phases if "general availability" in p["name"].lower()), None)
        normal = [p for p in phases if phase_type.get(p["name"]) == "normal" and p is not ga]
        extended = [p for p in phases if phase_type.get(p["name"]) == "extended"]
        extended_ends = [day(p["end_date"]) for p in extended]
        rows.append(standard_row(
            product_id=f"rh:{name}", source="redhat", vendor="redhat", product=name, aliases=aliases,
            category="redhat-product", version=str(version["name"]),
            release_date=day(ga["end_date"]) if ga else "",
            # with one normal phase it is the whole support window: only a second phase marks "end of full support"
            end_support=day(normal[0]["end_date"]) if len(normal) > 1 else "",
            eol=day(normal[-1]["end_date"]) if normal else "",
            end_extended=max(extended_ends, default=""),
            latest=version.get("final_minor_release") or "",
            url=product.get("link") or ""))
    return rows
