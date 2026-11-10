#!/usr/bin/env python3
"""OFFLINE CMDB verification/enrichment against a snapshot made by fetch_snapshot.py.
No network. Matching cascade: normalize -> exact/alias -> fuzzy(+vendor signal) -> version->cycle,
then EOL/EOS + CISA-KEV exposure + EPSS. Every row gets method/confidence/review flag.

  .venv/bin/python -I cmdb_enrich.py --snapshot snapshot --cmdb raw/inventory.csv [--truth t.csv] --out out_real
Needs snapshot/nvd.sqlite (fetch_snapshot.py --nvd, then build_nvd_index.py) for version-level NVD CVEs.
"""
import argparse, csv, gzip, json, re, sqlite3, unicodedata
from collections import defaultdict, Counter
from datetime import date, datetime
from pathlib import Path
from rapidfuzz import fuzz, process

AUTO, REVIEW = 90, 85   # score thresholds (0-100): >=AUTO auto-accept, >=REVIEW candidate for human review (raised after real-data check)
CORP = {"inc", "corp", "corporation", "ltd", "llc", "bv", "nv", "foundation", "the", "software", "systems",
        "company", "co", "gmbh", "sa", "by", "group", "global", "development", "project", "organization", "international",
        "networks", "technologies", "technology", "incorporated", "limited", "plc", "ag"}
NOISE_PHRASES = [r"\(x64\)", r"\bx64\b", r"\b64 ?bit\b", r"\benterprise edition\b", r"\bstandard\b", r"\blts\b",
                 r"\bclient\b", r"\bruntime\b", r"\bedition\b", r"\bdatacenter\b", r"\bprofessional plus\b",
                 r"\bprofessional\b", r"\bappliance\b", r"\benterprise$", r"\besr\b", r"\bbeta\b", r"\bnightly\b",
                 r"\bdeveloper edition\b", r"\bmsix\b"]
VENDOR_SYN = {"msft": "microsoft", "rh": "redhat", "orcl": "oracle", "pgdg": "postgresql", "asf": "apache",
              "hpe": "hewlett packard enterprise", "hp": "hewlett packard"}

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", s)).strip()
def norm_vendor(v):
    t = [VENDOR_SYN.get(w, w) for w in norm(v).replace(".", " ").split() if w not in CORP]
    return " ".join(t).replace("red hat", "redhat").replace(" ", "") if t else ""
def vend_ok(a, b):
    """Do two normalized vendor keys denote the same vendor? (equal, prefix of each other, or very similar)"""
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a))) or fuzz.ratio(a, b) >= 85)
def digit_tokens(x): return {w for w in x.split() if any(ch.isdigit() for ch in w)}
def strip_noise(s):
    for p in NOISE_PHRASES: s = re.sub(p, " ", s)
    return re.sub(r"\s+", " ", s).strip()
def pdate(v):
    return datetime.strptime(v[:10], "%Y-%m-%d").date() if isinstance(v, str) and v[:4].isdigit() else None

