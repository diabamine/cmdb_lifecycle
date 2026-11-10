# Where AI / GenAI adds value (and where it doesn't)

Measured on the 5,000-row synthetic dirty CMDB (`data/`), deterministic cascade only (no AI):
~98% correct product match, 99.6% precision on auto-accepted rows (76% of rows), 949 rows left in the review queue.
That queue + the coverage gaps below are the *only* places AI should be spent.

## Principle
AI **maps and extracts**; it never **supplies facts**. Dates (EOL/EOS), CVE ids and scores always come from the
snapshot. AI output is a *candidate* with a confidence, logged, and either auto-accepted above a threshold or
sent to a human whose decision becomes a permanent alias (so AI cost decays over time).

## Use cases, ranked by value / effort

| # | Use case | Gap it closes (observed) | Technique | Runs offline? | Guardrail |
|---|---|---|---|---|---|
| 1 | **Entity resolution of residual names** | ~115 low-confidence + 359 unmatched rows (typos, reversed words, vendor-prefixed names, "SUSESERVER", "windowsnanoserver") | Local embedding model (bge-small / MiniLM) over reference names + CPE titles to propose top-5; optional small LLM re-ranks | Yes (model file is a one-off download) | Top-k candidates only; accept if margin large, else review. Log decision as alias |
| 2 | **Version normalization / build-number mapping** | `cycle-not-found` (182), `major-ambiguous` (234): "10.0.17763" -> Windows Server 2019, "15.2.2562" -> Exchange 2019, "v2 Ootpa" -> RHEL 8 | Regex first; LLM or lookup-table for vendor-specific build schemes; learn a build->cycle table from Windows/Office/VMware release-notes pages offline | Yes | Output must be a cycle that exists in the reference, else reject |
| 3 | **Hardware lifecycle extraction** (474 rows = 9%: servers, switches, firewalls, storage) | No public API: Dell/HPE/Cisco/NetApp publish EoL/EoS as PDF/HTML bulletins | LLM structured extraction (schema: model, EoSale, EoSupport, EoSW-maint, replacement) from bulletins you download once; then fuzzy-match models | Extraction can be done once online, matching offline | Store source URL + page quote per date; human spot-check sample |
| 4 | **Is-this-in-house? classifier** (359 unmatched) | Distinguish "custom app" from "known product we failed to match" | Embedding nearest-neighbour distance + LLM yes/no with justification | Yes | Never auto-drop; feeds review queue priority |
| 5 | **Risk narrative & prioritization** | 685 EOL rows with KEV exposure (e.g. IE 7-11: 39 KEV CVEs, 8 ransomware, EPSS .97) | LLM drafts per-product/per-BU summary and remediation options (upgrade target = `newer_cycle`, ESU/ELS option from `extended_end`) from structured fields only | Needs local LLM | Prompt gets only the enriched row; numbers rendered by code, not by the model |
| 6 | **Data-quality triage / explanations** | 2,135 missing vendors, 1,346 non-standard spellings, 166 vendor conflicts | Cluster dirty vendor strings (embedding + agglomerative), LLM proposes canonical vendor per cluster -> one human click per cluster | Yes | Cluster-level approval, not row-level |
| 7 | **Package / SBOM line parsing** | `openssl-1.1.1k-9.el8`, `.deb`/`.msi` display names, "Java(TM) SE Runtime 8u291" | Rules for rpm/deb/purl; LLM fallback for free-form installer names | Yes | Validate against reference purl/CPE names |
| 8 | **Natural-language query over the CMDB** | "Which prod Plant-EU hosts run unsupported DBs exploited in the wild?" | Text-to-SQL / tool-calling over `cmdb_enriched` (SQLite) | Needs local LLM | Read-only DB, row limit, show the SQL |
| 9 | **Vendor-bulletin / advisory monitoring** (when online) | EOL dates move (extensions, early retirement) | LLM diff of new announcements vs snapshot -> proposed date changes | Online side only | Proposals reviewed before they enter snapshot |

## Not worth AI
* Exact/alias/fuzzy matching of the easy 90% (already deterministic, auditable, free).
* Training a supervised classifier: the review decisions are cheaper as an alias table.
* Generating EOL dates or CVE lists from model memory: hallucination risk, no provenance.

## Suggested rollout
1. Ship the deterministic cascade + review queue (done in `cmdb_enrich.py`).
2. Add embeddings candidate generator (#1) – biggest cut of the review queue for little risk.
3. Build build-number tables (#2) and cluster-level vendor cleanup (#6).
4. Hardware bulletins extraction (#3) as a separate, human-verified data project.
5. Only then LLM narrative/NL query (#5, #8), once the data underneath is trusted.

## Metrics to track per release of the matcher
auto-accept rate, auto-accept precision (sampled), review-queue size, % rows with usable cycle,
alias-table growth, cost per resolved row (human + model).
