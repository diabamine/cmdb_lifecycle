# Refactor plan: pipeline in six steps

Goal: replace the flat scripts with a small package `cmdb/` where each step reads and writes plain files / dicts.
Style rules (apply to every agent): lean Python, no dense one-liners, comments explain *why*, no speculative abstraction,
parsers/matching never import Streamlit, the UI never imports parser internals. Data crosses boundaries as `list[dict]` or CSV.

## Layout
```
snapshot/                      RAW layer, kept where it is (committed for offline deployment). json/csv as fetched.
data/standard/<source>.csv     step 1 output, STANDARD_COLUMNS (cmdb/schema.py), one file per source
data/master_catalog.csv        step 2 output, same columns, cleaned + concatenated
cmdb/schema.py                 column contracts (FROZEN, do not edit without telling the lead)
cmdb/ingest/<source>.py        step 1: fetch(raw_dir) -> None (network, writes raw) ; standardize(raw_dir) -> list[dict]
cmdb/ingest/__main__.py        python -m cmdb.ingest [--fetch] [--only name ...]
cmdb/catalog.py                step 2: build_catalog(standard_dir) -> list[dict] ; write_catalog / load_catalog
cmdb/matching/                 step 3: normalize.py, index.py, matcher.py, lifecycle.py
cmdb/exposure.py               optional layer kept from today's engine: KEV / EPSS / NVD columns on matched rows
cmdb/metrics.py                step 5
cmdb/compare.py                step 6
streamlit_app.py               step 4 (rendering only)
tools/                         test-data helpers (synthetic CMDB, public inventory, scoring)
tests/                         pytest, small fixtures only (never the 300 MB NVD file)
```

## Public interfaces (the contract between agents)
```python
# step 1  cmdb/ingest/<source>.py  (sources: endoflife, redhat, microsoft, juniper, vmware, ibm, apple, nvidia, cisco, eolnetwork, eosldate, manual)
def fetch(raw_dir: Path) -> None
def standardize(raw_dir: Path) -> list[dict]        # keys == STANDARD_COLUMNS

# step 2  cmdb/catalog.py
def build_catalog(standard_dir: Path) -> list[dict]
def write_catalog(rows, path) -> None
def load_catalog(path) -> list[dict]

# step 3  cmdb/matching/matcher.py   (inputs: dicts with vendor, product, version, optional ci_type, fw_or_pkg, any extra columns)
def match_rows(rows: list[dict], catalog: list[dict], aliases: dict | None = None, today: date | None = None) -> list[dict]
def match_one(query: dict, catalog_index, trace: dict | None = None) -> dict   # same keys as one match_rows output row
def build_index(catalog: list[dict])                # opaque to callers; cache it
def search_catalog(catalog: list[dict], text: str, limit: int = 50) -> list[dict]   # name/alias/vendor search for the UI
def load_aliases(path) -> dict ; def save_alias(path, vendor, product, target_id) -> None

# step 5  cmdb/metrics.py
def catalog_coverage(catalog) -> list[dict]         # per source: products, cycles, % with eol date, % with vendor, ...
def matching_metrics(results, truth=None) -> dict   # match rate, auto rate, review rate, by method / source / lifecycle, precision if truth

# step 6  cmdb/compare.py
def load_component_catalog(path) -> list[dict]      # user CSV (vendor, product, version, ...) mapped to STANDARD_COLUMNS
def compare_references(rows, references: dict[str, list[dict]]) -> list[dict]   # per input row: match from each reference + "agree" flag
def comparison_summary(compared) -> list[dict]
```

## Work packages (one agent each, own git worktree, own files only)
| WP | Step | Owns | Notes |
|---|---|---|---|
| A | 1 ingest | `cmdb/ingest/**`, `data/standard/**`, `tests/test_ingest_*.py` | split fetch_snapshot / fetch_vendors / fetch_eolnetwork into per-source modules; keep their safety behaviours (retries, keep old data on failure, eol.network quota rules, .env handling) |
| B | 2 catalog | `cmdb/catalog.py`, `tests/test_catalog.py` | works from fixture standard CSVs until A merges; ESU rows, vendor canonicalisation, dedupe, date validation live here |
| C | 3 matching | `cmdb/matching/**`, `cmdb/exposure.py`, `tools/score.py`, `tests/test_matching*.py` | port the cascade from `cmdb_enrich.py`; no accuracy regression (below) |
| D | 5+6 metrics, compare | `cmdb/metrics.py`, `cmdb/compare.py`, tests | code against the contract with fixture dicts |
| E | 4 UI | `streamlit_app.py`, `tests/test_app_smoke.py` | tabs: Search catalog, Match (text box / CSV upload), Metrics, Compare; calls only the public functions above |

Legacy scripts stay in place until the lead has checked parity, then are removed in one cleanup commit.

## Acceptance (lead checks after merge)
Baseline from the old engine on `data/cmdb_synthetic.csv` with `--today 2026-11-23`:
product matched 96.6%, correct product 96.0%, correct cycle 77.2%, auto-accepted 75.4% with precision 99.4%,
unknown rows falsely matched 121/814. New pipeline (master catalog from the committed snapshot) must stay within 0.5 point on each.
`pytest -q` green; `python -m cmdb.ingest` (offline standardize) then `python -m cmdb.catalog` rebuild with no network.
