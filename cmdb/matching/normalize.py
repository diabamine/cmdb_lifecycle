"""Name and vendor normalisation, plus the rules that turn a CMDB product string into candidate catalog names.
Pure functions on strings and plain dicts; no I/O."""
import re
import unicodedata

from rapidfuzz import fuzz

# Words that say nothing about which company a vendor is ("Oracle Corporation" == "Oracle").
CORP = {"inc", "corp", "corporation", "ltd", "llc", "bv", "nv", "foundation", "the", "software", "systems",
        "company", "co", "gmbh", "sa", "by", "group", "global", "development", "project", "organization",
        "international", "networks", "technologies", "technology", "incorporated", "limited", "plc", "ag"}
VENDOR_SYNONYMS = {"msft": "microsoft", "rh": "redhat", "orcl": "oracle", "sun": "oracle", "sun microsystems": "oracle", "pgdg": "postgresql", "asf": "apache",
                   "hpe": "hewlett packard enterprise", "hp": "hewlett packard"}
# Packagers / placeholders that carry no vendor signal ("Ubuntu Developers" says nothing about the product).
GENERIC_VENDORS = {"ubuntudevelopers", "debianmaintainers", "internal", "acmeit", "unknown", "na"}

NOISE_PHRASES = [r"\(x64\)", r"\bx64\b", r"\b64 ?bit\b", r"\benterprise edition\b", r"\bstandard\b", r"\blts\b",
                 r"\bclient\b", r"\bruntime\b", r"\bedition\b", r"\bdatacenter\b", r"\bprofessional plus\b",
                 r"\bprofessional\b", r"\bappliance\b", r"\benterprise$", r"\besr\b", r"\bbeta\b", r"\bnightly\b",
                 r"\bdeveloper edition\b", r"\bmsix\b"]
NOISE_PATTERNS = [re.compile(p) for p in NOISE_PHRASES]

# Locale / architecture suffix: "(hu)", "(es-MX)", "(MSIX)"
LOCALE_RE = re.compile(r"\s*\((?:[a-z]{2,3}(?:-[A-Za-z0-9]{2,4})?|msix|x86|x64|arm64)\)\s*$", re.I)
VENDOR_PREFIXES = ("microsoft", "mozilla", "oracle", "google", "adobe", "apple", "vmware", "red hat", "jetbrains",
                   "apache", "ibm", "cisco", "juniper", "citrix")

# Vendor naming habits: (regex on the normalised name, replacement).
VENDOR_RULES = [
    (r"^(?:microsoft )?(?:asp\.net core|\.net(?: core)?)(?: windows desktop| desktop)? (?:runtime|sdk|hosting bundle)\b.*", ".net"),
    (r"^(?:microsoft )?visual studio (?:build ?tools|community|professional|enterprise) (\d{4}).*", r"visual studio \1"),
    (r"^java (?:tm )?se (?:development kit|runtime environment)\b.*", "oracle jdk"),
    (r"^(?:mozilla )?(firefox|thunderbird)\b.*", r"\1"),
]

# Sub-package -> upstream product: (regex on the lower-case package name, upstream product).
ROLLUP_RULES = [
    (r"^(?:g\+\+|gcc|cpp|gfortran)(?:-.*)?$", "gcc"),
    (r"^linux-(?:image|headers|modules|tools|ibm|aws|azure|gcp|oem|generic|source|libc-dev).*$", "linux kernel"),
    (r"^libc6.*$|^libc-.*$|^libc\d.*$", "glibc"),
    (r"^(?:libssl|libcrypto|openssl).*$", "openssl"),
    (r"^libcurl.*$", "curl"),
    (r"^(?:libpq|postgresql).*$", "postgresql"),
    (r"^libsqlite3.*$", "sqlite"),
    # python3-colorama is a module, not Python, so only the interpreter packages are listed
    (r"^(?:libpython3(?:\.\d+)?.*|python3(?:\.\d+)?(?:-(?:minimal|dev|doc|venv|distutils|lib2to3|tk|gdbm))?)$", "python"),
    (r"^openjdk-\d+.*$", "openjdk"),
    (r"^(?:mysql|mariadb)-.*$", "mysql"),
    (r"^libxml2.*$", "libxml2"),
    (r"^libgnutls.*$", "gnutls"),
]
LANG_PREFIX = re.compile(r"^(?:python3?|ruby|golang|node|php|lua|perl)-")   # language binding named after the upstream module
PKG_SUFFIX = re.compile(r"-(?:dev|doc|docs|data|common|dbg|dbgsym|bin|utils|tools|cli|static|headers|tests?|examples|l10n|plugins?|server|client|libs?|core|base)$")
PKG_VERSION = re.compile(r"(?:-\d+(?:\.\d+)*)+$")   # trailing -14, -1.0-0
SONAME = re.compile(r"(?<=[a-z])\d+$")              # libtiff5 -> libtiff (only applied to lib* packages)