# ------------------------------------------------------------------ reference build
class Ref:
    def __init__(self, snap: Path, aliases="aliases.csv"):
        self.prods = {}            # id -> product dict
        self.index = defaultdict(set)   # normalized name -> product ids
        self._load_endoflife(snap / "endoflife_full.json")
        self._load_redhat(snap / "redhat_lifecycle.json")
        self._load_vendors(snap / "vendor_lifecycle.json")
        self.keys = list(self.index)
        self._load_kev(snap / "cisa_kev.json", snap / "epss_scores.csv.gz")
        self._load_nvd(snap / "nvd.sqlite")
        self._load_aliases(aliases)

    def _add(self, p, names):
        self.prods[p["id"]] = p
        for n in names:
            k = norm(n)
            if k: self.index[k].add(p["id"])
    def _load_endoflife(self, f):
        for x in json.load(open(f))["result"]:
            vend, cpes = "", []
            for i in x.get("identifiers", []):
                if i["type"] == "cpe" and i["id"].startswith("cpe:2.3"): vend = i["id"].split(":")[3]; cpes.append(tuple(i["id"].split(":")[3:5]))
            rel = [dict(cycle=r["name"], release_date=pdate(r.get("releaseDate")), eoas=pdate(r.get("eoasFrom")),
                        eol=pdate(r.get("eolFrom")), eoes=pdate(r.get("eoesFrom")), is_eol=r.get("isEol"),
                        maintained=r.get("isMaintained"), latest=(r.get("latest") or {}).get("name"), lts=r.get("isLts"), end_sale=None)
                   for r in x["releases"]]
            p = dict(id="eol:" + x["name"], source="endoflife.date", name=x["name"], label=x["label"], category=x["category"],
                     vendor=vend, cpes=cpes, releases=rel, kev=[], url=x["links"]["html"])
            names = [x["name"].replace("-", " "), x["label"], *x.get("aliases", [])]
            for i in x.get("identifiers", []):          # purl / cpe product names are extra aliases
                if i["type"] == "purl": names.append(i["id"].split("/")[-1].replace("-", " "))
                if i["type"] == "cpe" and i["id"].startswith("cpe:2.3"): names.append(i["id"].split(":")[4].replace("_", " "))
            if vend: names += [f"{vend} {x['name'].replace('-', ' ')}"]
            self._add(p, names)
    def _load_redhat(self, f):
        for pr in json.load(open(f))["data"]:
            pt = {a["name"]: a["ptype"] for a in pr["all_phases"]}; rel = []
            for v in pr["versions"]:
                ph = v.get("phases", []); ga = next((q for q in ph if "general availability" in q["name"].lower()), None)
                nor = [q for q in ph if pt.get(q["name"]) == "normal" and q is not ga]; ext = [q for q in ph if pt.get(q["name"]) == "extended"]
                rel.append(dict(cycle=str(v["name"]), release_date=pdate(ga["end_date"]) if ga else None,
                                eoas=pdate(nor[0]["end_date"]) if len(nor) > 1 else None, eol=pdate(nor[-1]["end_date"]) if nor else None,
                                eoes=max((d for d in (pdate(q["end_date"]) for q in ext) if d), default=None), is_eol=None,
                                maintained=None, latest=v.get("final_minor_release") or None, lts=None, end_sale=None))
            p = dict(id="rh:" + pr["name"], source="redhat", name=pr["name"], label=pr["name"], category="redhat-product",
                     vendor="redhat", cpes=[], releases=rel, kev=[], url=pr.get("link") or "")
            self._add(p, [pr["name"], *pr.get("former_names", [])])
    def _load_vendors(self, f):
        """Vendor feeds from fetch_vendors.py (Microsoft, Juniper, Cisco, VMware, manual csv): one product per (vendor, product)."""
        if not Path(f).exists(): return
        groups = defaultdict(list)
        for r in json.load(open(f)): groups[(r["vendor"], r["product"])].append(r)
        for (vendor, prod), recs in groups.items():
            # paid Extended Security Update rows are not regular cycles: keep them as the product's "extended" end date
            esu = [pdate(r["eol"]) for r in recs if re.search(r"extended security|\besu\b", r["cycle"], re.I)]
            recs = [r for r in recs if not re.search(r"extended security|\besu\b", r["cycle"], re.I)] or recs
            rel = [dict(cycle=r["cycle"], release_date=pdate(r["release_date"]), eoas=pdate(r["end_support_full"]), eol=pdate(r["eol"]), eoes=max((d for d in esu if d), default=None),
                        is_eol=None, maintained=None, latest=None, lts=None, end_sale=pdate(r["end_sale"])) for r in recs]
            p = dict(id=f"v:{vendor}:{prod}", source=vendor, name=prod, label=prod, category=recs[0]["category"] or "vendor-feed",
                     vendor=norm_vendor(vendor), cpes=[], releases=rel, kev=[], url=recs[0]["url"])
            names = [prod, *(a for r in recs for a in r["label_alias"])]
            names += [f"{vendor} {prod}", prod.removeprefix("Microsoft ")]                    # "VMware ESXi", "Office 2016" == "Microsoft Office 2016"
            if vendor == "microsoft": names.append("Microsoft " + prod.removeprefix("Microsoft "))
            self._add(p, names)
    def _load_kev(self, kev, epss):
        sc = {}
        with gzip.open(epss, "rt") as fh:
            for line in fh:
                if line.startswith(("#", "cve")): continue
                c, e, _ = line.strip().split(","); sc[c] = float(e)
        for v in json.load(open(kev))["vulnerabilities"]:
            q = norm(v["product"]); vend = norm_vendor(v["vendorProject"])
            cands = list(self.index.get(q, [])) or [sorted(self.index[k])[0] for k, s, _ in
                    process.extract(norm(v["vendorProject"] + " " + v["product"]), self.keys, scorer=fuzz.token_set_ratio, limit=1, score_cutoff=95)]
            for pid in cands:
                p = self.prods[pid]
                if p["vendor"] and norm_vendor(p["vendor"]) != vend and fuzz.ratio(norm_vendor(p["vendor"]), vend) < 80: continue
                p["kev"].append(dict(cve=v["cveID"], added=pdate(v["dateAdded"]), ransom=v["knownRansomwareCampaignUse"] == "Known", epss=sc.get(v["cveID"], 0.0)))
    def _load_aliases(self, f):
        """Human decisions from the review loop: (vendor key, product key) -> product id, or "-" (= confirmed: not a catalogue product)."""
        self.aliases = {}
        if Path(f).exists():
            for a in csv.DictReader(open(f, encoding="utf-8")): self.aliases[(norm_vendor(a["vendor"]), norm(a["product"]))] = a["target_id"]
    def _load_nvd(self, f):
        """Open the NVD index; self.nvd_names: normalized product name -> [(vendor, product, n_cve)]."""
        self.nvd = sqlite3.connect(f, check_same_thread=False) if Path(f).exists() else None
        self.nvd_names = defaultdict(list)
        self.nvd_cache = {}            # (vendor:product, version) -> CVE lists, avoids repeated SQL
        if self.nvd:
            for v, pr, n in self.nvd.execute("select vendor, product, sum(n_cve) from products group by vendor, product"):
                self.nvd_names[norm(pr.replace("_", " "))].append((v, pr, n))

