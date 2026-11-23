#!/usr/bin/env python3
"""Bulk-ingest hardware lifecycle data from the eol.network API (needs a free account + API key). Stdlib only.

  EOL_NETWORK_API_KEY=eol_...                                # in ./.env (gitignored) or exported; never on the command line
  EOL_NETWORK_CONTACT=https://your.site-or-mail              # same place; goes into the User-Agent, keyed calls without one are rejected
  python3 -I fetch_eolnetwork.py --list                      # vendors, SKU counts, what is already pulled (no listing slot used)
  python3 -I fetch_eolnetwork.py --vendors cisco aruba dell  # full vendor listings, 500 rows per call
  python3 -I fetch_eolnetwork.py --all --full-listing-tier   # every vendor (only with full-listing access)

Why it is built this way (eol.network docs, terms and acceptable-use policy):
  * ONE paged listing per vendor, never per-SKU calls: 500 rows per call, ~30 calls/min (limit is 60). Cisco = 127 calls.
  * Free accounts get full listings for 5 vendors per rolling 30 days; a vendor's slot starts at its first listing call.
    Re-listing the same vendor costs no new slot, so --vendors is explicit and --all needs --full-listing-tier.
  * Pull once, store, diff: a vendor is re-pulled only when its generated_at changed (or --force).
  * The API returns hardware only (software/OS releases are on the website only, not collected here).
  * Output (kept local, gitignored: the terms forbid republishing and each key gets synthetic watermark records):
        snapshot/eolnetwork/<vendor>.ndjson.gz     raw rows exactly as received
        snapshot/eolnetwork/manifest.json          per vendor: rows, generated_at, sha256, pulled time
        snapshot/vendor_lifecycle_eolnetwork.json  same record schema as vendor_lifecycle.json; cmdb_enrich.py loads it too
  * SKUs with identical series + dates are grouped into ONE record (SKUs become aliases), like the Juniper feed, which keeps the
    matching index small and loses no date.
  Field mapping:  end_of_sale_date -> end_sale | end_of_sw_maintenance_date -> end_support_full | last_date_of_support -> eol
                  lifecycle_status 'end_of_life' with no date -> status 'obsolete' | replacement_product -> replacement
                  category (hardware/software/service/unknown) -> category | source.url (the vendor's own page) -> url
  --rebuild re-derives the records from the raw archive offline (no key, no slots) after a mapping fix.
Attribution: eol.network, derived from vendor publications. Not authoritative: verify dates with the vendor before acting.
"""
import argparse, difflib, gzip, hashlib, io, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("EOL_NETWORK_BASE", "https://eol.network").rstrip("/")      # overridable for tests only
SOURCE = "eol.network"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")

def load_dotenv(path=".env"):
    """Read EOL_NETWORK_* lines from ./.env into the environment (real environment variables win). Values are never printed."""
    try: lines = open(path, encoding="utf-8").read().splitlines()
    except OSError: return
    for ln in lines:
        m = re.match(r"^\s*(?:export\s+)?(EOL_NETWORK_[A-Z_]+)\s*=\s*(.*?)\s*$", ln)
        if m and m.group(1) not in os.environ:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
            if v: os.environ[m.group(1)] = v

class StopRun(RuntimeError):
    """Account-level problem (key, access, quota, vendor-slot limit): continuing would only make it worse."""
class VendorError(RuntimeError):
    """This vendor could not be pulled completely; nothing of it is merged. Other vendors may continue."""

