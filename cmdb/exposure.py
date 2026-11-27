"""OPTIONAL exposure layer: CISA KEV / EPSS per matched product, and NVD CVEs per row (version-level).
Reads snapshot_dir/cisa_kev.json, epss_scores.csv.gz and nvd.sqlite; any file that is missing is skipped.
Runs after matching and only adds columns, it never changes the match."""
import gzip
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process

from cmdb.matching.index import build_index
from cmdb.matching.normalize import GENERIC_VENDORS, name_candidates, norm, norm_vendor, vend_ok

EXPOSURE_COLUMNS = ["kev_cves", "kev_ransomware", "kev_latest_added", "epss_max",
                    "nvd_cpe", "nvd_match", "nvd_cves_total", "nvd_cves_version", "nvd_max_cvss",
                    "nvd_critical", "nvd_kev", "nvd_latest_cve"]


# ------------------------------------------------------------------ KEV + EPSS
def load_epss(path):
    """{cve: epss probability}; empty when the file is missing."""
    scores = {}
    if not Path(path).exists():
        return scores
    with gzip.open(path, "rt") as handle:
        for line in handle:
            if line.startswith(("#", "cve")):
                continue
            cve, epss, _percentile = line.strip().split(",")
            scores[cve] = float(epss)
    return scores


def kev_products(index, snapshot_dir):
    """{product_id: [KEV entries]}: each KEV vulnerability is linked to catalog products by exact, then very close, name."""
    kev_file = Path(snapshot_dir) / "cisa_kev.json"
    if not kev_file.exists():
        return {}
    epss = load_epss(Path(snapshot_dir) / "epss_scores.csv.gz")
    with open(kev_file, encoding="utf-8") as handle:
        vulnerabilities = json.load(handle)["vulnerabilities"]

    linked = defaultdict(list)
    for vuln in vulnerabilities:
        vendor_key = norm_vendor(vuln["vendorProject"])
        product_ids = list(index.names.get(norm(vuln["product"]), []))
        if not product_ids:
            query = norm(vuln["vendorProject"] + " " + vuln["product"])
            hits = process.extract(query, index.keys, scorer=fuzz.token_set_ratio, limit=1, score_cutoff=95)
            product_ids = [sorted(index.names[key])[0] for key, _score, _pos in hits]
        for product_id in product_ids:
            product_key = norm_vendor(index.products[product_id]["vendor"])
            if product_key and product_key != vendor_key and fuzz.ratio(product_key, vendor_key) < 80:
                continue                  # same product name, different company
            linked[product_id].append({
                "cve": vuln["cveID"],
                "added": vuln["dateAdded"],
                "ransom": vuln["knownRansomwareCampaignUse"] == "Known",
                "epss": epss.get(vuln["cveID"], 0.0),
            })
    return linked


def kev_columns(entries):
    if not entries:
        return {}
    return {
        "kev_cves": len(entries),
        "kev_ransomware": sum(entry["ransom"] for entry in entries),
        "kev_latest_added": max(entry["added"] for entry in entries),
        "epss_max": round(max(entry["epss"] for entry in entries), 3),
    }


# ------------------------------------------------------------------ NVD
def version_key(text):
    """Comparable key for versions like 1.1.1k / 10.0.14393: numbers compare as numbers, letters as text."""
    key = []
    for token in re.findall(r"\d+|[a-z]+", text.lower()):
        key.append((0, int(token)) if token.isdigit() else (1, token))
    while key and key[-1] == (0, 0):
        key.pop()                         # 8.0 == 8
    return tuple(key)


def in_range(key, exact, start_incl, start_excl, end_incl, end_excl):
    """Is the version key inside one NVD affected entry (exact version or start/end bounds)?"""
    if exact == "-":
        return False                      # CPE "not applicable" version
    if exact:
        return key == version_key(exact)
    if not (start_incl or start_excl or end_incl or end_excl):
        return True                       # no bounds: every version is affected
    if start_incl and key < version_key(start_incl):
        return False
    if start_excl and key <= version_key(start_excl):
        return False
    if end_incl and key > version_key(end_incl):
        return False
    if end_excl and key >= version_key(end_excl):
        return False
    return True