# ------------------------------------------------------------------ matching
# Every derived spelling is tagged with WHY it exists, so a result says how it matched: raw | cleaned | rule | rollup
LOCALE_RE = re.compile(r"\s*\((?:[a-z]{2,3}(?:-[A-Za-z0-9]{2,4})?|msix|x86|x64|arm64)\)\s*$", re.I)       # "(hu)", "(es-MX)", "(MSIX)"
VENDOR_PREFIXES = ("microsoft", "mozilla", "oracle", "google", "adobe", "apple", "vmware", "red hat", "jetbrains", "apache", "ibm", "cisco", "juniper", "citrix")
VENDOR_RULES = [      # vendor naming habits: (regex on the normalized name, replacement)
    (r"^(?:microsoft )?(?:asp\.net core|\.net(?: core)?)(?: windows desktop| desktop)? (?:runtime|sdk|hosting bundle)\b.*", ".net"),
    (r"^(?:microsoft )?visual studio (?:build ?tools|community|professional|enterprise) (\d{4}).*", r"visual studio \1"),
    (r"^java (?:tm )?se (?:development kit|runtime environment)\b.*", "oracle jdk"),
    (r"^(?:mozilla )?(firefox|thunderbird)\b.*", r"\1"),
]
ROLLUP_RULES = [      # sub-package -> parent product: (regex on the lower-case package name, upstream product)
    (r"^(?:g\+\+|gcc|cpp|gfortran)(?:-.*)?$", "gcc"), (r"^linux-(?:image|headers|modules|tools|ibm|aws|azure|gcp|oem|generic|source|libc-dev).*$", "linux kernel"),
    (r"^libc6.*$|^libc-.*$|^libc\d.*$", "glibc"), (r"^(?:libssl|libcrypto|openssl).*$", "openssl"), (r"^libcurl.*$", "curl"),
    (r"^(?:libpq|postgresql).*$", "postgresql"), (r"^libsqlite3.*$", "sqlite"), (r"^(?:libpython3(?:\.\d+)?.*|python3(?:\.\d+)?(?:-(?:minimal|dev|doc|venv|distutils|lib2to3|tk|gdbm))?)$", "python"),   # python3-colorama is a module, not Python
    (r"^openjdk-\d+.*$", "openjdk"), (r"^(?:mysql|mariadb)-.*$", "mysql"), (r"^libxml2.*$", "libxml2"), (r"^libgnutls.*$", "gnutls"),
]
LANG_PREFIX = re.compile(r"^(?:python3?|ruby|golang|node|php|lua|perl)-")      # language binding: named after the upstream module
PKG_SUFFIX = re.compile(r"-(?:dev|doc|docs|data|common|dbg|dbgsym|bin|utils|tools|cli|static|headers|tests?|examples|l10n|plugins?|server|client|libs?|core|base)$")
PKG_VERSION = re.compile(r"(?:-\d+(?:\.\d+)*)+$")                                  # trailing -14, -1.0-0
SONAME = re.compile(r"(?<=[a-z])\d+$")                                                # libtiff5 -> libtiff (only for lib* packages)

