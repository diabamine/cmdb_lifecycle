"""Cisco: official Support API "EoX v5", EOXByDates over ONE wide announcement window = the whole catalogue.

1,000 records per page (CISCO_WINDOW=year: one query per year instead, in case the API caps wide results).
Auth is OAuth2 client credentials (token lasts 1 h, refreshed automatically). A free Cisco.com account is NOT enough:
the Cisco ID must be enabled for the Support APIs (Smart Net Total Care / Partner Support Service), otherwise calls fail
with "HTTP 403 Developer Inactive" and the run stops without touching old Cisco data.
Field mapping (OpenEoX name): EndOfSaleDate -> end_sale, EndOfSWMaintenanceReleases -> end_support_full,
LastDateOfSupport -> eol. EndOfSecurityVulSupportDate is stored in the raw feed but has no standard column.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from cmdb.ingest.common import (BROWSER_UA, CollectorError, iso, feed_records, rec, replace_vendor_records,
                                vendor_rows)

VENDOR = "cisco"
# eol.network also has a "cisco" vendor, but in its own raw file and with source "eol.network/cisco".


class CiscoEox:
    """Minimal EoX v5 client: token refresh before expiry or after a 401, retries, explicit errors."""
    BASE = "https://apix.cisco.com/supporttools/eox/rest/5"

    def __init__(self, client_id, client_secret):
        self.client_id, self.client_secret = client_id, client_secret
        self.token, self.expires = None, 0.0

    def refresh(self):
        url = os.environ.get("CISCO_TOKEN_URL", "https://id.cisco.com/oauth2/default/v1/token")
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        headers = {"User-Agent": BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=60) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as err:
            raise CollectorError(f"cisco: token request failed: HTTP {err.code} "
                                 "(check CISCO_CLIENT_ID / CISCO_CLIENT_SECRET / CISCO_TOKEN_URL)") from err
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            raise CollectorError(f"cisco: token request failed: {err}") from err
        if "access_token" not in reply:
            raise CollectorError("cisco: token response has no access_token")
        self.token = reply["access_token"]
        self.expires = time.time() + int(reply.get("expires_in", 3600))

    def call(self, path, tries=4):
        refreshed_on_401 = False
        short_path = path.split("?")[0]
        for attempt in range(tries):
            if not self.token or time.time() > self.expires - 120:
                self.refresh()
            headers = {"User-Agent": BROWSER_UA, "Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            request = urllib.request.Request(f"{self.BASE}/{path}", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.load(response)
            except urllib.error.HTTPError as err:
                if err.code == 401 and not refreshed_on_401:
                    refreshed_on_401 = True
                    self.token = None
                    continue
                if err.code in (401, 403):
                    raise CollectorError(
                        f"cisco: HTTP {err.code} on {short_path}: the Cisco ID is not enabled for the EoX Support API "
                        "(needs Smart Net Total Care or Partner Support Service)") from err
                if err.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise CollectorError(f"cisco: HTTP {err.code} on {short_path}") from err
            except (urllib.error.URLError, TimeoutError, ValueError) as err:     # network error or non-JSON body
                if attempt < tries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise CollectorError(f"cisco: {short_path}: {err}") from err


def fetch(raw_dir: Path) -> None:
    client_id, secret = os.environ.get("CISCO_CLIENT_ID"), os.environ.get("CISCO_CLIENT_SECRET")
    if not (client_id and secret):
        print("cisco: skipped (set CISCO_CLIENT_ID / CISCO_CLIENT_SECRET from the Cisco API Console)")
        return
    records = collect(CiscoEox(client_id, secret))
    replace_vendor_records(raw_dir, VENDOR, records)


def collect(api: CiscoEox) -> list[dict]:
    def date_of(field):
        # {"value": "2019-05-01", "dateFormat": "YYYY-MM-DD"}
        return iso(field.get("value", "")) if isinstance(field, dict) else ""

    last_year = datetime.now().year + 5                     # announcements up to 5 years ahead
    if os.environ.get("CISCO_WINDOW") == "year":
        windows = [(f"{y}-01-01", f"{y}-12-31") for y in range(2000, last_year + 1)]
    else:
        windows = [("2000-01-01", f"{last_year}-12-31")]
    out, seen = [], set()
    for start, end in windows:
        page = 1
        while True:
            reply = api.call(f"EOXByDates/{page}/{start}/{end}?responseencoding=json&eoxAttrib=EO_EXTERNAL_ANNOUNCE_DATE")
            for e in reply.get("EOXRecord", []):
                pid = e.get("EOLProductID")
                # a window with no data returns a single EOXError record
                if "EOXError" in e or not pid or pid in seen:
                    continue
                seen.add(pid)
                description = e.get("ProductIDDescription")
                out.append(rec(VENDOR, pid, pid, "cisco EoX v5", e.get("LinkToProductBulletinURL", ""), "hardware",
                               aliases=[description] if description else [],
                               release=date_of(e.get("EOXExternalAnnouncementDate")),
                               end_sale=date_of(e.get("EndOfSaleDate")),
                               end_full=date_of(e.get("EndOfSWMaintenanceReleases")),
                               eol=date_of(e.get("LastDateOfSupport")),
                               end_security=date_of(e.get("EndOfSecurityVulSupportDate"))))
            last_page = int((reply.get("PaginationResponseRecord") or {}).get("LastIndex", 1) or 1)
            if page >= last_page:
                break
            page += 1
            time.sleep(0.3)
            if page % 10 == 0:
                print(f"  cisco {start}..{end}: page {page}/{last_page}, {len(out)} PIDs so far", flush=True)
    return out


def standardize(raw_dir: Path) -> list[dict]:
    return vendor_rows(feed_records(raw_dir, VENDOR), source=VENDOR)
