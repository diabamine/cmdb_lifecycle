#!/usr/bin/env python3
"""Collect vendor lifecycle data that has no (free) bulk API. Run ONCE with internet; stdlib only.
Output: snapshot/vendor_lifecycle.json  -> list of records in ONE common schema:
   vendor, product, label_alias[], cycle, release_date, end_sale, end_support_full, eol, category, source, url
(all dates ISO yyyy-mm-dd or "").  cmdb_enrich.py loads this file next to endoflife.date / Red Hat data.

  python3 -I fetch_vendors.py --out snapshot                      # microsoft + juniper + vmware (public KB) + manual/*.csv
  CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -I fetch_vendors.py --out snapshot --only cisco

How each vendor is obtained (and why):
  microsoft : scrape learn.microsoft.com/lifecycle/products/<slug> pages (URLs from the official sitemap), ~800 pages
  juniper   : scrape support.juniper.net/support/eol/product/<family>/ HTML tables
  cisco     : official Support API "EoX v5" (OAuth2 client credentials from the Cisco API Console; needs a service contract)
  vmware    : lifecycle matrix is behind the Broadcom login -> public KB "End of General Support" tables + manual/*.csv export
  netapp    : EOA/EOS lives in Hardware Universe / login-only -> manual/*.csv import only (no reliable public source)
"""
import argparse, csv, html, json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}   # some sites block bare clients

def get(url, headers=None, data=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})}, data=data)
            with urllib.request.urlopen(req, timeout=60) as r: return r.read().decode("utf-8", "ignore")
        except Exception as e:
            if i == tries - 1: print(f"  ! {url[:90]}: {e}", file=sys.stderr); return ""
            time.sleep(2 * (i + 1))

# ---------------------------------------------------------------- tiny HTML + date helpers
clean = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
def tables(page):
    """HTML -> list of tables; each table is a list of rows; each row a list of cell texts."""
    return [[[clean(c) for c in re.findall(r"<t[dh][^>]*>.*?</t[dh]>", r, flags=re.S)] for r in re.findall(r"<tr.*?</tr>", t, flags=re.S)]
            for t in re.findall(r"<table.*?</table>", page, flags=re.S)]
def iso(s):
    """'10/30/2026', '1/12/2022 6:59:59 AM', 'April 2, 2020', '2026-01-31' -> '2026-01-31'; anything else -> ''"""
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try: return datetime.strptime(s[:len(s)], fmt).strftime("%Y-%m-%d")
        except ValueError: pass
    return ""
def rec(vendor, product, cycle, source, url, category="", aliases=(), release="", end_sale="", end_full="", eol=""):
    return dict(vendor=vendor, product=product, label_alias=list(aliases), cycle=cycle, release_date=release, end_sale=end_sale,
                end_support_full=end_full, eol=eol, category=category, source=source, url=url)

# ---------------------------------------------------------------- Microsoft (HTML scrape)
def microsoft():
    base = "https://learn.microsoft.com"
    sm = get(f"{base}/_sitemaps/lifecycle_en-us_1.xml")
    urls = sorted(set(u for u in re.findall(r"<loc>([^<]+)</loc>", sm) if "/lifecycle/products/" in u))
    print(f"microsoft: {len(urls)} product pages")
    def one(u):
        page = get(u); out = []
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S); name = clean(m.group(1)) if m else u.rsplit("/", 1)[-1]
        for t in tables(page):
            if not t or not any("start date" in c.lower() for c in t[0]): continue
            h = [c.lower() for c in t[0]]
            col = lambda *k: next((i for i, c in enumerate(h) if any(x in c for x in k)), None)
            i_s, i_m, i_e, i_r = col("start"), col("mainstream"), col("extended"), col("retire", "end date")
            for r in t[1:]:
                if len(r) < 2: continue
                get_ = lambda i: iso(r[i]) if i is not None and i < len(r) else ""
                end_full = get_(i_m); eol = get_(i_e) or (get_(i_r) if i_r not in (i_m, i_e) else "") or end_full
                out.append(rec("microsoft", name, r[0], "learn.microsoft.com/lifecycle", u, "os-or-app", release=get_(i_s), end_full=end_full, eol=eol))
        return out
    with ThreadPoolExecutor(6) as ex: return [x for chunk in ex.map(one, urls) for x in chunk]

# ---------------------------------------------------------------- Juniper (HTML scrape, hardware)
def juniper():
    base = "https://support.juniper.net/support/eol"
    idx = get(f"{base}/")
    fams = sorted(set(re.findall(r"/support/eol/product/([a-z0-9_]+)", idx))) or \
        ["ex_series", "qfx_series", "mx_series", "srx_series", "ptx_series", "acx_series"]   # fallback: families seen on the site
    print("juniper families:", fams); out = []
    for f in fams:
        url = f"{base}/product/{f}/"
        for t in tables(get(url)):
            if not t: continue
            h = [c.lower() for c in t[0]]
            col = lambda k: next((i for i, c in enumerate(h) if k in c), None)
            i_sku = col("sku") if col("sku") is not None else col("product")
            i_tsb, i_ann, i_ord, i_eng, i_sup = col("technical support bulletin"), col("eol announced"), col("last order"), col("end of engineering"), col("end of support")
            if i_sku is None or i_sup is None: continue
            for r in t[1:]:
                if len(r) <= i_sup: continue
                skus = [s.strip() for s in r[i_sku].split(",") if s.strip()]
                label = re.sub(r"^TSB\d+\s*-\s*End of Life Announcement\s*[:\-]?\s*", "", r[i_tsb], flags=re.I).strip() if i_tsb is not None else (skus[0] if skus else "")
                if not label: continue
                out.append(rec("juniper", label, label, "support.juniper.net/eol", url, "hardware", aliases=skus,
                               release=iso(r[i_ann]) if i_ann is not None else "", end_sale=iso(r[i_ord]) if i_ord is not None else "",
                               end_full=iso(r[i_eng]) if i_eng is not None else "", eol=iso(r[i_sup])))
        time.sleep(0.5)
    return out