def rollup_names(pkg):
    """Debian/RPM sub-package name -> [(candidate upstream name, kind)]. kind rollup-lang = guessed from a python3-/ruby- binding."""
    out = [(up, "rollup") for rx, up in ROLLUP_RULES if re.match(rx, pkg)]
    x = LANG_PREFIX.sub("", pkg); lang = x != pkg
    for _ in range(3):                                      # peel suffixes and version tokens until stable
        y = PKG_VERSION.sub("", PKG_SUFFIX.sub("", x))
        if y.startswith("lib"): y = SONAME.sub("", y)
        if y == x or not y: break
        x = y
    c = norm(x.replace("-", " "))
    if x != pkg and len(c) >= 4: out.append((c, "rollup-lang" if lang else "rollup"))
    return out

def name_candidates_kind(r):
    """{candidate spelling: kind} in trial order."""
    out = {}
    def add(c, kind):
        if c and c not in out: out[c] = kind
    prod = r["product"]; add(norm(prod), "raw")
    base = norm(LOCALE_RE.sub("", prod))                    # "Mozilla Firefox (hu)" -> "mozilla firefox"
    names = [(base, "cleaned")] + [(base[len(p) + 1:], "vstrip") for p in (*VENDOR_PREFIXES, norm(r.get("vendor", ""))) if p and base.startswith(p + " ") and len(base) - len(p) - 1 >= 5]
    for n, kind in names:                                   # vstrip = vendor word removed ("mozilla firefox" -> "firefox"): exact lookup only, never fuzzy
        s = strip_noise(n); add(n, kind); add(s, kind)
        if s.endswith(" server"): add(s[:-7], kind)
        m = re.match(r"^(.*?)\s+v?\d.*$", s)               # version embedded in the name: "sql server 2019" -> "sql server"
        if m: add(m.group(1), kind)
    for n, _ in names:
        for rx, rep in VENDOR_RULES:
            if re.match(rx, n): add(re.sub(rx, rep, n).strip(), "rule")
    pk = r.get("fw_or_pkg") or ""
    if pk:                                                  # package-style row (Debian, RPM)
        add(norm(re.split(r"-\d", pk)[0].replace("-", " ")), "cleaned")
        for c, kind in rollup_names(prod.lower()): add(c, kind)
    return out
def name_candidates(r): return list(name_candidates_kind(r))

