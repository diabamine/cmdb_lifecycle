"""eol.network API: bulk hardware lifecycle data (needs a free account + API key).

  EOL_NETWORK_API_KEY=eol_...          in ./.env (gitignored) or exported; never on the command line
  EOL_NETWORK_CONTACT=https://your.site-or-mail    same place; goes into the User-Agent, keyed calls without one are rejected

Why it is built this way (eol.network docs, terms and acceptable-use policy):
  * ONE paged listing per vendor, never per-SKU calls: 500 rows per call, ~30 calls/min (limit is 60).
  * Free accounts get full listings for 5 vendors per rolling 30 days; a vendor's slot starts at its first listing call.
    Re-listing the same vendor costs no new slot, so fetch() only re-pulls vendors already in the local manifest unless
    vendor slugs are passed explicitly (first listing of a new vendor uses a slot).
  * Pull once, store, diff: a vendor is re-pulled only when its generated_at changed.
  * Raw archive is kept local and gitignored (the terms forbid republishing and each key gets watermark records):
        snapshot/eolnetwork/<vendor>.ndjson.gz   raw rows exactly as received
        snapshot/eolnetwork/manifest.json        per vendor: rows, generated_at, sha256, pulled time
        snapshot/vendor_lifecycle_eolnetwork.json  vendor-feed records, the input of standardize()
  * SKUs with identical series + dates are grouped into ONE record (SKUs become aliases), like the Juniper feed.
Attribution: eol.network, derived from vendor publications. Not authoritative: verify dates with the vendor before acting.
"""
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cmdb.ingest.common import CollectorError, day, read_json_list, vendor_rows, write_atomic

# overridable for tests only
BASE = os.environ.get("EOL_NETWORK_BASE", "https://eol.network").rstrip("/")
SOURCE = "eol.network"
FEED_FILE = "vendor_lifecycle_eolnetwork.json"
PER_PAGE = 500
REQUESTS_PER_MIN = 30


class StopRun(RuntimeError):
    """Account-level problem (key, access, quota, vendor-slot limit): continuing would only make it worse."""


class VendorError(RuntimeError):
    """This vendor could not be pulled completely; nothing of it is merged. Other vendors may continue."""


