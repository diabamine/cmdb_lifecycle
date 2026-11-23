"""Turn master-catalog rows (one row per release cycle) into products with releases, plus a normalised name index."""
from collections import defaultdict
from datetime import date

from cmdb.matching.normalize import norm, norm_vendor


def parse_date(text):
    """Catalog dates are ISO 'YYYY-MM-DD' or ''. Anything unparsable counts as unknown."""
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


class CatalogIndex:
    """products: id -> product dict (releases are dicts with parsed dates); names: normalised name -> {product ids}."""

    def __init__(self):
        self.products = {}
        self.names = defaultdict(set)
        self.keys = []          # all normalised names, the corpus for fuzzy search


def make_release(row):
    eol = parse_date(row.get("eol"))
    is_eol = row.get("is_eol") == "true"
    return {
        "cycle": row["version"],
        "release_date": parse_date(row.get("release_date")),
        "end_sale": parse_date(row.get("end_sale")),
        "end_support": parse_date(row.get("end_support")),
        "eol": eol,
        "end_extended": parse_date(row.get("end_extended")),
        "is_eol": is_eol,
        "latest": row.get("latest") or "",
        # The catalog has no "maintained" flag. endoflife.date marks a cycle without an EOL date as maintained;
        # vendor feeds with no date stay unknown.
        "maintained": row.get("source") == "endoflife.date" and not eol and not is_eol,
    }


def product_names(product):
    """Every spelling under which a product can be found: name, aliases, '<vendor> <name>', Microsoft prefix variants."""
    name = product["name"]
    names = [name, *product["aliases"]]
    if product["vendor"]:
        names.append(f"{product['vendor']} {name}")
    if norm_vendor(product["vendor"]) == "microsoft":
        bare = name.removeprefix("Microsoft ")        # "Office 2016" == "Microsoft Office 2016"
        names += [bare, "Microsoft " + bare]
    return names


def build_index(catalog):
    """Group catalog rows by product_id into products and index their names. Call once, reuse for every row."""
    rows_by_product = defaultdict(list)
    for row in catalog:
        rows_by_product[row["product_id"]].append(row)

    index = CatalogIndex()
    for product_id, rows in rows_by_product.items():
        first = rows[0]
        aliases = []
        for row in rows:
            aliases += [a for a in (row.get("aliases") or "").split("|") if a]
        product = {
            "id": product_id,
            "source": first["source"],
            "name": first["product"],
            "category": first.get("category") or "",
            "vendor": first.get("vendor") or "",
            "aliases": list(dict.fromkeys(aliases)),
            "url": first.get("url") or "",
            "releases": [make_release(row) for row in rows],
        }
        index.products[product_id] = product
        for name in product_names(product):
            key = norm(name)
            if key:
                index.names[key].add(product_id)
    index.keys = list(index.names)
    return index