def match_product(ref, r, trace=None):
    # trace: optional dict the UI passes in to see each step of the cascade
    kinds = name_candidates_kind(r); cands = list(kinds)
    vk = norm_vendor(r["vendor"]); vk = "" if vk in GENERIC_VENDORS else vk        # "Ubuntu Developers" says nothing about the product
    if trace is not None: trace.update(candidates=cands, kinds=kinds, vendor_key=vk, alias=None, exact=None, fuzzy=[])
    al = ref.aliases.get((vk, norm(r["product"]))) or ref.aliases.get(("", norm(r["product"])))      # 0. decisions made by humans win
    if al == "-" or al in ref.prods:
        if trace is not None: trace["alias"] = al
        return (None, "alias-none", 100) if al == "-" else (al, "alias", 100)
    def pick(ids):
        ids = list(ids)
        if len(ids) > 1 and vk:
            good = [i for i in ids if norm_vendor(ref.prods[i]["vendor"]) == vk]
            ids = good or ids
        if len(ids) > 1:   # official vendor feed first, then Red Hat (for Red Hat vendor), then endoflife.date
            pref = lambda i: 0 if ref.prods[i]["source"] not in ("endoflife.date", "redhat") else 1 if ref.prods[i]["source"] == ("redhat" if vk == "redhat" else "endoflife.date") else 2
            ids.sort(key=lambda i: (pref(i), i))
        return ids[0]
    for c in cands:
        if c in ref.index:
            ids = ref.index[c]; pid = pick(ids); kind = kinds[c]
            if kind == "rollup-lang" and ref.prods[pid]["category"] in ("os", "lang", "device"): continue      # python3-debian is a module, not Debian
            if kind in ("vstrip", "rule", "rollup", "rollup-lang") and vk and ref.prods[pid]["vendor"] and not vend_ok(norm_vendor(ref.prods[pid]["vendor"]), vk): continue   # guessed spellings must not contradict the vendor (a packager like RubyInstallerTeam is fine for plain cleaned names)
            if trace is not None: trace["exact"] = (c, sorted(ids))
            conf = {"rollup": 92, "rollup-lang": 91, "rule": 95}.get(kind, 98 if len(ids) == 1 else 93)
            return pid, "exact" if kind == "raw" else "exact+" + kind.replace("-lang", "").replace("vstrip", "cleaned"), conf
    best = None
    for c in [c for c in cands if kinds[c] in ("raw", "cleaned")]:         # fuzzy never runs on rule/rollup guesses
        for k, sc, _ in process.extract(c, ref.keys, scorer=fuzz.token_sort_ratio, limit=3, score_cutoff=REVIEW):
            if digit_tokens(c) and digit_tokens(k) and digit_tokens(c) != digit_tokens(k): continue   # different model/version number
            pid = pick(ref.index[k]); p = ref.prods[pid]; bonus = 0
            if vk and p["vendor"]:
                bonus = 4 if vend_ok(norm_vendor(p["vendor"]), vk) else -6
            raw = sc
            if abs(len(k) - len(c)) > 6 and min(len(k), len(c)) < 8: sc -= 15   # short-token false friends
            elif min(len(k), len(c)) <= 6: sc -= 8                              # one letter apart is a different product at this length (sonar / sonarr)
            if trace is not None: trace["fuzzy"].append((k, p["label"], raw, bonus, sc + bonus))
            if not best or sc + bonus > best[1]: best = (pid, sc + bonus)
    if best and best[1] >= REVIEW: return best[0], "fuzzy", round(min(best[1], 97), 1)      # penalties can push a candidate back under the review line
    return None, "none", 0

VER_RE = re.compile(r"\d+(?:\.\d+)*")
def match_cycle(p, version, pkg):
    version = re.sub(r"^\d+:", "", version or "")   # drop deb/rpm epoch: 1:2.3 -> 2.3
    v = norm(version) or ""
    m = VER_RE.search(v) or (VER_RE.search(re.sub(r"^.*?-(?=\d)", "", pkg)) if pkg else None)
    flat = lambda x: re.sub(r"[\s-]", "", x.lower())
    cyc = {flat(c["cycle"]): c for c in p["releases"]}
    cyc.update({k[:-2]: c for k, c in list(cyc.items()) if k.endswith(".x")})      # '7.x' also answers to '7'

    if flat(v) in cyc: return cyc[flat(v)], "name"          # whole string is a cycle name ('2012 r2', '8.6')
    if not m: return None, "no-version"
    parts = m.group(0).split(".")
    for n in range(len(parts), 0, -1):
        c = cyc.get(".".join(parts[:n]))
        if c: return c, "exact" if n == len(parts) else "prefix"
    sub = [c for k, c in cyc.items() if k.startswith(parts[0] + ".") and c["release_date"]]
    if sub: return max(sub, key=lambda c: c["release_date"]), "major-ambiguous"   # '4' -> newest 4.x, flagged
    return None, "cycle-not-found"

# ------------------------------------------------------------------ NVD: full CVE base, version-level
GENERIC_VENDORS = {"ubuntudevelopers", "debianmaintainers", "internal", "acmeit", "unknown", "na"}   # carry no vendor signal

def vkey(v):
    """Comparable key for versions like 1.1.1k / 10.0.14393: numbers compare as numbers, letters as text."""
    k = [(0, int(t)) if t.isdigit() else (1, t) for t in re.findall(r"\d+|[a-z]+", v.lower())]
    while k and k[-1] == (0, 0): k.pop()          # 8.0 == 8
    return tuple(k)