class NvdLookup:
    """Thin wrapper over nvd.sqlite. The owner must call close()."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.names = defaultdict(list)    # normalised product name -> [(vendor, product, n_cve)]
        self.cache = {}                   # (vendor, product, version) -> (all CVEs, CVEs for the version)
        query = "select vendor, product, sum(n_cve) from products group by vendor, product"
        for vendor, product, n_cve in self.db.execute(query):
            self.names[norm(product.replace("_", " "))].append((vendor, product, n_cve))

    def close(self):
        self.db.close()

    def identify(self, result, catalog_product):
        """CMDB row -> ((nvd vendor, nvd product), how). Tries the lifecycle product's own CPE name, then the row's names."""
        if catalog_product and catalog_product["id"].startswith("eol:"):
            # most endoflife.date products are named after their CPE product; the catalog does not carry CPE ids
            slug = catalog_product["id"][4:].replace("-", "_")
            for hit in self.names.get(norm(slug.replace("_", " ")), []):
                if hit[:2] == (catalog_product["vendor"], slug):
                    return hit[:2], "from-lifecycle-product"

        vendor_key = norm_vendor(result.get("vendor"))
        if vendor_key in GENERIC_VENDORS:
            vendor_key = ""
        for candidate in name_candidates(result):
            hits = self.names.get(candidate, [])
            if vendor_key:
                hits = [h for h in hits if vend_ok(norm_vendor(h[0]), vendor_key)]
            elif len(candidate) < 6:
                hits = []                 # no vendor: accept only a long name ...
            elif len(hits) > 1:           # ... and, if several vendors use it, the project's own vendor:product
                own = [h for h in hits if norm(h[0].replace("_", " ")) == candidate]
                hits = own if len(own) == 1 else []
            if hits:
                best = max(hits, key=lambda h: h[2])
                return best[:2], "name+vendor" if vendor_key else "name-unique"
        return None, ""

    def cves(self, vendor_product, version):
        """-> (all CVEs of the product, CVEs applicable to `version`, or None when the version is unusable)."""
        key = (*vendor_product, version)
        if key in self.cache:
            return self.cache[key]
        sql = ("select a.cve, a.ver, a.si, a.se, a.ei, a.ee, c.cvss, c.kev, c.published from affects a "
               "join cve c on c.id = a.cve where a.vendor = ? and a.product = ?")
        rows = self.db.execute(sql, vendor_product).fetchall()
        everything = {}
        for row in rows:
            everything[row[0]] = {"cvss": row[6] or 0, "kev": bool(row[7]), "published": row[8]}

        found = re.search(r"\d+(?:\.\d+)*[a-z]?", re.sub(r"^\d+:", "", version or "").lower())
        if re.search(r"_(19|20)\d\d(_r2)?$", vendor_product[1]):
            applicable = list(everything.values())      # the product name already carries the version
        elif not found:
            applicable = None
        else:
            wanted = version_key(found.group(0))
            ids = {row[0] for row in rows if in_range(wanted, *row[1:6])}
            applicable = [everything[cve] for cve in ids]
        self.cache[key] = (list(everything.values()), applicable)
        return self.cache[key]

    def columns(self, result, catalog_product):
        vendor_product, how = self.identify(result, catalog_product)
        if not vendor_product:
            return {}
        everything, applicable = self.cves(vendor_product, result.get("version"))
        columns = {"nvd_cpe": f"{vendor_product[0]}:{vendor_product[1]}", "nvd_match": how,
                   "nvd_cves_total": len(everything)}
        if applicable is not None:
            columns.update(
                nvd_cves_version=len(applicable),
                nvd_max_cvss=max((c["cvss"] for c in applicable), default=0),
                nvd_critical=sum(c["cvss"] >= 9 for c in applicable),
                nvd_kev=sum(c["kev"] for c in applicable),
                nvd_latest_cve=max((c["published"] for c in applicable), default=""),
            )
        return columns


# ------------------------------------------------------------------ public
def add_exposure(results, catalog, snapshot_dir):
    """Add the kev_* / epss_max / nvd_* columns to matched rows (new dicts; the input list is left untouched)."""
    snapshot_dir = Path(snapshot_dir)
    index = build_index(catalog)
    kev = kev_products(index, snapshot_dir)
    nvd_file = snapshot_dir / "nvd.sqlite"
    nvd = NvdLookup(nvd_file) if nvd_file.exists() else None

    try:
        out = []
        for result in results:
            row = dict(result)
            row.update(kev_cves=0, kev_ransomware=0, kev_latest_added="", epss_max="")
            row.update({column: "" for column in EXPOSURE_COLUMNS[4:]})
            # a distro match only dates a package: the distro's own CVEs are not the package's
            product = None if result.get("match_method") == "distro" else index.products.get(result.get("product_id"))
            if product:
                row.update(kev_columns(kev.get(product["id"])))
            if nvd:
                # unmatched long-tail products are still worth identifying in NVD
                row.update(nvd.columns(result, product))
            out.append(row)
    finally:
        if nvd:
            nvd.close()
    return out
