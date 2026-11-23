#!/usr/bin/env python3
"""Generate a realistic, DIRTY CMDB extract from real public product names (endoflife.date
catalogue) + hand-listed hardware. Ground truth goes to a separate file so the matcher can be
scored. Usage: make_synthetic_cmdb.py snapshot out_dir [--n 5000] [--seed 7]"""
import argparse, csv, json, random, re
from pathlib import Path

HARDWARE = [  # (vendor, model, type) – no public EOL feed for these: a deliberate gap
    ("Dell Inc.", "PowerEdge R640", "server"), ("Dell Inc.", "PowerEdge R740xd", "server"),
    ("HPE", "ProLiant DL380 Gen10", "server"), ("Hewlett Packard Enterprise", "ProLiant DL360 Gen9", "server"),
    ("Cisco Systems", "Catalyst 9300-48P", "switch"), ("Cisco", "ASA 5525-X", "firewall"),
    ("Lenovo", "ThinkSystem SR650", "server"), ("NetApp", "FAS8200", "storage"),
    ("Palo Alto Networks", "PA-3220", "firewall"), ("Fortinet", "FortiGate 100F", "firewall"),
    ("Juniper Networks", "EX4300-48T", "switch"), ("IBM", "Power S922", "server"),
]
INHOUSE = ["BillingCore", "plant-scada-gw", "HR Portal", "LogiTrack", "MES-Connector", "QualityHub",
           "SAP-Adapter-X", "InventoryDB-legacy", "Reporting Engine", "CustomerSync"]
VENDOR_ALIASES = {"microsoft": ["Microsoft Corporation", "MSFT", "Microsoft Corp.", "microsoft"],
                  "redhat": ["Red Hat, Inc.", "RedHat", "Red Hat Inc", "RH"],
                  "oracle": ["Oracle Corporation", "Oracle America, Inc.", "ORCL", "oracle corp"],
                  "canonical": ["Canonical Ltd.", "Canonical"], "apache": ["The Apache Software Foundation", "ASF", "Apache"],
                  "vmware": ["VMware, Inc.", "VMware by Broadcom", "Broadcom (VMware)"], "ibm": ["IBM Corp.", "International Business Machines"],
                  "google": ["Google LLC", "Google Inc."], "postgresql": ["PostgreSQL Global Development Group", "PGDG"],
                  "mozilla": ["Mozilla Foundation", "Mozilla Corporation"], "elastic": ["Elasticsearch B.V.", "Elastic NV"]}
SUFFIX = ["", "", "", " (x64)", " Server", " Enterprise Edition", " Standard", " - 64 bit", " LTS", " Client", " runtime"]
ENVS = ["prod", "prod", "prod", "preprod", "test", "dev", "dr"]
BUS = ["Plant-EU", "Plant-NA", "IT-Core", "R&D", "Finance", "Logistics", "Digital"]

def typo(s, r):
    if len(s) < 5: return s
    i = r.randrange(1, len(s) - 2); op = r.choice("sdt")
    if op == "s": return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "d": return s[:i] + s[i + 1:]
    return s[:i] + s[i] + s[i:]
def vendor_from(p):
    for i in p.get("identifiers", []):
        if i["type"] == "cpe" and i["id"].startswith("cpe:2.3"): return i["id"].split(":")[3]
    return ""
def pretty_vendor(v, r):
    if not v: return ""
    if v in VENDOR_ALIASES and r.random() < .7: return r.choice(VENDOR_ALIASES[v])
    return r.choice([v.replace("_", " ").title(), v.replace("_", " "), v.upper()])
def name_variant(p, r):
    base = r.choice([p["label"], p["label"], p["name"].replace("-", " "), *(p.get("aliases") or [])])
    if r.random() < .35: base = base + r.choice(SUFFIX)
    if r.random() < .12: base = typo(base, r)
    if r.random() < .15: base = base.upper() if r.random() < .5 else base.lower()
    if r.random() < .08: base = " ".join(reversed(base.split()))
    return base.strip()
def version_variant(rel, r):
    latest = (rel.get("latest") or {}).get("name") or rel["name"]
    v = latest if re.match(r"^[\d.]+$", latest) else rel["name"]
    if r.random() < .12: return ""  # missing
    f = r.random()
    if f < .15: return "v" + v
    if f < .25: return f"release {v}"
    if f < .35: return v.split(".")[0] if re.match(r"^\d", v) else v
    if f < .40: return v + ".0"
    return v

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("snapshot"); ap.add_argument("out"); ap.add_argument("--n", type=int, default=5000); ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(); r = random.Random(a.seed)
    prods = json.load(open(Path(a.snapshot) / "endoflife_full.json"))["result"]
    w = {"os": 6, "server-app": 5, "database": 5, "app": 4, "lang": 3, "framework": 3, "service": 1, "device": 1, "standard": 0}
    pool = [p for p in prods if p["releases"] and w[p["category"]]]
    wts = [w[p["category"]] for p in pool]
    # long-tail: a few popular products dominate, as in real CMDBs
    pop = {p["name"]: r.paretovariate(1.2) for p in pool}; wts = [x * pop[p["name"]] for x, p in zip(wts, pool)]
    rows, truth = [], []
    for i in range(a.n):
        aid = f"CI{100000+i}"; host = f"{r.choice(['srv','vm','app','db','ctl'])}-{r.choice(['par','fra','dtw','sin','mil'])}-{r.randrange(1,999):03d}"
        k = r.random(); base = dict(asset_id=aid, hostname=host, environment=r.choice(ENVS), business_unit=r.choice(BUS),
                                    last_seen=f"2026-{r.randrange(1,11):02d}-{r.randrange(1,28):02d}")
        if k < .10:                                   # hardware
            v, m, t = r.choice(HARDWARE)
            rows.append(dict(base, ci_type="hardware", vendor=v if r.random() > .1 else "", product=m if r.random() > .1 else m.lower(), version="", fw_or_pkg=""))
            truth.append(dict(asset_id=aid, truth_product="", truth_cycle="", note="hardware-no-source")); continue
        if k < .17:                                   # in-house / unknown
            rows.append(dict(base, ci_type="software", vendor=r.choice(["", "Internal", "ACME IT"]), product=r.choice(INHOUSE), version=f"{r.randrange(1,6)}.{r.randrange(0,9)}", fw_or_pkg=""))
            truth.append(dict(asset_id=aid, truth_product="", truth_cycle="", note="inhouse")); continue
        p = r.choices(pool, wts)[0]; rel = r.choice(p["releases"])
        ctype = {"os": "os", "server-app": "software", "database": "software", "app": "software"}.get(p["category"], "software")
        vend = pretty_vendor(vendor_from(p), r) if r.random() > .25 else ""
        pkg = ""
        if r.random() < .12 and p["category"] != "os":  # package-style row
            pkg = f"{p['name']}-{(rel.get('latest') or {}).get('name') or rel['name']}-{r.randrange(1,20)}.el{r.choice([7,8,9])}"
        rows.append(dict(base, ci_type=ctype, vendor=vend, product=name_variant(p, r), version=version_variant(rel, r), fw_or_pkg=pkg))
        truth.append(dict(asset_id=aid, truth_product=p["name"], truth_cycle=rel["name"], note=""))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for fn, data in (("cmdb_synthetic.csv", rows), ("cmdb_truth.csv", truth)):
        with open(out / fn, "w", newline="") as f:
            w_ = csv.DictWriter(f, fieldnames=list(data[0].keys())); w_.writeheader(); w_.writerows(data)
    print(f"{len(rows)} rows -> {out}")
if __name__ == "__main__": main()