def in_range(vk, ver, si, se, ei, ee):
    """Is version key vk inside one NVD affected entry (exact version or start/end bounds)?"""
    if ver == "-": return False                      # CPE "not applicable" version
    if ver: return vk == vkey(ver)
    if not (si or se or ei or ee): return True      # no bounds: every version is affected
    return not ((si and vk < vkey(si)) or (se and vk <= vkey(se)) or (ei and vk > vkey(ei)) or (ee and vk >= vkey(ee)))

def nvd_identify(ref, r, p, cands):
    """CMDB row -> NVD (vendor, product). 1) CPE ids of the matched lifecycle product, 2) exact name (+vendor)."""
    if not ref.nvd: return None, ""
    if p:
        for vp in p["cpes"]:
            if any(vp == h[:2] for h in ref.nvd_names.get(norm(vp[1].replace("_", " ")), [])): return vp, "from-lifecycle-product"
    vk = norm_vendor(r["vendor"]); vk = "" if vk in GENERIC_VENDORS else vk
    for c in dict.fromkeys(cands):
        hits = ref.nvd_names.get(c, [])
        if vk: hits = [h for h in hits if vend_ok(norm_vendor(h[0]), vk)]
        elif len(c) < 6: hits = []                               # no vendor: accept only a long name ...
        elif len(hits) > 1:                                      # ... and, if several vendors use it, the project's own vendor:product
            own = [h for h in hits if norm(h[0].replace("_", " ")) == c]; hits = own if len(own) == 1 else []
        if hits: return max(hits, key=lambda h: h[2])[:2], "name+vendor" if vk else "name-unique"
    return None, ""

def nvd_cves(ref, vp, version):
    """-> (all CVEs of the product, CVEs applicable to `version` or None when the version is unusable)."""
    key = (vp, version)
    if key not in ref.nvd_cache:
        rows = ref.nvd.execute("select a.cve,a.ver,a.si,a.se,a.ei,a.ee,c.cvss,c.kev,c.published,c.descr from affects a join cve c "
                               "on c.id=a.cve where a.vendor=? and a.product=?", vp).fetchall()
        allc = {r[0]: dict(cve=r[0], cvss=r[6] or 0, kev=bool(r[7]), published=r[8], descr=r[9]) for r in rows}
        m = re.search(r"\d+(?:\.\d+)*[a-z]?", re.sub(r"^\d+:", "", version or "").lower())
        if re.search(r"_(19|20)\d\d(_r2)?$", vp[1]): app = list(allc.values())     # product name already carries the version
        elif not m: app = None
        else:
            vk = vkey(m.group(0)); ids = {r[0] for r in rows if in_range(vk, *r[1:6])}
            app = [allc[i] for i in ids]
        ref.nvd_cache[key] = (list(allc.values()), app)
    return ref.nvd_cache[key]

def add_nvd(ref, o, r, p):
    vp, how = nvd_identify(ref, r, p, name_candidates(r))
    if not vp: return
    allc, app = nvd_cves(ref, vp, r["version"])
    o.update(nvd_cpe=f"{vp[0]}:{vp[1]}", nvd_match=how, nvd_cves_total=len(allc))
    if app is not None:
        o.update(nvd_cves_version=len(app), nvd_max_cvss=max((c["cvss"] for c in app), default=0),
                 nvd_critical=sum(c["cvss"] >= 9 for c in app), nvd_kev=sum(c["kev"] for c in app),
                 nvd_latest_cve=max((c["published"] for c in app), default=""))

def lifecycle(c, today):
    if c["eol"]:
        if c["eol"] < today: return "EXTENDED_SUPPORT_ONLY" if c["eoes"] and c["eoes"] >= today else "EOL"
        if c["eoas"] and c["eoas"] < today: return "MAINTENANCE_ONLY"
        return "SUPPORTED"
    if c["is_eol"]: return "EOL"
    if c["maintained"]: return "SUPPORTED"
    return "UNKNOWN"

