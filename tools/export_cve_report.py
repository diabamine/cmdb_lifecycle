#!/usr/bin/env python3
"""Export the CVE layer to CSV so its size and completeness can be checked. Read-only, offline, stdlib only.

  python3 -I export_cve_report.py [--snapshot snapshot] [--out reports/cve]

Compares what the raw NVD files contain (snapshot/nvd/CVE-<year>.json.xz, from fetch_snapshot.py --nvd) with what
build_nvd_index.py kept in snapshot/nvd.sqlite, and joins CISA KEV and EPSS. Files written:
  summary.csv               headline dimensions, null counts and cross-checks (metric, value)
  nvd_source_files.csv      one row per raw yearly file: items read vs CVEs in the index, items with/without product (CPE) data
  cve_by_year.csv           per publication year: CVEs, with/without product mapping, CVSS present, critical, KEV
  cve_status_by_year.csv    per publication year x NVD analysis status (Analyzed, Awaiting Analysis, ...) with mapped counts
  cve.csv                   one row per CVE (all of them): index fields + NVD status + product mapping + KEV + EPSS
  kev.csv                   one row per CISA KEV entry, and whether NVD has it / maps it to a product
  products.csv              distinct (vendor, product, part) names in the index with their CVE count
  affects.csv               one row per vulnerable CPE match / version range (~2M rows: too big for Excel)
  affects_sample.csv        5,000 evenly spaced rows of affects.csv, for eyeballing
"""
import argparse, csv, gzip, json, lzma, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--snapshot", default="snapshot"); ap.add_argument("--out", default="reports/cve")
a = ap.parse_args(); snap, out = Path(a.snapshot), Path(a.out); out.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(f"file:{snap / 'nvd.sqlite'}?mode=ro", uri=True)

