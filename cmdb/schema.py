"""Column contracts shared by every pipeline stage. Stages exchange plain dicts / CSV files with these keys, nothing else."""

# One row per product release cycle. Dates are ISO "YYYY-MM-DD" or "". Lists are "|"-separated.
STANDARD_COLUMNS = [
    "id",            # unique row id: f"{product_id}@{version}"
    "product_id",    # stable per product, e.g. "eol:python", "rh:rhel", "v:microsoft:Windows Server 2019"
    "source",        # endoflife.date | redhat | microsoft | juniper | vmware | ibm | apple | nvidia | cisco | eol.network | eosl.date | eosl.ai | citrix | trellix | parkplace | manual:<file>
    "vendor",        # as published by the source (cleaning happens in the catalog step)
    "product",       # canonical product name
    "aliases",       # alternative spellings, "|"-separated (marketing names, SKUs, purl/cpe names)
    "category",      # os | lang | db | hardware | ... (source's own category, "" if unknown)
    "version",       # release cycle: "3.11", "2019", "7.x", or a hardware model
    "release_date", "end_sale", "end_support", "eol", "end_extended",
    "is_eol",        # "true" when the source says no longer serviced but gives no date, else ""
    "latest",        # latest release inside the cycle, if known
    "url",           # vendor / source page
    "origin",        # finer provenance inside a source (e.g. IBM bulk csv, juniper kb)
]

# Columns written by matching for every input row (input columns are kept as-is, these are appended).
MATCH_COLUMNS = [
    "product_id", "matched_product", "matched_source", "matched_vendor", "category",
    "match_method",       # alias | alias-none | exact | exact+cleaned | exact+rule | exact+rollup | fuzzy | none
    "match_confidence",   # 0-100
    "review",             # "" = auto-accepted, otherwise the reason a human should look
    "matched_cycle", "cycle_match", "release_date", "end_sale", "support_end", "eol_date", "extended_end",
    "days_to_eol", "lifecycle_status", "latest_in_cycle", "newer_cycle", "dq_flags",
]