def enrich(ref, rows, today):
    out = []
    for r in rows:
        o = dict(r); o.update(matched_product="", matched_source="", category="", canonical_vendor="", match_method="none", match_confidence=0,
                              review="", matched_cycle="", cycle_match="", release_date="", end_sale="", support_end="", eol_date="", extended_end="",
                              days_to_eol="", lifecycle_status="", latest_in_cycle="", newer_cycle="", kev_cves=0, kev_ransomware=0,
                              kev_latest_added="", epss_max="", dq_flags="",
                              nvd_cpe="", nvd_match="", nvd_cves_total="", nvd_cves_version="", nvd_max_cvss="", nvd_critical="", nvd_kev="", nvd_latest_cve="")
        flags = []
        pid, method, conf = match_product(ref, r)
        if not pid:
            hw = r.get("ci_type") == "hardware"
            o.update(lifecycle_status="NO_SOURCE" if hw else "UNMATCHED", review="hardware: model not in any vendor feed" if hw else "no match: in-house/unknown product?")
            if method == "alias-none": o.update(lifecycle_status="NO_PRODUCT", review="", match_method="alias-none", match_confidence=100)   # a human confirmed: not a catalogue product
            add_nvd(ref, o, r, None)           # long-tail product: still try to identify it in NVD
            out.append(o); continue
        p = ref.prods[pid]
        o.update(matched_product=p["label"], matched_source=p["source"], category=p["category"], canonical_vendor=p["vendor"],
                 match_method=method, match_confidence=conf)
        vk, pv = norm_vendor(r["vendor"]), norm_vendor(p["vendor"])
        if not r["vendor"]: flags.append("vendor_missing")
        elif vk in GENERIC_VENDORS: flags.append("vendor_generic")
        elif pv and not vend_ok(vk, pv): flags.append(f"vendor_conflict({r['vendor']}!={p['vendor']})")
        if r["vendor"] and r["vendor"] != p["vendor"] and pv and vend_ok(vk, pv): flags.append("vendor_nonstandard_spelling")
        c, cm = match_cycle(p, r["version"], r.get("fw_or_pkg", ""))
        if cm in ("cycle-not-found", "no-version"):         # e.g. product "Windows Server 2016", version is a build number
            emb = re.search(r"\s(v?\d[\w.]*(?: r2)?)", " " + norm(r["product"]))
            c2, cm2 = match_cycle(p, emb.group(1), "") if emb else (None, "")
            if c2: c, cm = c2, "from-name:" + cm2
        if cm in ("cycle-not-found", "no-version") and len(p["releases"]) == 1: c, cm = p["releases"][0], "single-cycle"   # hardware model
        if cm in ("cycle-not-found", "no-version") and p["source"] not in ("endoflife.date", "redhat") and len(p["releases"]) > 1:
            c, cm = max(p["releases"], key=lambda x: x["eol"] or date.min), "product-level"      # e.g. Exchange 2013, service pack unknown
        o["cycle_match"] = cm
        if conf >= AUTO and cm not in ("cycle-not-found", "no-version", "major-only", "major-ambiguous"): o["review"] = ""
        elif conf >= AUTO: o["review"] = f"product ok, version issue: {cm}"
        else: o["review"] = "low-confidence product match"
        if c:
            o.update(matched_cycle=c["cycle"], release_date=c["release_date"] or "", end_sale=c.get("end_sale") or "", support_end=c["eoas"] or "", eol_date=c["eol"] or "",
                     extended_end=c["eoes"] or "", latest_in_cycle=c["latest"] or "", lifecycle_status=lifecycle(c, today))
            if c["eol"]: o["days_to_eol"] = (c["eol"] - today).days
            newer = [x for x in p["releases"] if x["release_date"] and c["release_date"] and x["release_date"] > c["release_date"] and (x["maintained"] or (x["eol"] and x["eol"] >= today))]
            if newer: o["newer_cycle"] = max(newer, key=lambda x: x["release_date"])["cycle"]
            twin = next((q for q in (ref.prods[i] for i in ref.index.get(norm(p["label"]), [])) if q["id"] != p["id"]), None)
            if twin:
                tc = next((x for x in twin["releases"] if x["cycle"] == c["cycle"]), None)
                if tc and tc["eol"] and c["eol"] and tc["eol"] != c["eol"]: flags.append(f"source_mismatch_eol({twin['source']}={tc['eol']})")
        else: o["lifecycle_status"] = "VERSION_UNKNOWN"
        if p["kev"]:
            o.update(kev_cves=len(p["kev"]), kev_ransomware=sum(k["ransom"] for k in p["kev"]),
                     kev_latest_added=max(k["added"] for k in p["kev"]), epss_max=round(max(k["epss"] for k in p["kev"]), 3))
        add_nvd(ref, o, r, p)
        o["dq_flags"] = ";".join(flags); out.append(o)
    return out