# ---------------------------------------------------------------- VMware (public Broadcom KB tables)
VMWARE_KB = [415405, 459135, 326984]   # "End of General Support for vSphere 7.0 / 8.0 / 6.5-6.7-7.0" - public articles
def vmware():
    out = []
    for kb in VMWARE_KB:
        url = f"https://knowledge.broadcom.com/external/article/{kb}"
        for t in tables(get(url)):
            if not t or "product version" not in t[0][0].lower(): continue
            for r in t[1:]:
                m = re.match(r"(.+?)\s+(\d[\w.]*)$", r[0]) if len(r) >= 3 else None      # 'ESXi 7.x' -> product ESXi, cycle 7.x
                if m: out.append(rec("vmware", m.group(1), m.group(2), f"knowledge.broadcom.com/{kb}", url, "virtualization", release=iso(r[1]), end_full=iso(r[2]), eol=iso(r[2])))
    return out

# ---------------------------------------------------------------- Cisco (official EoX v5 API)
def cisco():
    cid, sec = os.environ.get("CISCO_CLIENT_ID"), os.environ.get("CISCO_CLIENT_SECRET")
    if not (cid and sec): print("cisco: skipped (set CISCO_CLIENT_ID / CISCO_CLIENT_SECRET from the Cisco API Console)"); return []
    tok_url = os.environ.get("CISCO_TOKEN_URL", "https://id.cisco.com/oauth2/default/v1/token")
    token = json.loads(get(tok_url, {"Content-Type": "application/x-www-form-urlencoded"},
                           urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": sec}).encode()))["access_token"]
    out, d = [], lambda x: (x or {}).get("value", "") if isinstance(x, dict) else ""
    for year in range(2000, datetime.now().year + 6):         # announcement dates, one year per query to stay under result caps
        page = 1
        while True:
            url = (f"https://apix.cisco.com/supporttools/eox/rest/5/EOXByDates/{page}/{year}-01-01/{year}-12-31"
                   "?responseencoding=json&eoxAttrib=EO_EXTERNAL_ANNOUNCE_DATE")
            j = json.loads(get(url, {"Authorization": f"Bearer {token}", "Accept": "application/json"}) or "{}")
            for e in j.get("EOXRecord", []):
                if "EOXError" in e: continue
                pid = e["EOLProductID"]
                out.append(rec("cisco", pid, pid, "cisco EoX v5", e.get("LinkToProductBulletinURL", ""), "hardware", aliases=[e.get("ProductIDDescription", "")],
                               release=d(e.get("EOXExternalAnnouncementDate")), end_sale=d(e.get("EndOfSaleDate")),
                               end_full=d(e.get("EndOfSWMaintenanceReleases")) or d(e.get("EndOfSecurityVulSupportDate")), eol=d(e.get("LastDateOfSupport"))))
            pg = j.get("PaginationResponseRecord", {})
            if page >= int(pg.get("LastIndex", 1) or 1): break
            page += 1; time.sleep(0.3)
    return out

# ---------------------------------------------------------------- manual CSV exports (NetApp, VMware portal, anything else)
def manual(folder="manual"):
    """Any manual/*.csv with columns: vendor,product,cycle,release_date,end_sale,end_support_full,eol[,aliases(;-separated),url,category]"""
    out = []
    for f in sorted(Path(folder).glob("*.csv")):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if r.get("product"): out.append(rec(r.get("vendor", f.stem), r["product"], r.get("cycle") or r["product"], f"manual:{f.name}", r.get("url", ""),
                                               r.get("category", ""), [a for a in (r.get("aliases") or "").split(";") if a], iso(r.get("release_date")),
                                               iso(r.get("end_sale")), iso(r.get("end_support_full")), iso(r.get("eol"))))
    print(f"manual: {len(out)} rows from {folder}/*.csv"); return out

COLLECTORS = dict(microsoft=microsoft, juniper=juniper, vmware=vmware, cisco=cisco, manual=manual)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="snapshot"); ap.add_argument("--only", nargs="*", choices=list(COLLECTORS))
    a = ap.parse_args(); out = Path(a.out) / "vendor_lifecycle.json"
    old = json.load(open(out)) if out.exists() and a.only else []       # --only refreshes just those vendors
    run = a.only or [k for k in COLLECTORS if k != "cisco"]
    old = [r for r in old if r["source"].split(":")[0] not in {COLLECTORS[k].__name__ for k in run} and r["vendor"] not in run]
    new = []
    for k in run:
        got = COLLECTORS[k](); print(f"{k}: {len(got)} records"); new += got
    Path(a.out).mkdir(exist_ok=True); json.dump(old + new, open(out, "w"), indent=0); print("->", out, len(old + new), "records")

if __name__ == "__main__": main()
