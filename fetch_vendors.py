#!/usr/bin/env python3
"""Collect vendor lifecycle data that has no (free) bulk API. Run ONCE with internet; stdlib only.
Output: snapshot/vendor_lifecycle.json  -> list of records in ONE common schema:
   vendor, product, label_alias[], cycle, release_date, end_sale, end_support_full, eol, end_security_support, category, source, url
(all dates ISO yyyy-mm-dd or "").  cmdb_enrich.py loads this file next to endoflife.date / Red Hat data.
(end_security_support is filled by Cisco only; the engine ignores it for now.)

  python3 -I fetch_vendors.py --out snapshot                      # microsoft + juniper + vmware (public KB) + manual/*.csv
  CISCO_CLIENT_ID=.. CISCO_CLIENT_SECRET=.. python3 -I fetch_vendors.py --out snapshot --only cisco
Records of vendors that are NOT re-run (or whose run fails) are kept from the existing file.

How each vendor is obtained (and why):
  microsoft : scrape learn.microsoft.com/lifecycle/products/<slug> pages (URLs from the official sitemap), ~800 pages
  juniper   : scrape support.juniper.net/support/eol/product/<family>/ HTML tables
  cisco     : official Support API "EoX v5", EOXByDates over ONE wide announcement window (2000..now+5) = the whole
              catalogue, 1,000 records per page (CISCO_WINDOW=year: one query per year instead).
              Auth = OAuth2 client credentials (token lasts 1 h, refreshed automatically). Access is NOT given by a free
              Cisco.com account: the Cisco ID must be enabled for the Support APIs (Smart Net Total Care / Partner Support
              Service); otherwise calls fail with "HTTP 403 Developer Inactive" and this run stops without touching old data.
              Field mapping (OpenEoX name):  EndOfSaleDate -> end_sale (end_of_sales)
                                             EndOfSecurityVulSupportDate -> end_security_support (end_of_security_support)
                                             EndOfSWMaintenanceReleases -> end_support_full
                                             LastDateOfSupport -> eol (end_of_life)
  vmware    : lifecycle matrix is behind the Broadcom login -> public KB "End of General Support" tables + manual/*.csv export
  netapp    : EOA/EOS lives in Hardware Universe / login-only -> manual/*.csv import only (no reliable public source)
  ibm       : official bulk CSV (ibm.com/support/pages/product-lifecycle-list), no login. Hardware only by default (2.4k machine
              types); --ibm-software adds software. GA -> release_date, Withdrawn from Marketing -> end_sale,
              Transition to Extended/End of Support -> eol, Completion of Extended -> end_extended. MTM + "(1747-HC2)" -> aliases.
  apple     : public service-status page (support.apple.com/102772): model names under "vintage" / "obsolete", NO dates -> stored as
              status (obsolete = EOL for the engine; vintage = unknown). iPhone/iPad/Watch skipped: endoflife.date has them with dates.
  nvidia    : Jetson lifecycle page tables (developer.nvidia.com/embedded/lifecycle): "available through" -> end_sale, "date of EOL" -> eol,
              month precision -> last day of the month. Networking (Mellanox) EOL notices are PDFs: not collected (no PDF parser, stdlib only).
  (dell, hp/hpe, sun/oracle, alcatel-lucent: no collector. Dell and HP block scripted clients (HTTP 403); the others publish no bulk list.)
  Every new collector fails loudly (keeps the old rows, exit 1) when its download or expected layout is missing.
"""
import argparse, calendar, csv, html, io, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
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
        try: return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError: pass
    return ""
def rec(vendor, product, cycle, source, url, category="", aliases=(), release="", end_sale="", end_full="", eol="", end_security="", end_extended="", status=""):
    """end_extended = end of paid/extended support; status = 'vintage' | 'obsolete' for vendors that publish a status but no date (Apple)."""
    return dict(vendor=vendor, product=product, label_alias=list(aliases), cycle=cycle, release_date=release, end_sale=end_sale,
                end_support_full=end_full, eol=eol, end_security_support=end_security, end_extended=end_extended, status=status,
                category=category, source=source, url=url)
def month_end(s):
    """'January 2032' -> '2032-01-31' (a vendor saying 'available through January 2032' means through the end of that month); anything else -> ''"""
    try: d = datetime.strptime((s or "").strip(), "%B %Y"); return d.replace(day=calendar.monthrange(d.year, d.month)[1]).strftime("%Y-%m-%d")
    except ValueError: return ""

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
class CollectorError(RuntimeError):
    """A collector could not do its job (download failed, page layout changed): main() keeps that vendor's existing records and exits 1."""
class CiscoError(CollectorError):
    """Hard failure of the Cisco collector (auth, access, or repeated API errors): the run must not overwrite old Cisco data."""