def write(name, header, rows):
    n = 0
    with open(out / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(header)
        for r in rows: w.writerow(r); n += 1
    print(f"  {name}: {n:,} rows", flush=True); return n

# ---- 1. raw NVD files: same fields build_nvd_index.py reads; has_cpe = at least one vulnerable CPE match (what becomes an 'affects' row)
print("scanning raw NVD files ...", flush=True)
raw_status, raw_cpe, files = {}, {}, []
for f in sorted((snap / "nvd").glob("CVE-*.json.xz")):
    items = json.load(lzma.open(f))["cve_items"]; ids = [it["id"] for it in items]
    n_cpe = 0
    for it in items:
        has = any(m.get("vulnerable") for cfg in it.get("configurations", []) for n in cfg.get("nodes", []) for m in n.get("cpeMatch", []))
        raw_cpe[it["id"]] = has; n_cpe += has; raw_status[it["id"]] = it.get("vulnStatus", "")
    year = f.stem.split("-")[1].split(".")[0]
    in_idx = db.execute("select count(*) from cve where id like ?", (f"CVE-{year}-%",)).fetchone()[0]
    files.append([f.name, f.stat().st_size, len(items), len(set(ids)), sum(i.split("-")[1] != year for i in ids), in_idx, in_idx - len(set(ids)),
                  n_cpe, len(items) - n_cpe])
write("nvd_source_files.csv", ["file", "bytes", "raw_items", "raw_distinct_ids", "ids_with_other_year_prefix", "cves_in_index_with_this_id_year",
                               "index_minus_raw_distinct", "raw_items_with_cpe", "raw_items_without_cpe"], files)

# ---- 2. KEV + EPSS
kev = json.load(open(snap / "cisa_kev.json"))["vulnerabilities"]
epss = {}
with gzip.open(snap / "epss_scores.csv.gz", "rt") as fh:
    for line in fh:
        if line.startswith(("#", "cve")): continue
        c, e, p = line.strip().split(","); epss[c] = (e, p)

# ---- 3. cve.csv (index fields + joins)
mapped = {c: n for c, n in db.execute("select cve, count(*) from affects group by cve")}
prods = {c: n for c, n in db.execute("select cve, count(distinct vendor || ':' || product) from affects group by cve")}
kev_ids = {v["cveID"] for v in kev}
def cve_rows():
    for id_, pub, cvss, sev, k, cwe, descr in db.execute("select id, published, cvss, severity, kev, cwe, descr from cve order by id"):
        yield [id_, pub, pub[:4], "" if cvss is None else cvss, sev, k, int(id_ in kev_ids), cwe, raw_status.get(id_, ""), mapped.get(id_, 0), prods.get(id_, 0),
               int(id_ in mapped), *epss.get(id_, ("", "")), descr]
write("cve.csv", ["cve", "published", "published_year", "cvss", "severity", "kev_flag_in_index", "in_cisa_kev_file", "cwe", "nvd_status",
                  "n_affects_rows", "n_products", "has_product_mapping", "epss", "epss_percentile", "description_first_200_chars"], cve_rows())

# ---- 4. per-year dimensions
yr = defaultdict(Counter); st = defaultdict(Counter)
for id_, pub, cvss, sev, k in db.execute("select id, published, cvss, severity, kev from cve"):
    y = pub[:4]; c = yr[y]; c["cves"] += 1; m = id_ in mapped; c["mapped"] += m; c["unmapped"] += not m
    c["cvss_present"] += cvss is not None; c["critical_9plus"] += (cvss or 0) >= 9; c["kev"] += k
    s = st[(y, raw_status.get(id_, "") or "(none)")]; s["cves"] += 1; s["mapped"] += m
write("cve_by_year.csv", ["published_year", "cves", "with_product_mapping", "without_product_mapping", "pct_without", "cvss_present", "cvss_missing", "critical_9plus", "kev"],
      ([y, c["cves"], c["mapped"], c["unmapped"], round(100 * c["unmapped"] / c["cves"], 1), c["cvss_present"], c["cves"] - c["cvss_present"], c["critical_9plus"], c["kev"]]
       for y, c in sorted(yr.items())))
write("cve_status_by_year.csv", ["published_year", "nvd_status", "cves", "with_product_mapping", "without_product_mapping"],
      ([y, s_, c["cves"], c["mapped"], c["cves"] - c["mapped"]] for (y, s_), c in sorted(st.items())))

# ---- 5. KEV
write("kev.csv", ["cve", "vendor_project", "product", "date_added", "ransomware_use", "in_nvd_index", "nvd_has_product_mapping", "epss"],
      ([v["cveID"], v["vendorProject"], v["product"], v["dateAdded"], v["knownRansomwareCampaignUse"],
        int(db.execute("select 1 from cve where id=?", (v["cveID"],)).fetchone() is not None), int(v["cveID"] in mapped), epss.get(v["cveID"], ("",))[0]] for v in kev))

# ---- 6. products / affects
write("products.csv", ["vendor", "product", "part", "n_cve"], db.execute("select vendor, product, part, n_cve from products order by n_cve desc"))
n_aff = write("affects.csv", ["vendor", "product", "part", "cve", "version", "start_incl", "start_excl", "end_incl", "end_excl"], db.execute("select * from affects"))
step = max(n_aff // 5000, 1)
with open(out / "affects.csv", encoding="utf-8") as src, open(out / "affects_sample.csv", "w", encoding="utf-8", newline="") as dst:
    for i, line in enumerate(src):
        if i == 0 or i % step == 0: dst.write(line)
print("  affects_sample.csv written")

# ---- 7. summary
tot = db.execute("select count(*) from cve").fetchone()[0]
raw_total = sum(f[2] for f in files); raw_distinct = len(raw_status)
nulls = lambda col, cond: db.execute(f"select count(*) from cve where {cond}").fetchone()[0]
S = [("raw_files", len(files)), ("raw_items_total", raw_total), ("raw_distinct_cve_ids", raw_distinct), ("raw_duplicate_ids", raw_total - raw_distinct),
     ("cves_in_index", tot), ("index_minus_raw_distinct (0 = nothing lost)", tot - raw_distinct),
     ("raw_cves_with_cpe", sum(raw_cpe.values())), ("index_cves_with_product_mapping", len(mapped)), ("mapping_matches_raw (0 = consistent)", len(mapped) - sum(raw_cpe.values())),
     ("cves_without_product_mapping", tot - len(mapped)), ("pct_without_product_mapping", round(100 * (tot - len(mapped)) / tot, 1)),
     ("affects_rows", n_aff), ("distinct_products_vendor_product_part", db.execute("select count(*) from products").fetchone()[0]),
     ("cvss_missing", nulls("cvss", "cvss is null")), ("severity_blank", nulls("severity", "severity = ''")), ("cwe_blank", nulls("cwe", "cwe = ''")),
     ("description_blank", nulls("descr", "descr = ''")), ("published_min", db.execute("select min(published) from cve").fetchone()[0]),
     ("published_max", db.execute("select max(published) from cve").fetchone()[0]), ("kev_entries_in_file", len(kev)),
     ("kev_present_in_index", sum(1 for k in kev_ids if db.execute("select 1 from cve where id=?", (k,)).fetchone())), ("kev_with_product_mapping", sum(k in mapped for k in kev_ids)),
     ("kev_flag_set_in_index", db.execute("select count(*) from cve where kev=1").fetchone()[0]), ("epss_scores_loaded", len(epss)),
     ("cves_with_epss", sum(1 for (c,) in db.execute("select id from cve") if c in epss))]
S += [(f"nvd_status:{s}", n) for s, n in Counter(raw_status.values()).most_common()]
write("summary.csv", ["metric", "value"], S)
print("done ->", out)