def score(res, truth):
    t = {r["asset_id"]: r for r in truth}; S = Counter()
    for o in res:
        g = t[o["asset_id"]]; has = bool(g["truth_product"]); got = o["matched_product"] != "" 
        gp = g["truth_product"]; ok_p = has and got and (o["matched_product"] and ref_label_to_name(o, gp))
        auto = got and o["review"] == "" ; S["rows"] += 1
        if has:
            S["known"] += 1; S["known_matched"] += got; S["known_correct"] += bool(ok_p)
            S["auto"] += auto; S["auto_correct"] += auto and bool(ok_p)
            S["cycle_correct"] += bool(ok_p) and o["matched_cycle"] == g["truth_cycle"]
            S["review_q"] += got and not auto
        else:
            S["unknown"] += 1; S["unknown_false_match"] += got
    return S
_LAB = {}
def ref_label_to_name(o, truth_name):
    return _LAB.get(o["matched_product"], "") == truth_name or norm(o["matched_product"]) == norm(truth_name.replace("-", " "))

def main():
    global ref
    ap = argparse.ArgumentParser(); ap.add_argument("--snapshot", default="snapshot"); ap.add_argument("--cmdb", required=True)
    ap.add_argument("--truth"); ap.add_argument("--out", default="out2"); ap.add_argument("--today")
    a = ap.parse_args(); today = date.fromisoformat(a.today) if a.today else date.today()
    ref = Ref(Path(a.snapshot))
    for p in ref.prods.values(): _LAB[p["label"]] = p["name"]
    rows = list(csv.DictReader(open(a.cmdb))); res = enrich(ref, rows, today)
    out = Path(a.out); out.mkdir(exist_ok=True)
    def wr(fn, data):
        with open(out / fn, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys())); w.writeheader(); w.writerows(data)
    wr("cmdb_enriched.csv", res); wr("review_queue.csv", [r for r in res if r["review"] and r["lifecycle_status"] not in ("NO_SOURCE",)])
    st = Counter(r["lifecycle_status"] for r in res); meth = Counter(r["match_method"] for r in res)
    print(f"{len(res)} rows | reference: {len(ref.prods)} products, {len(ref.keys)} name keys, KEV-linked products: {sum(1 for p in ref.prods.values() if p['kev'])}")
    print("lifecycle:", dict(st)); print("method:", dict(meth))
    print("exposed (EOL/extended-only) rows:", sum(st[k] for k in ("EOL", "EXTENDED_SUPPORT_ONLY")), "| with KEV exposure:", sum(1 for r in res if r["kev_cves"] and r["lifecycle_status"] in ("EOL", "EXTENDED_SUPPORT_ONLY")))
    n = [r for r in res if r["nvd_cpe"]]; v = [r for r in n if r["nvd_cves_version"] not in ("", 0)]
    print(f"NVD: identified {len(n)} rows ({len(n)/len(res):.1%}) | with >=1 CVE applicable to the version: {len(v)} | "
          f"critical (CVSS>=9): {sum(1 for r in n if r['nvd_critical'] not in ('', 0))} | affected by a KEV CVE: {sum(1 for r in n if r['nvd_kev'] not in ('', 0))}")
    if a.truth:
        S = score(res, list(csv.DictReader(open(a.truth))))
        pct = lambda x, y: f"{100*x/max(y,1):.1f}%"
        print(f"\nvs ground truth: known-product rows={S['known']}\n  matched {pct(S['known_matched'],S['known'])} | correct product {pct(S['known_correct'],S['known'])} | correct cycle {pct(S['cycle_correct'],S['known'])}"
              f"\n  auto-accepted {pct(S['auto'],S['known'])} of rows, precision of auto-accepted {pct(S['auto_correct'],S['auto'])}"
              f"\n  review queue {S['review_q']} rows | unknown/in-house rows falsely matched: {S['unknown_false_match']}/{S['unknown']}")
if __name__ == "__main__": main()