def load_dotenv(path=".env"):
    """Read EOL_NETWORK_* lines from ./.env into the environment (real environment variables win). Values are never printed."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?(EOL_NETWORK_[A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if not match or match.group(1) in os.environ:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ[match.group(1)] = value


class Api:
    def __init__(self, key, contact, rate_per_min):
        self.key = key
        self.user_agent = f"cmdb-lifecycle-ingest/0.1 (+{contact})"
        self.gap = 60.0 / rate_per_min
        self.last_call = 0.0
        self.remaining_day = None
        self.slots = None

    def get(self, path, params=None, tries=5):
        url = f"{BASE}/api/v1/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(tries):
            time.sleep(max(0.0, self.last_call + self.gap - time.monotonic()))
            self.last_call = time.monotonic()
            headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json",
                       "User-Agent": self.user_agent}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
                    self.read_quota_headers(response.headers)
                    return json.load(response)
            except urllib.error.HTTPError as err:
                self.handle_http_error(err, path, attempt, tries)
            except (urllib.error.URLError, TimeoutError, ValueError) as err:
                if attempt < tries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise VendorError(f"{path}: {err}") from err

    def read_quota_headers(self, headers):
        if headers.get("X-RateLimit-Remaining-Day", "").isdigit():
            self.remaining_day = int(headers["X-RateLimit-Remaining-Day"])
        if headers.get("X-Listing-Vendors-Used"):
            self.slots = f"{headers['X-Listing-Vendors-Used']}/{headers.get('X-Listing-Vendors-Limit', '?')}"

    def handle_http_error(self, err, path, attempt, tries):
        """Sleep and return to retry; raise StopRun / VendorError when retrying cannot help."""
        try:
            body = json.load(err).get("error", {})
        except Exception:
            body = {}
        code, message = body.get("code", ""), body.get("message", "")
        retry_after = err.headers.get("Retry-After", "60") or "60"
        retry = int(retry_after) if retry_after.isdigit() else 60
        last_try = attempt >= tries - 1
        if err.code == 429 and code == "rate_limit_minute" and not last_try:
            print(f"  rate limit, waiting {retry}s", file=sys.stderr)
            time.sleep(retry)
            return
        if err.code == 503 and not last_try:
            time.sleep(min(retry, 120))
            return
        if err.code == 429 and code == "quota_exhausted":
            raise StopRun("daily row quota used up (resets 00:00 UTC)") from err
        if err.code == 429 and code == "listing_vendor_limit":
            raise StopRun(f"free-tier vendor listing limit reached: {message}") from err
        if err.code in (401, 403) or code == "bad_user_agent":
            raise StopRun(f"HTTP {err.code} {code}: {message or 'check the key, email verification and account approval'}") from err
        if err.code == 404:
            raise VendorError(f"{code or 'not found'}: {path}") from err
        if err.code >= 500 and not last_try:
            time.sleep(5 * (attempt + 1))
            return
        raise VendorError(f"HTTP {err.code} {code} on {path}") from err


def pull_vendor(api, slug, expected_count):
    rows, page, total = [], 1, None
    while True:
        if api.remaining_day is not None and api.remaining_day < PER_PAGE:
            raise StopRun(f"only {api.remaining_day} rows left in today's quota")
        reply = api.get(f"vendors/{slug}/products", {"page": page, "per_page": PER_PAGE})
        data, meta = reply.get("data"), reply.get("meta") or {}
        if not isinstance(data, list):
            raise VendorError(f"{slug}: unexpected response shape on page {page}")
        total = meta.get("total", total)
        rows += data
        if page % 10 == 0:
            print(f"  {slug}: page {page}, {len(rows):,}/{total if total is not None else '?'} rows", flush=True)
        if not data or (total is not None and (len(rows) >= total or page > math.ceil(total / PER_PAGE) + 1)):
            break
        page += 1
    # a partial listing must never replace a complete one
    if total is not None and len(rows) != total:
        raise VendorError(f"{slug}: received {len(rows)} rows but meta.total says {total}; not merged")
    if expected_count and abs(len(rows) - expected_count) > max(5, 0.02 * expected_count):
        print(f"  ! {slug}: {len(rows):,} rows vs {expected_count:,} in the vendor list (>2% apart)", file=sys.stderr)
    return rows


def write_raw(path: Path, rows):
    """Gzipped ndjson with mtime=0 so identical data gives an identical sha256."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz, io.TextIOWrapper(gz, encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    content = buffer.getvalue()
    write_atomic(path, content)
    return hashlib.sha256(content).hexdigest(), len(content)