def norm(text):
    """Lower-case ASCII with single spaces; keeps dots so versions and '.net' survive."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_vendor(vendor):
    """Compact vendor key: corporate suffixes dropped, synonyms folded, spaces removed."""
    words = norm(vendor).replace(".", " ").split()
    words = [VENDOR_SYNONYMS.get(w, w) for w in words if w not in CORP]
    if not words:
        return ""
    return " ".join(words).replace("red hat", "redhat").replace(" ", "")


def vend_ok(a, b):
    """Do two normalised vendor keys denote the same vendor? Equal, prefix of each other, or very similar."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return fuzz.ratio(a, b) >= 85


def digit_tokens(text):
    """Words containing a digit; two names with different ones are different models/versions."""
    return {word for word in text.split() if any(ch.isdigit() for ch in word)}


def strip_noise(text):
    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def rollup_names(pkg):
    """Debian/RPM sub-package name -> [(candidate upstream name, kind)].
    kind 'rollup-lang' marks a guess from a python3-/ruby- binding."""
    out = [(upstream, "rollup") for rx, upstream in ROLLUP_RULES if re.match(rx, pkg)]
    stripped = LANG_PREFIX.sub("", pkg)
    is_lang = stripped != pkg
    # peel suffixes and version tokens until the name is stable
    for _ in range(3):
        peeled = PKG_VERSION.sub("", PKG_SUFFIX.sub("", stripped))
        if peeled.startswith("lib"):
            peeled = SONAME.sub("", peeled)
        if peeled == stripped or not peeled:
            break
        stripped = peeled
    cleaned = norm(stripped.replace("-", " "))
    if stripped != pkg and len(cleaned) >= 4:
        out.append((cleaned, "rollup-lang" if is_lang else "rollup"))
    return out


def name_candidates(row):
    """{candidate spelling: kind} in trial order. The kind records WHY a spelling exists, so a result can say how it matched:
    raw | cleaned | vstrip | rule | rollup | rollup-lang."""
    out = {}

    def add(candidate, kind):
        if candidate and candidate not in out:
            out[candidate] = kind

    product = row.get("product") or ""
    add(norm(product), "raw")

    base = norm(LOCALE_RE.sub("", product))             # "Mozilla Firefox (hu)" -> "mozilla firefox"
    spellings = [(base, "cleaned")]
    for prefix in (*VENDOR_PREFIXES, norm(row.get("vendor"))):
        long_enough = len(base) - len(prefix) - 1 >= 5
        if prefix and base.startswith(prefix + " ") and long_enough:
            # vendor word removed ("mozilla firefox" -> "firefox"): exact lookup only, never fuzzy
            spellings.append((base[len(prefix) + 1:], "vstrip"))

    for name, kind in spellings:
        stripped = strip_noise(name)
        add(name, kind)
        add(stripped, kind)
        if stripped.endswith(" server"):
            add(stripped[:-7], kind)
        # version embedded in the name: "sql server 2019" -> "sql server"
        embedded = re.match(r"^(.*?)\s+v?\d.*$", stripped)
        if embedded:
            add(embedded.group(1), kind)

    for name, _ in spellings:
        for pattern, replacement in VENDOR_RULES:
            if re.match(pattern, name):
                add(re.sub(pattern, replacement, name).strip(), "rule")

    package = row.get("fw_or_pkg") or ""
    if package:                                          # package-style row (Debian, RPM)
        add(norm(re.split(r"-\d", package)[0].replace("-", " ")), "cleaned")
        for candidate, kind in rollup_names(product.lower()):
            add(candidate, kind)
    return out
