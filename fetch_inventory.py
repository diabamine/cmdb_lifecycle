"""Build a REAL-WORLD software inventory (no synthetic noise) from public catalogues.
Needs internet, run once. Output: raw/inventory.csv  (vendor, product, version, fw_or_pkg ...)
  - winget  : Windows apps  (publisher.id, display name, latest version)  ~16k
  - Homebrew: macOS casks   (app name, version; no vendor -> real "missing vendor" case) ~7k
  - Ubuntu  : apt packages  (package, maintainer, epoch/ubuntu-style versions) ~6k
"""
import csv, gzip, io, json, re, sqlite3, urllib.request, zipfile
UA = {"User-Agent": "cmdb-lifecycle-inventory/0.1"}
get = lambda u: urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=120).read()
rows = []   # (source, ci_type, vendor, product, version, package)

# winget: the index is a sqlite db inside an .msix (zip)
z = zipfile.ZipFile(io.BytesIO(get("https://cdn.winget.microsoft.com/cache/source2.msix")))
open("raw/winget_index.db", "wb").write(z.read("Public/index.db"))
for pid, name, ver in sqlite3.connect("raw/winget_index.db").execute("select id,name,latest_version from packages"):
    rows.append(("winget", "software", pid.split(".")[0], name, ver or "", ""))

# Homebrew casks: JSON list, 'name' is a list of display names
for c in json.loads(get("https://formulae.brew.sh/api/cask.json")):
    rows.append(("homebrew", "software", "", (c["name"] or [c["token"]])[0], str(c.get("version") or ""), ""))

# Ubuntu main: 'Packages' stanzas separated by blank lines
for stanza in gzip.decompress(get("http://archive.ubuntu.com/ubuntu/dists/jammy/main/binary-amd64/Packages.gz")).decode().split("\n\n"):
    f = dict(l.split(": ", 1) for l in stanza.splitlines() if re.match(r"^[A-Za-z-]+: ", l))
    if "Package" in f:
        rows.append(("ubuntu", "software", re.sub(r"\s*<.*>", "", f.get("Maintainer", "")), f["Package"], f.get("Version", ""), f"{f['Package']}-{f.get('Version', '')}"))

with open("raw/inventory.csv", "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["asset_id", "source", "ci_type", "vendor", "product", "version", "fw_or_pkg"])
    for i, r in enumerate(rows): w.writerow([f"R{i:06d}", r[0], *r[1:]])
print(len(rows), "rows -> raw/inventory.csv")