# ---------------------------------------------------------------- HTTP
class Api:
    def __init__(self, key, contact, rate_per_min):
        self.key, self.ua, self.gap, self.last = key, f"cmdb-lifecycle-ingest/0.1 (+{contact})", 60.0 / rate_per_min, 0.0
        self.remaining_day, self.slots = None, None
    def get(self, path, params=None, tries=5):
        url = f"{BASE}/api/v1/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        for i in range(tries):
            time.sleep(max(0.0, self.last + self.gap - time.monotonic())); self.last = time.monotonic()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json", "User-Agent": self.ua})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    h = r.headers
                    if h.get("X-RateLimit-Remaining-Day", "").isdigit(): self.remaining_day = int(h["X-RateLimit-Remaining-Day"])
                    if h.get("X-Listing-Vendors-Used"): self.slots = f"{h['X-Listing-Vendors-Used']}/{h.get('X-Listing-Vendors-Limit', '?')}"
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try: err = json.load(e).get("error", {})
                except Exception: err = {}
                code, msg = err.get("code", ""), err.get("message", "")
                retry = int(e.headers.get("Retry-After", "60") or 60) if e.headers.get("Retry-After", "60").isdigit() else 60
                if e.code == 429 and code == "rate_limit_minute" and i < tries - 1: print(f"  rate limit, waiting {retry}s", file=sys.stderr); time.sleep(retry); continue
                if e.code == 503 and i < tries - 1: time.sleep(min(retry, 120)); continue
                if e.code == 429 and code == "quota_exhausted": raise StopRun("daily row quota used up (resets 00:00 UTC)") from e
                if e.code == 429 and code == "listing_vendor_limit": raise StopRun(f"free-tier vendor listing limit reached: {msg}") from e
                if e.code in (401, 403) or code in ("bad_user_agent",): raise StopRun(f"HTTP {e.code} {code}: {msg or 'check the key, email verification and account approval'}") from e
                if e.code == 404: raise VendorError(f"{code or 'not found'}: {path}") from e
                if e.code >= 500 and i < tries - 1: time.sleep(5 * (i + 1)); continue
                raise VendorError(f"HTTP {e.code} {code} on {path}") from e
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                if i < tries - 1: time.sleep(3 * (i + 1)); continue
                raise VendorError(f"{path}: {e}") from e

# ---------------------------------------------------------------- pull one vendor
def pull_vendor(api, slug, per_page, expect):
    rows, page, total = [], 1, None
    while True:
        if api.remaining_day is not None and api.remaining_day < per_page: raise StopRun(f"only {api.remaining_day} rows left in today's quota")
        j = api.get(f"vendors/{slug}/products", {"page": page, "per_page": per_page})
        data, meta = j.get("data"), j.get("meta") or {}
        if not isinstance(data, list): raise VendorError(f"{slug}: unexpected response shape on page {page}")
        total = meta.get("total", total); rows += data
        if page % 10 == 0: print(f"  {slug}: page {page}, {len(rows):,}/{total if total is not None else '?'} rows", flush=True)
        if not data or (total is not None and (len(rows) >= total or page > math.ceil(total / per_page) + 1)): break
        page += 1
    if total is not None and len(rows) != total: raise VendorError(f"{slug}: received {len(rows)} rows but meta.total says {total}; not merged")
    if expect and abs(len(rows) - expect) > max(5, 0.02 * expect): print(f"  ! {slug}: {len(rows):,} rows vs {expect:,} in the vendor list (>2% apart)", file=sys.stderr)
    return rows

# ---------------------------------------------------------------- raw archive + records
def write_raw(path, rows):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, io.TextIOWrapper(gz, encoding="utf-8") as fh:
        for r in rows: fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path); return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size

def day(s): return s[:10] if isinstance(s, str) and ISO.match(s) else ""

