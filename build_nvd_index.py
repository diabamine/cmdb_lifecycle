"""Reduce the full NVD CVE base (per-year files from fkie-cad/nvd-json-data-feeds) to a compact SQLite index.
Offline, run once after fetch_snapshot.py --nvd.   Usage:  python3 -I build_nvd_index.py snapshot/nvd snapshot/nvd.sqlite
  cve(id, published, cvss, severity, kev, cwe, descr)
  affects(vendor, product, part, cve, ver, si, se, ei, ee)   -- one row per vulnerable CPE match
     ver = exact version ('' = any version '*', '-' = not applicable), si/se = start incl/excl, ei/ee = end incl/excl (version ranges)
  products(vendor, product, part, n_cve)                       -- distinct names, used for name matching
"""
import lzma, json, sqlite3, sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
dst.unlink(missing_ok=True)
db = sqlite3.connect(dst)
db.executescript("""create table cve(id text primary key, published text, cvss real, severity text, kev int, cwe text, descr text);
create table affects(vendor text, product text, part text, cve text, ver text, si text, se text, ei text, ee text);""")

def best_cvss(metrics):
    """Highest-priority CVSS: v4.0 > v3.1 > v3.0 > v2, preferring the NVD/primary entry."""
    for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        ms = metrics.get(k) or []
        if ms:
            m = next((x for x in ms if x.get("type") == "Primary"), ms[0])["cvssData"]
            return m.get("baseScore"), m.get("baseSeverity", "")
    return None, ""

def cpe_matches(nodes):
    for n in nodes:
        yield from (m for m in n.get("cpeMatch", []) if m.get("vulnerable"))

for f in sorted(src.glob("CVE-*.json.xz")):
    items = json.load(lzma.open(f))["cve_items"]
    cves, aff = [], set()                      # set() removes duplicate (product, range) rows within a CVE
    for it in items:
        score, sev = best_cvss(it.get("metrics") or {})
        descr = next((d["value"] for d in it.get("descriptions", []) if d["lang"] == "en"), "")[:200]
        cwe = next((d["value"] for w in it.get("weaknesses", []) for d in w["description"]), "")
        cves.append((it["id"], it["published"][:10], score, sev, int("cisaExploitAdd" in it), cwe, descr))
        for cfg in it.get("configurations", []):
            for m in cpe_matches(cfg["nodes"]):
                p = m["criteria"].split(":")                 # cpe:2.3:part:vendor:product:version:...
                ver = "" if p[5] == "*" else p[5]            # "*" = any version, "-" = not applicable
                aff.add((p[3], p[4], p[2], it["id"], ver, m.get("versionStartIncluding", ""), m.get("versionStartExcluding", ""),
                         m.get("versionEndIncluding", ""), m.get("versionEndExcluding", "")))
    db.executemany("insert or replace into cve values (?,?,?,?,?,?,?)", cves)
    db.executemany("insert into affects values (?,?,?,?,?,?,?,?,?)", aff)
    db.commit(); print(f.name, len(cves), "CVEs", len(aff), "affects rows", flush=True)

db.executescript("""create index ix_aff on affects(vendor, product);
create table products as select vendor, product, part, count(distinct cve) n_cve from affects group by vendor, product, part;
create index ix_prod on products(product);""")
print("CVEs:", db.execute("select count(*) from cve").fetchone()[0], "| products:", db.execute("select count(*) from products").fetchone()[0],
      "| affects:", db.execute("select count(*) from affects").fetchone()[0])
db.execute("vacuum")