def to_records(slug, rows):
    """Raw rows -> vendor-feed records; SKUs with the same series, category, status and dates collapse into one record."""
    groups, seen = defaultdict(list), set()
    for r in rows:
        pid = str(r.get("product_id") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        series = r.get("applicable_series")
        if isinstance(series, list):
            series = series[0] if series else ""
        elif not isinstance(series, str):
            series = ""
        end_sale, end_support = day(r.get("end_of_sale_date")), day(r.get("last_date_of_support"))
        # the real field is end_of_sw_maintenance_date; the docs call it end_of_software_support_date
        sw_maintenance = day(r.get("end_of_sw_maintenance_date") or r.get("end_of_software_support_date"))
        product_type, raw_category = str(r.get("product_type") or "").lower(), str(r.get("category") or "").lower()
        if raw_category in ("hardware", "software", "service", "unknown"):
            category = raw_category
        else:
            category = "license" if "licen" in product_type else "hardware"
        ended_without_date = str(r.get("lifecycle_status") or "").lower() == "end_of_life" and not end_support
        status = "obsolete" if ended_without_date else ""
        groups[(series, category, status, end_sale, sw_maintenance, end_support)].append((pid, r))

    out, labels = [], set()
    for (series, category, status, end_sale, sw_maintenance, end_support), group in groups.items():
        group.sort(key=lambda item: item[0])
        first_pid, first = group[0]
        name = str(first.get("product_name") or first_pid).strip()
        # a group is named after its series and dates, not after one arbitrary member SKU
        if len(group) == 1:
            label = name
        else:
            label = f"{series or name} (EOS {end_sale or 'n/a'}, EOL {end_support or 'n/a'}; {len(group)} SKUs)"
        if label in labels:
            label += f" [{first_pid}]"
        labels.add(label)
        replacements = sorted({str(x.get("replacement_product")) for _, x in group if x.get("replacement_product")})
        source = first.get("source")
        vendor_doc = source.get("url") if isinstance(source, dict) else ""          # the vendor's own publication, if given
        if first.get("series_slug"):
            page = f"{BASE}/{slug}/{first['series_slug']}/{urllib.parse.quote(first_pid, safe='/=')}"
        else:
            page = f"{BASE}/{slug}"
        record = dict(vendor=slug, product=label, label_alias=list(dict.fromkeys([p for p, _ in group] + [name])),
                      cycle=label, release_date="", end_sale=end_sale, end_support_full=sw_maintenance, eol=end_support,
                      end_security_support="", end_extended="", status=status, category=category,
                      source=f"{SOURCE}/{slug}", url=vendor_doc or page, sku_count=len(group))
        if replacements:
            record["replacement"] = replacements[0] if len(replacements) == 1 else replacements
        out.append(record)
    return out


def merge(path: Path, slug, records):
    kept = [r for r in read_json_list(path) if r.get("vendor") != slug]
    write_atomic(path, json.dumps(kept + records, indent=0).encode())


def fetch(raw_dir: Path, vendors=None) -> None:
    """Pull vendor listings. Without `vendors`: those already in the local manifest (no new listing slot used).

    Skipped without credentials. Raises CollectorError at the end if any vendor failed; the others stay merged.
    """
    load_dotenv()
    key, contact = os.environ.get("EOL_NETWORK_API_KEY"), os.environ.get("EOL_NETWORK_CONTACT")
    if not key:
        print("eol.network: skipped (set EOL_NETWORK_API_KEY in .env: register at eol.network, verify email, mint a key)")
        return
    if not contact:
        # eol.network rejects keyed calls without a contact in the User-Agent
        print("eol.network: skipped (set EOL_NETWORK_CONTACT: a URL or address for the User-Agent)")
        return
    archive = raw_dir / "eolnetwork"
    manifest_path = archive / "manifest.json"
    feed_path = raw_dir / FEED_FILE
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"source": "eol.network (derived from vendor publications; not authoritative)", "vendors": {}}
    todo = list(vendors or manifest["vendors"])
    if not todo:
        print("eol.network: no vendors pulled yet; pass explicit slugs (--eol-vendors cisco aruba), each uses a free-tier slot")
        return

    api = Api(key, contact, REQUESTS_PER_MIN)
    try:
        listing = api.get("vendors").get("data") or []
    except (StopRun, VendorError) as err:
        raise CollectorError(f"eol.network: cannot read the vendor list: {err}") from err
    by_slug = {v["slug"]: v for v in listing if isinstance(v, dict) and v.get("slug")}
    unknown = [s for s in todo if s not in by_slug]
    if unknown:
        raise CollectorError(f"eol.network: unknown vendor slug(s): {', '.join(unknown)}")

    failed = []
    for slug in todo:
        vendor, pulled = by_slug[slug], manifest["vendors"].get(slug)
        if pulled and pulled.get("generated_at") == vendor.get("generated_at") and (archive / f"{slug}.ndjson.gz").exists():
            print(f"{slug}: unchanged since {pulled['pulled_utc'][:10]}, skipped")
            continue
        print(f"{slug}: pulling ~{vendor.get('count') or '?'} SKUs" + ("" if pulled else "  (first listing: uses a free-tier vendor slot)"), flush=True)
        try:
            rows = pull_vendor(api, slug, vendor.get("count"))
        except VendorError as err:
            print(f"{slug}: FAILED, nothing merged: {err}", file=sys.stderr)
            failed.append(slug)
            continue
        except StopRun as err:
            print(f"STOPPED at {slug}: {err}", file=sys.stderr)
            failed.append(slug)
            break
        digest, size = write_raw(archive / f"{slug}.ndjson.gz", rows)
        records = to_records(slug, rows)
        merge(feed_path, slug, records)
        manifest["vendors"][slug] = dict(
            rows=len(rows), unique_skus=sum(r["sku_count"] for r in records), records=len(records),
            generated_at=vendor.get("generated_at"),
            pulled_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"), sha256=digest, bytes=size)
        write_atomic(manifest_path, json.dumps(manifest, indent=1).encode())
        print(f"{slug}: {len(rows):,} rows -> {len(records):,} records")
    if failed:
        raise CollectorError(f"eol.network: failed vendors: {', '.join(failed)}")


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(read_json_list(raw_dir / FEED_FILE), source=SOURCE)
