import gzip
import json
from datetime import date

from cmdb.exposure import add_exposure
from cmdb.matching import match_rows
from cmdb.schema import STANDARD_COLUMNS


def catalog_row(**values):
    row = {column: "" for column in STANDARD_COLUMNS}
    row.update(values)
    return row


def test_kev_and_epss_columns_added_and_nvd_skipped_when_missing(tmp_path):
    catalog = [catalog_row(id="p:widget@1", product_id="p:widget", product="Widget", version="1",
                           vendor="acme", source="manual:test", eol="2020-01-01")]
    kev = {"vulnerabilities": [
        {"cveID": "CVE-1", "vendorProject": "Acme", "product": "Widget", "dateAdded": "2024-01-02",
         "knownRansomwareCampaignUse": "Known"},
        {"cveID": "CVE-2", "vendorProject": "Other Corp", "product": "Widget", "dateAdded": "2024-05-06",
         "knownRansomwareCampaignUse": "Unknown"},       # same name, different vendor: must not link
    ]}
    (tmp_path / "cisa_kev.json").write_text(json.dumps(kev))
    with gzip.open(tmp_path / "epss_scores.csv.gz", "wt") as handle:
        handle.write("#model_version:v1\ncve,epss,percentile\nCVE-1,0.5,0.9\nCVE-2,0.7,0.95\n")

    rows = [{"vendor": "Acme", "product": "Widget", "version": "1"}]
    results = match_rows(rows, catalog, today=date(2026, 1, 1))
    exposed = add_exposure(results, catalog, tmp_path)[0]

    assert exposed["kev_cves"] == 1
    assert exposed["kev_ransomware"] == 1
    assert exposed["kev_latest_added"] == "2024-01-02"
    assert exposed["epss_max"] == 0.5
    assert exposed["nvd_cpe"] == ""
    assert "kev_cves" not in results[0]            # input rows are not mutated
