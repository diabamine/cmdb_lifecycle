#!/usr/bin/env python3
"""Prototype: build a normalized product-lifecycle table from
 - endoflife.date  (https://endoflife.date/api/<product>.json)
 - Red Hat Product Life Cycle API (https://access.redhat.com/product-life-cycles/api/v1/products)
and verify/enrich a CMDB extract (CSV).

Usage: python3 -I lifecycle_check.py cmdb_extract.csv [--out-dir out] [--refresh]
Stdlib only.
"""
import argparse, csv, json, sys, time, urllib.parse, urllib.request
from datetime import date, datetime
from pathlib import Path

EOL_URL = "https://endoflife.date/api/{slug}.json"
RH_URL = "https://access.redhat.com/product-life-cycles/api/v1/products?name={name}"
CACHE_TTL = 24 * 3600

# CMDB product label (lower-case) -> (endoflife.date slug | None, Red Hat product name | None)
# This is the one piece of curation a real CMDB needs: a product alias catalogue.
PRODUCT_MAP = {
    "red hat enterprise linux": ("rhel", "Red Hat Enterprise Linux"),
    "rhel":                     ("rhel", "Red Hat Enterprise Linux"),
    "openshift container platform": ("red-hat-openshift", "OpenShift Container Platform"),
    "oracle linux":   ("oracle-linux", None),
    "postgresql":     ("postgresql", None),
    "ubuntu":         ("ubuntu", None),
    "windows server": ("windows-server", None),
    "python":         ("python", None),
}