def to_records(slug, rows):
    """Raw rows -> vendor_lifecycle records; SKUs with the same series, category, status and dates collapse into one record."""
    groups, seen = defaultdict(list), set()
    for r in rows:
        pid = str(r.get("product_id") or "").strip()
        if not pid or pid in seen: continue
        seen.add(pid)
        series = r.get("applicable_series"); series = series[0] if isinstance(series, list) and series else (series if isinstance(series, str) else "")
        # the real field is end_of_sw_maintenance_date; the docs call it end_of_software_support_date (checked against a live Cisco pull)
        eos, eosw, eol = day(r.get("end_of_sale_date")), day(r.get("end_of_sw_maintenance_date") or r.get("end_of_software_support_date")), day(r.get("last_date_of_support"))
        ptype, c = str(r.get("product_type") or "").lower(), str(r.get("category") or "").lower()
        cat = c if c in ("hardware", "software", "service", "unknown") else ("license" if "licen" in ptype else "hardware")
        status = "obsolete" if str(r.get("lifecycle_status") or "").lower() == "end_of_life" and not eol else ""
        groups[(series, cat, status, eos, eosw, eol)].append((pid, r))
    out, labels = [], set()
    for (series, cat, status, eos, eosw, eol), g in groups.items():
        g.sort(key=lambda x: x[0]); first_pid, first = g[0]
        name = str(first.get("product_name") or first_pid).strip()
        # a group is named after its series and dates, not after one arbitrary member SKU ("Catalyst 3850 24 Port PoE" for a 48-port switch)
        label = name if len(g) == 1 else f"{series or name} (EOS {eos or 'n/a'}, EOL {eol or 'n/a'}; {len(g)} SKUs)"
        if label in labels: label += f" [{first_pid}]"
        labels.add(label)
        repl = sorted({str(x.get("replacement_product")) for _, x in g if x.get("replacement_product")})
        src = first.get("source"); vendor_doc = src.get("url") if isinstance(src, dict) else ""                  # the vendor's own publication, when given
        page = f"{BASE}/{slug}/{first['series_slug']}/{urllib.parse.quote(first_pid, safe='/=')}" if first.get("series_slug") else f"{BASE}/{slug}"
        rec = dict(vendor=slug, product=label, label_alias=list(dict.fromkeys([p for p, _ in g] + [name])), cycle=label, release_date="", end_sale=eos,
                   end_support_full=eosw, eol=eol, end_security_support="", end_extended="", status=status, category=cat,
                   source=f"{SOURCE}/{slug}", url=vendor_doc or page, sku_count=len(g))
        if repl: rec["replacement"] = repl[0] if len(repl) == 1 else repl
        out.append(rec)
    return out