class CiscoEox:
    """Minimal EoX v5 client: OAuth2 client-credentials token (1 h, refreshed before expiry or after a 401), retries, explicit errors."""
    BASE = "https://apix.cisco.com/supporttools/eox/rest/5"
    def __init__(self, cid, sec):
        self.cid, self.sec, self.token, self.expires = cid, sec, None, 0.0
    def _refresh(self):
        url = os.environ.get("CISCO_TOKEN_URL", "https://id.cisco.com/oauth2/default/v1/token")
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.sec}).encode()
        req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r: j = json.load(r)
        except urllib.error.HTTPError as e:
            raise CiscoError(f"token request failed: HTTP {e.code} (check CISCO_CLIENT_ID / CISCO_CLIENT_SECRET / CISCO_TOKEN_URL)") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise CiscoError(f"token request failed: {e}") from e
        if "access_token" not in j: raise CiscoError("token response has no access_token")
        self.token, self.expires = j["access_token"], time.time() + int(j.get("expires_in", 3600))
    def call(self, path, tries=4):
        refreshed_on_401 = False
        for i in range(tries):
            if not self.token or time.time() > self.expires - 120: self._refresh()
            req = urllib.request.Request(f"{self.BASE}/{path}", headers={**UA, "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r: return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed_on_401: refreshed_on_401 = True; self.token = None; continue
                if e.code in (401, 403):
                    raise CiscoError(f"HTTP {e.code} on {path.split('?')[0]}: the Cisco ID is not enabled for the EoX Support API "
                                     "(needs Smart Net Total Care or Partner Support Service; a free Cisco.com account is not enough)") from e
                if e.code in (429, 500, 502, 503, 504) and i < tries - 1: time.sleep(5 * (i + 1)); continue
                raise CiscoError(f"HTTP {e.code} on {path.split('?')[0]}") from e
            except (urllib.error.URLError, TimeoutError, ValueError) as e:      # network error or non-JSON body
                if i < tries - 1: time.sleep(3 * (i + 1)); continue
                raise CiscoError(f"{path.split('?')[0]}: {e}") from e

def cisco():
    cid, sec = os.environ.get("CISCO_CLIENT_ID"), os.environ.get("CISCO_CLIENT_SECRET")
    if not (cid and sec): print("cisco: skipped (set CISCO_CLIENT_ID / CISCO_CLIENT_SECRET from the Cisco API Console)"); return None
    api, out, seen = CiscoEox(cid, sec), [], set()
    date = lambda x: iso((x or {}).get("value", "")) if isinstance(x, dict) else ""      # {"value": "2019-05-01", "dateFormat": "YYYY-MM-DD"}
    last_year = datetime.now().year + 5                       # announcements up to 5 years ahead
    # default: ONE wide announcement-date window = the whole catalogue, paged 1,000 records at a time (no per-product calls).
    # CISCO_WINDOW=year falls back to one query per announcement year, in case the API caps the size of a wide result.
    windows = ([(f"{y}-01-01", f"{y}-12-31") for y in range(2000, last_year + 1)] if os.environ.get("CISCO_WINDOW") == "year"
               else [("2000-01-01", f"{last_year}-12-31")])
    for start, end in windows:
        page = 1
        while True:
            j = api.call(f"EOXByDates/{page}/{start}/{end}?responseencoding=json&eoxAttrib=EO_EXTERNAL_ANNOUNCE_DATE")
            for e in j.get("EOXRecord", []):
                pid = e.get("EOLProductID")
                if "EOXError" in e or not pid or pid in seen: continue        # a year with no data returns a single EOXError record
                seen.add(pid)
                out.append(rec("cisco", pid, pid, "cisco EoX v5", e.get("LinkToProductBulletinURL", ""), "hardware", aliases=[e["ProductIDDescription"]] if e.get("ProductIDDescription") else [],
                               release=date(e.get("EOXExternalAnnouncementDate")), end_sale=date(e.get("EndOfSaleDate")),
                               end_full=date(e.get("EndOfSWMaintenanceReleases")), eol=date(e.get("LastDateOfSupport")),
                               end_security=date(e.get("EndOfSecurityVulSupportDate"))))
            last = int((j.get("PaginationResponseRecord") or {}).get("LastIndex", 1) or 1)
            if page >= last: break
            page += 1; time.sleep(0.3)
            if page % 10 == 0: print(f"  cisco {start}..{end}: page {page}/{last}, {len(out)} PIDs so far", flush=True)
        print(f"  cisco {start}..{end}: {len(out)} PIDs so far", flush=True)
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

# ---------------------------------------------------------------- IBM (official bulk CSV, no login)
IBM_CSV = "https://www.ibm.com/support/pages/space/product-lifecycle/ibm_product_lifecycle_list.csv"
IBM_TYPES = {"HW"}                                  # --ibm-software adds "SW" (another ~11,800 rows)
def ibm():
    text = get(IBM_CSV)
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿")))) if text else []
    if not rows or not {"Type", "IBM Product", "MTM", "General Availability"} <= set(rows[0]): raise CollectorError("ibm: lifecycle CSV missing or its header changed")
    out = []
    for r in rows:
        if r["Type"] not in IBM_TYPES: continue
        name = re.sub(r"\s*-\s*Withdrawal notification\s*$", "", r["IBM Product"]).strip()
        mtm = "" if r["MTM"].strip() in ("", "N/A") else r["MTM"].strip()                      # machine type + model, e.g. 1747HC2
        pm = re.search(r"\((\d{4}-[A-Z0-9]{3})\)", name)                                      # "(1747-HC2)" in the title
        vrm = "" if r["VRM"].strip() in ("", "N/A") else r["VRM"].strip()
        ext, eos = iso(r["Completion of Extended, Sustained, Extension availability"]), iso(r["Transition to Extended/Sustained, End of Support"])
        hw = r["Type"] == "HW"
        base = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()                                  # "IBM Power System S922 Server (9009-22A)" -> "IBM Power System S922 Server"
        short = re.sub(r"^IBM\s+", "", base)                                                  # -> "Power System S922 Server"
        lean = re.sub(r"\s+", " ", re.sub(r"\b(?:System|Systems|Server)\b", " ", short)).strip()   # -> "Power S922": how a CMDB usually spells it
        names = {mtm, pm.group(1) if pm else "", base, short, lean} - {"", name}
        out.append(rec("ibm", name, (mtm or name) if hw else (vrm or name), "ibm.com/support/pages/product-lifecycle-list", r.get("More Info URL") or "https://www.ibm.com/support/pages/product-lifecycle-list",
                       "hardware" if hw else "software", aliases=sorted(a for a in names if len(a) >= 5 or a == mtm),
                       release=iso(r["General Availability"]), end_sale=iso(r["Withdrawn from Marketing"]), eol=eos or ext, end_extended=ext))
    if sum(r["category"] == "hardware" for r in out) < 1000 and "HW" in IBM_TYPES: raise CollectorError(f"ibm: only {len(out)} rows, expected thousands (format change?)")
    return out

# ---------------------------------------------------------------- Apple (public service-status page: model lists, NO dates)
APPLE_URL = "https://support.apple.com/en-us/102772"
APPLE_SKIP = {"iphone", "ipad", "watch"}            # already in the catalog WITH dates (endoflife.date): a date-less record here would outrank them
def apple():
    page = get(APPLE_URL); out = []
    for part in re.split(r"(?=<h2[ >])", page):
        m = re.match(r'<h2 id="(.+?)(vintage|obsolete|obs)"', part)                            # ids: macvintage, macobsolete, ipodobsolete, tvobs ...
        if not m or m.group(1) in APPLE_SKIP: continue
        family, status = m.group(1), "vintage" if m.group(2) == "vintage" else "obsolete"
        for li in re.findall(r"<li[^>]*>.*?</li>", part, flags=re.S):
            name = clean(li)
            if name: out.append(rec("apple", name, name, "support.apple.com/102772", APPLE_URL, "hardware", status=status))
    if len(out) < 100: raise CollectorError(f"apple: only {len(out)} models parsed, expected hundreds (page layout changed?)")
    return out

# ---------------------------------------------------------------- NVIDIA Jetson (public lifecycle page, month-precision dates)
JETSON_URL = "https://developer.nvidia.com/embedded/lifecycle"
def nvidia():
    out = []
    for t in tables(get(JETSON_URL)):
        if not t or len(t[0]) != 2: continue
        head = t[0][1].lower()
        if "available through" not in head and "date of eol" not in head: continue           # skips the developer-kit table (names only)
        for r in t[1:]:
            d = month_end(r[1]) if len(r) == 2 else ""
            if not d: continue
            eol = "date of eol" in head                                                       # EOL date vs 'available through' (= last ship month)
            out.append(rec("nvidia", r[0], r[0], "developer.nvidia.com/embedded/lifecycle", JETSON_URL, "hardware", end_sale=d, eol=d if eol else ""))
    if len(out) < 10: raise CollectorError(f"nvidia: only {len(out)} Jetson rows, expected ~30 (page layout changed?)")
    return out

COLLECTORS = dict(microsoft=microsoft, juniper=juniper, vmware=vmware, cisco=cisco, ibm=ibm, apple=apple, nvidia=nvidia, manual=manual)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="snapshot"); ap.add_argument("--only", nargs="*", choices=list(COLLECTORS))
    ap.add_argument("--ibm-software", action="store_true", help="also keep IBM software rows (default: hardware only)")
    a = ap.parse_args(); out = Path(a.out) / "vendor_lifecycle.json"
    if a.ibm_software: IBM_TYPES.add("SW")
    old = []
    if out.exists():
        with open(out) as fh: old = json.load(fh)
    run = a.only or [k for k in COLLECTORS if k != "cisco"]
    owned = lambda r, k: r["source"].startswith("manual:") if k == "manual" else r["vendor"] == k      # which records a collector replaces
    failed = []
    for k in run:
        try: got = COLLECTORS[k]()
        except CollectorError as e: print(f"{k}: FAILED, keeping existing {k} records: {e}", file=sys.stderr); failed.append(k); continue
        if got is None: continue                                  # collector skipped (no credentials): keep what is there
        print(f"{k}: {len(got)} records"); old = [r for r in old if not owned(r, k)] + got
    Path(a.out).mkdir(exist_ok=True)
    with open(out, "w") as fh: json.dump(old, fh, indent=0)
    print("->", out, len(old), "records")
    if failed: sys.exit(1)

if __name__ == "__main__": main()