# ---------- fetch (with tiny file cache) ----------
def fetch_json(url, cache_dir: Path, refresh=False):
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / (urllib.parse.quote(url, safe="") [:150] + ".json")
    if not refresh and f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
        return json.loads(f.read_text())
    req = urllib.request.Request(url, headers={"User-Agent": "cmdb-lifecycle-prototype/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"  ! fetch failed {url}: {e}", file=sys.stderr)
        return None
    f.write_text(json.dumps(data))
    return data

# ---------- normalization ----------
def pdate(v):
    """'2027-05-31', '2027-05-31T00:00:00.000Z' -> date; True/False/'N/A'/'Ongoing' -> None"""
    if isinstance(v, str) and v[:4].isdigit():
        return datetime.strptime(v[:10], "%Y-%m-%d").date()
    return None

def norm_endoflife(slug, rows):
    """endoflife.date: eol / support / extendedSupport may be date or bool."""
    out = []
    for r in rows:
        out.append(dict(
            source="endoflife.date", product=slug, cycle=str(r["cycle"]),
            release_date=pdate(r.get("releaseDate")),
            support_end=pdate(r.get("support")),
            eol=pdate(r.get("eol")),
            extended_end=pdate(r.get("extendedSupport")),
            eol_flag=r.get("eol") if isinstance(r.get("eol"), bool) else None,
            latest=r.get("latest"), lts=bool(r.get("lts")),
        ))
    return out

def norm_redhat(product):
    """Red Hat: each version has ordered phases. Use all_phases' ptype to classify:
       first 'normal' phase after GA = full support, later 'normal' = maintenance,
       'extended' = ELS / long-life. EOL = end of last 'normal' phase."""
    ptype = {p["name"]: p["ptype"] for p in product["all_phases"]}
    out = []
    for v in product["versions"]:
        ph = v.get("phases", [])
        ga = next((p for p in ph if "general availability" in p["name"].lower()), None)
        normal = [p for p in ph if ptype.get(p["name"]) == "normal" and p is not ga]
        ext = [p for p in ph if ptype.get(p["name"]) == "extended"]
        out.append(dict(
            source="redhat", product=product["name"], cycle=str(v["name"]),
            release_date=pdate(ga["end_date"]) if ga else None,
            support_end=pdate(normal[0]["end_date"]) if len(normal) > 1 else None,
            eol=pdate(normal[-1]["end_date"]) if normal else None,
            extended_end=max((d for d in (pdate(p["end_date"]) for p in ext) if d), default=None),
            eol_flag=None, latest=v.get("final_minor_release") or None, lts=False,
        ))
    return out

def build_lifecycle_table(cache_dir, refresh):
    table = []
    for slug in sorted({s for s, _ in PRODUCT_MAP.values() if s}):
        rows = fetch_json(EOL_URL.format(slug=slug), cache_dir, refresh)
        if rows: table += norm_endoflife(slug, rows)
    for name in sorted({n for _, n in PRODUCT_MAP.values() if n}):
        data = fetch_json(RH_URL.format(name=urllib.parse.quote(name)), cache_dir, refresh)
        for p in (data or {}).get("data", []):
            if p["name"] == name: table += norm_redhat(p)
    return table

# ---------- verification ----------
def match_cycle(version, cycles):
    """Longest dotted-prefix match: '3.8.10' -> '3.8' -> '3'; case-insens for names like '2012 R2'."""
    parts = str(version).strip().lower().replace(" ", "-").split(".")
    for n in range(len(parts), 0, -1):
        cand = ".".join(parts[:n])
        for c in cycles:
            if c["cycle"].lower().replace(" ", "-") == cand: return c
    return None

def status(c, today):
    if c["eol"] and c["eol"] < today: return "EOL"
    if c["eol_flag"] is True: return "EOL"
    if c["support_end"] and c["support_end"] < today: return "MAINTENANCE"  # security-only phase
    return "SUPPORTED"

def verify(cmdb_rows, table, today):
    idx = {}
    for t in table: idx.setdefault((t["source"], t["product"]), []).append(t)
    out = []
    for r in cmdb_rows:
        res = dict(r, lifecycle_status="UNKNOWN", eol_date="", days_to_eol="", support_end="",
                   extended_end="", latest_release="", sources="", note="")
        slug, rhname = PRODUCT_MAP.get(r["product"].strip().lower(), (None, None))
        if not (slug or rhname):
            res["note"] = "product not in alias catalogue"; out.append(res); continue
        hits = []
        if slug:   hits.append(match_cycle(r["version"], idx.get(("endoflife.date", slug), [])))
        if rhname: hits.append(match_cycle(r["version"], idx.get(("redhat", rhname), [])))
        hits = [h for h in hits if h]
        if not hits:
            res["note"] = "version/cycle not found in sources"; out.append(res); continue
        # Red Hat is authoritative for Red Hat products; endoflife.date otherwise
        main = next((h for h in hits if h["source"] == "redhat"), hits[0])
        res.update(lifecycle_status=status(main, today),
                   eol_date=main["eol"] or "", support_end=main["support_end"] or "",
                   extended_end=main["extended_end"] or "", latest_release=main["latest"] or "",
                   sources="+".join(h["source"] for h in hits))
        if main["eol"]: res["days_to_eol"] = (main["eol"] - today).days
        notes = []
        if len(hits) == 2 and hits[0]["eol"] != hits[1]["eol"]:
            notes.append(f"source mismatch EOL: {hits[0]['source']}={hits[0]['eol']} vs {hits[1]['source']}={hits[1]['eol']}")
        if main["extended_end"] and res["lifecycle_status"] == "EOL" and main["extended_end"] >= today:
            notes.append(f"paid extended support until {main['extended_end']}")
        res["note"] = "; ".join(notes)
        out.append(res)
    return out

def write_csv(path, rows):
    if not rows: return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmdb_csv"); ap.add_argument("--out-dir", default="out")
    ap.add_argument("--refresh", action="store_true"); ap.add_argument("--today")
    a = ap.parse_args()
    today = date.fromisoformat(a.today) if a.today else date.today()
    out = Path(a.out_dir); out.mkdir(exist_ok=True)

    table = build_lifecycle_table(out / "cache", a.refresh)
    write_csv(out / "lifecycle_table.csv", table)
    print(f"lifecycle table: {len(table)} product cycles -> {out/'lifecycle_table.csv'}")

    with open(a.cmdb_csv, newline="") as f: cmdb = list(csv.DictReader(f))
    res = verify(cmdb, table, today)
    write_csv(out / "cmdb_enriched.csv", res)

    print(f"\n{'asset':6} {'host':14} {'product':28} {'ver':8} {'status':12} {'eol':11} {'days':>6}  note")
    for r in res:
        print(f"{r['asset_id']:6} {r['hostname']:14} {r['product'][:28]:28} {r['version']:8} "
              f"{r['lifecycle_status']:12} {str(r['eol_date']):11} {str(r['days_to_eol']):>6}  {r['note']}")
if __name__ == "__main__":
    main()