def merge(path, slug, records):
    old = json.load(open(path)) if path.exists() else []
    keep = [r for r in old if r.get("vendor") != slug]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh: json.dump(keep + records, fh, indent=0)
    os.replace(tmp, path)

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="snapshot"); ap.add_argument("--vendors", nargs="*", default=[]); ap.add_argument("--all", action="store_true")
    ap.add_argument("--full-listing-tier", action="store_true", help="confirm your account may list every vendor (required by --all)")
    ap.add_argument("--list", action="store_true", help="show vendors and local status, then exit"); ap.add_argument("--force", action="store_true", help="re-pull even if generated_at is unchanged")
    ap.add_argument("--per-page", type=int, default=500); ap.add_argument("--rate", type=float, default=30, help="requests per minute (the API allows 60)")
    ap.add_argument("--contact", default=None)
    ap.add_argument("--rebuild", action="store_true", help="re-derive records from the raw archive: no network, no key, no vendor slots")
    load_dotenv()
    a = ap.parse_args(); a.contact = a.contact or os.environ.get("EOL_NETWORK_CONTACT")
    if a.rebuild:
        arch = Path(a.out) / "eolnetwork"; vpath = Path(a.out) / "vendor_lifecycle_eolnetwork.json"
        for s in a.vendors or sorted(p.name[:-len(".ndjson.gz")] for p in arch.glob("*.ndjson.gz")):
            recs = to_records(s, [json.loads(ln) for ln in gzip.open(arch / f"{s}.ndjson.gz", "rt", encoding="utf-8")]); merge(vpath, s, recs)
            print(f"{s}: rebuilt {len(recs):,} records from the raw archive")
        return
    key = os.environ.get("EOL_NETWORK_API_KEY")
    if not key: sys.exit("set EOL_NETWORK_API_KEY (register at eol.network, verify the email, mint a key in /account)")
    if not a.contact: sys.exit("set EOL_NETWORK_CONTACT or --contact (a URL or address for the User-Agent; eol.network rejects keyed calls without one)")
    if not 1 <= a.per_page <= 500: sys.exit("--per-page must be 1..500")
    if a.all and not a.full_listing_tier: sys.exit("--all lists every vendor. On a free account that burns all 5 monthly slots on the biggest vendors.\n"
                                                  "Name the vendors with --vendors, or add --full-listing-tier if your account has that access.")
    if not (a.list or a.all or a.vendors): sys.exit("nothing to do: use --list, --vendors <slug...> or --all --full-listing-tier")
    out = Path(a.out); arch = out / "eolnetwork"; arch.mkdir(parents=True, exist_ok=True)
    mpath, vpath = arch / "manifest.json", out / "vendor_lifecycle_eolnetwork.json"
    manifest = json.load(open(mpath)) if mpath.exists() else {"source": "eol.network (derived from vendor publications; not authoritative)", "vendors": {}}
    api = Api(key, a.contact, a.rate)
    try: vlist = api.get("vendors").get("data") or []
    except (StopRun, VendorError) as e: sys.exit(f"cannot read the vendor list: {e}")
    by = {v["slug"]: v for v in vlist if isinstance(v, dict) and v.get("slug")}
    if not by: sys.exit("the vendor list came back empty or in an unexpected shape")
    print(f"eol.network: {len(by)} vendors, {sum(v.get('count') or 0 for v in by.values()):,} SKUs listed")
    if a.list:
        for s, v in sorted(by.items(), key=lambda kv: -(kv[1].get("count") or 0)):
            m = manifest["vendors"].get(s); st = "-" if not m else ("up to date" if m["generated_at"] == v.get("generated_at") else "changed upstream")
            print(f"  {s:28s}{v.get('count') or 0:8,d}  generated {str(v.get('generated_at'))[:10]}  local: {st}")
        return
    todo = sorted(by, key=lambda s: -(by[s].get("count") or 0)) if a.all else a.vendors
    bad = [s for s in todo if s not in by]
    if bad: sys.exit("unknown vendor slug(s): " + "; ".join(f"{s} (did you mean {', '.join(difflib.get_close_matches(s, by, 3)) or 'none'}?)" for s in bad))
    failed = []
    for s in todo:
        v, m = by[s], manifest["vendors"].get(s)
        if m and not a.force and m.get("generated_at") == v.get("generated_at") and (arch / f"{s}.ndjson.gz").exists():
            print(f"{s}: unchanged since {m['pulled_utc'][:10]}, skipped"); continue
        print(f"{s}: pulling ~{v.get('count') or '?'} SKUs" + ("" if m else "  (first listing: uses a free-tier vendor slot)"), flush=True)
        try: rows = pull_vendor(api, s, a.per_page, v.get("count"))
        except VendorError as e: print(f"{s}: FAILED, nothing merged: {e}", file=sys.stderr); failed.append(s); continue
        except StopRun as e: print(f"STOPPED at {s}: {e}", file=sys.stderr); failed.append(s); break
        digest, size = write_raw(arch / f"{s}.ndjson.gz", rows); recs = to_records(s, rows); merge(vpath, s, recs)
        manifest["vendors"][s] = dict(rows=len(rows), unique_skus=sum(r["sku_count"] for r in recs), records=len(recs), generated_at=v.get("generated_at"),
                                      pulled_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"), sha256=digest, bytes=size)
        with open(mpath, "w") as fh: json.dump(manifest, fh, indent=1)
        print(f"{s}: {len(rows):,} rows -> {len(recs):,} records" + (f" | listing slots {api.slots}" if api.slots else "") + (f" | rows left today {api.remaining_day:,}" if api.remaining_day is not None else ""))
    print("done:", ", ".join(f"{s} ok" for s in todo if s not in failed) or "nothing pulled", "|", f"FAILED: {', '.join(failed)}" if failed else "no failures")
    if failed: sys.exit(1)

if __name__ == "__main__": main()
