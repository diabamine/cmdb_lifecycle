"""Standardize/parse tests with tiny inline fixtures (no network)."""
import csv
import json

import pytest

from cmdb.ingest import apple, citrix, trellix, eolnetwork, eosldate, eoslai, endoflife, ibm, manual, microsoft, redhat
from cmdb.ingest.__main__ import main
from cmdb.ingest.common import CollectorError, iso, month_end, rec, vendor_rows
from cmdb.schema import STANDARD_COLUMNS

ENDOFLIFE = {"result": [{
    "name": "python-lang", "label": "Python", "category": "lang", "aliases": ["py"],
    "links": {"html": "https://endoflife.date/python-lang"},
    "identifiers": [{"type": "cpe", "id": "cpe:2.3:a:python:python:*:*:*:*:*:*:*:*"},
                    {"type": "purl", "id": "pkg:docker/library/python"}],
    "releases": [
        {"name": "3.12", "releaseDate": "2023-10-02", "eoasFrom": "2025-04-02", "eolFrom": "2028-10-31",
         "eoesFrom": None, "isEol": False, "latest": {"name": "3.12.4"}},
        {"name": "2.7", "releaseDate": "2010-07-03", "eoasFrom": None, "eolFrom": None, "isEol": True, "latest": None},
    ]}]}

REDHAT = {"data": [{
    "name": "Red Hat Enterprise Linux", "link": "https://example.test/rhel", "former_names": ["RHEL"],
    "all_phases": [{"name": "General availability", "ptype": "normal"}, {"name": "Full support", "ptype": "normal"},
                   {"name": "Maintenance support", "ptype": "normal"}, {"name": "ELS", "ptype": "extended"}],
    "versions": [{"name": "7", "final_minor_release": "7.9", "phases": [
        {"name": "General availability", "end_date": "2013-12-01"}, {"name": "Full support", "end_date": "2019-08-06"},
        {"name": "Maintenance support", "end_date": "2024-06-30"}, {"name": "ELS", "end_date": "2028-06-30T00:00:00Z"}]}],
}]}


def test_endoflife_rows(tmp_path):
    (tmp_path / "endoflife_full.json").write_text(json.dumps(ENDOFLIFE))
    rows = endoflife.standardize(tmp_path)
    assert [r["id"] for r in rows] == ["eol:python-lang@3.12", "eol:python-lang@2.7"]
    current, dead = rows
    assert list(current) == STANDARD_COLUMNS
    assert (current["vendor"], current["product"], current["category"]) == ("python", "Python", "lang")
    assert current["end_support"] == "2025-04-02" and current["eol"] == "2028-10-31" and current["latest"] == "3.12.4"
    assert set(current["aliases"].split("|")) >= {"python lang", "py", "python-lang", "python python lang"}
    assert current["is_eol"] == ""
    assert dead["is_eol"] == "true" and dead["eol"] == ""      # "dead" without a date


def test_redhat_phases(tmp_path):
    (tmp_path / "redhat_lifecycle.json").write_text(json.dumps(REDHAT))
    (row,) = redhat.standardize(tmp_path)
    assert row["product_id"] == "rh:Red Hat Enterprise Linux" and row["aliases"] == "RHEL"
    assert row["release_date"] == "2013-12-01"
    assert row["end_support"] == "2019-08-06" and row["eol"] == "2024-06-30" and row["end_extended"] == "2028-06-30"
    assert row["latest"] == "7.9"


def test_vendor_rows_keep_esu_and_status():
    records = [
        rec("microsoft", "Windows Server 2012", "Windows Server 2012", "learn", "u", "os-or-app", eol="2023-10-10"),
        rec("microsoft", "Windows Server 2012", "Extended Security Updates year 3", "learn", "u", eol="2026-10-13"),
        rec("apple", "MacBook (2006)", "MacBook (2006)", "apple.page", "u", "hardware", status="obsolete"),
    ]
    rows = vendor_rows(records, source="vendor")
    assert len(rows) == 3                                       # ESU stays a row; the catalog step merges it
    assert rows[1]["version"] == "Extended Security Updates year 3" and rows[1]["eol"] == "2026-10-13"
    assert "microsoft windows server 2012" in rows[0]["aliases"].lower().split("|")   # alias dedupe is case-insensitive
    assert rows[2]["is_eol"] == "true" and rows[2]["origin"] == "apple.page" and rows[2]["source"] == "vendor"
    assert rows[0]["product_id"] == "v:microsoft:Windows Server 2012"


def test_duplicate_cycles_get_unique_ids():
    records = [rec("juniper", "EX4200", "EX4200", "s", "u", eol="2020-01-01"),
               rec("juniper", "EX4200", "EX4200", "s", "u", eol="2021-01-01")]
    ids = [r["id"] for r in vendor_rows(records, source="juniper")]
    assert ids == ["v:juniper:EX4200@EX4200", "v:juniper:EX4200@EX4200#2"]


def test_date_helpers():
    assert iso("10/30/2026") == "2026-10-30" and iso("April 2, 2020") == "2020-04-02" and iso("n/a") == ""
    assert month_end("February 2024") == "2024-02-29" and month_end("soon") == ""


def test_microsoft_table_parse():
    table = [["Release", "Start date", "Mainstream end date", "Extended end date"],
             ["SQL Server 2019", "11/04/2019", "02/28/2025", "01/08/2030"]]
    (record,) = microsoft.parse_tables([table], "SQL Server", "http://x")
    assert (record["cycle"], record["release_date"], record["end_support_full"], record["eol"]) == \
        ("SQL Server 2019", "2019-11-04", "2025-02-28", "2030-01-08")


def test_ibm_rows():
    row = {"Type": "HW", "IBM Product": "IBM Power System S922 Server (9009-22A)", "MTM": "9009-22A", "VRM": "N/A",
           "General Availability": "2018-02-13", "Withdrawn from Marketing": "2022-03-01",
           "Transition to Extended/Sustained, End of Support": "2025-04-30",
           "Completion of Extended, Sustained, Extension availability": "2027-04-30", "More Info URL": ""}
    (record,) = ibm.parse_rows([row, {**row, "Type": "SW"}])
    assert record["eol"] == "2025-04-30" and record["end_extended"] == "2027-04-30"
    assert "Power S922" in record["label_alias"] and "9009-22A" in record["label_alias"]


def test_apple_page_skips_phones_and_maps_status():
    page = ('<h2 id="macvintage">Vintage</h2><ul><li>MacBook Pro (2012)</li></ul>'
            '<h2 id="macobsolete">Obsolete</h2><ul><li>iMac (2006)</li></ul>'
            '<h2 id="iphoneobsolete">Obsolete</h2><ul><li>iPhone 3G</li></ul>')
    records = apple.parse_page(page)
    assert [(r["product"], r["status"]) for r in records] == [("MacBook Pro (2012)", "vintage"), ("iMac (2006)", "obsolete")]


def test_eolnetwork_groups_skus():
    rows = [
        {"product_id": "A1", "product_name": "Switch A1", "applicable_series": ["Series X"], "end_of_sale_date": "2020-01-01",
         "last_date_of_support": "2025-01-01T00:00:00Z", "category": "hardware"},
        {"product_id": "A2", "product_name": "Switch A2", "applicable_series": ["Series X"], "end_of_sale_date": "2020-01-01",
         "last_date_of_support": "2025-01-01", "category": "hardware"},
        {"product_id": "B1", "product_name": "Old B", "lifecycle_status": "end_of_life", "category": "hardware"},
    ]
    records = eolnetwork.to_records("acme", rows)
    assert len(records) == 2
    grouped = next(r for r in records if r["sku_count"] == 2)
    assert grouped["product"] == "Series X (EOS 2020-01-01, EOL 2025-01-01; 2 SKUs)" and grouped["label_alias"][:2] == ["A1", "A2"]
    standard = vendor_rows(records, source="eol.network")
    assert {r["is_eol"] for r in standard} == {"", "true"} and standard[0]["source"] == "eol.network"


def test_eolnetwork_fetch_skipped_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EOL_NETWORK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)                                  # no .env here
    eolnetwork.fetch(tmp_path / "snapshot")
    assert "skipped" in capsys.readouterr().out


def test_manual_csv_roundtrip(tmp_path):
    raw = tmp_path / "snapshot"
    raw.mkdir()
    (tmp_path / "manual").mkdir()
    (tmp_path / "manual" / "netapp.csv").write_text(
        "vendor,product,cycle,eol,aliases\nnetapp,FAS2240,FAS2240,2022-05-01,FAS2240-2;FAS2240-4\n", encoding="utf-8")
    manual.fetch(raw)
    (row,) = manual.standardize(raw)
    assert row["source"] == "manual:netapp.csv" and row["eol"] == "2022-05-01"
    assert row["aliases"].split("|")[:2] == ["FAS2240-2", "FAS2240-4"]


def test_cli_offline_writes_one_csv_per_source(tmp_path, monkeypatch):
    raw, out = tmp_path / "snapshot", tmp_path / "standard"
    raw.mkdir()
    (raw / "endoflife_full.json").write_text(json.dumps(ENDOFLIFE))
    (raw / "redhat_lifecycle.json").write_text(json.dumps(REDHAT))
    monkeypatch.setattr("sys.argv", ["ingest", "--raw", str(raw), "--out", str(out)])
    assert main() == 0
    with open(out / "endoflife.csv", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == STANDARD_COLUMNS
        assert len(list(reader)) == 2
    assert (out / "microsoft.csv").exists() and not (out / "security.csv").exists()


def test_eosldate_parses_tables_by_header():
    page = """
    <table><tr><th>Product</th><th>Model Number</th><th>Product Line</th><th>Release Date</th><th>End of Support</th></tr>
      <tr><td>Catalyst 2960X</td><td>WS-C2960X-24TS</td><td>Cisco Catalyst</td><td><time>Oct. 01, 2019</time></td><td>2027-10-31</td></tr>
      <tr><td>(adsbygoogle = window.adsbygoogle || []).push({});</td></tr></table>
    <table><tr><th>Product</th><th>Version Number</th><th>Release Date</th><th>End of Support</th></tr>
      <tr><td></td><td>Sonoma (macOS 14)</td><td>Sep. 26, 2023</td><td>Not Announced</td></tr></table>
    <table><tr><th>Product</th><th>Model Number</th><th>Product Line</th><th>End of Sale Date</th><th>End of Service Date</th></tr>
      <tr><td>AF20Q</td><td>AF20Q</td><td>All Flash Array</td><td>Jul. 31, 2023</td><td>Jul. 31, 2028 Support continues for 1 year</td></tr></table>
    <table><tr><th>Name</th><th>Colour</th></tr><tr><td>x</td><td>y</td></tr></table>"""
    cisco_row, mac_row, hpe_row = eosldate.parse_page(page, "cisco", "cisco")
    assert (cisco_row["eol"], cisco_row["release_date"], cisco_row["label_alias"]) == ("2027-10-31", "2019-10-01", ["WS-C2960X-24TS", "Cisco Catalyst"])
    assert (mac_row["product"], mac_row["eol"], mac_row["category"]) == ("Sonoma (macOS 14)", "", "software")
    assert (hpe_row["end_sale"], hpe_row["eol"]) == ("2023-07-31", "2028-07-31")
    assert vendor_rows([cisco_row], source=eosldate.SOURCE)[0]["source"] == "eosl.date"


def test_eoslai_groups_to_records():
    family = {
        "vendor": "Cisco", "line": "Nexus", "series": "9300 Series", "url": "https://eosl.ai/system/x/",
        "skuGroups": [
            {"label": "Switch hardware", "endOfSale": "2017-03", "endOfServiceLife": "2022-02", "eoslComputed": False,
             "exactDates": {"endOfServiceLife": {"date": "2022-02-28"}}, "sourceUrl": "https://cisco.example/eol",
             "skus": [{"partNumber": "N9K-M6PQ", "replacement": "N9K-M6PQ-E"}, {"partNumber": "N9K-M4PC"}]},
            {"label": "Policy-derived", "endOfServiceLife": "2030-06", "eoslComputed": True, "skus": []},
            {"label": "Current", "skus": [{"partNumber": "C9300-24T"}]},
            {"label": "Ended, no date", "vendorStatesSupportEnded": True, "skus": []},
        ]}
    exact, computed, current, ended = eoslai.to_records(family)
    assert exact["product"] == "Nexus 9300 Series" and exact["vendor"] == "cisco"
    assert (exact["eol"], exact["end_sale"], exact["source"]) == ("2022-02-28", "2017-03-31", "eosl.ai")
    assert exact["label_alias"] == ["9300 Series", "N9K-M6PQ", "N9K-M4PC"] and exact["url"] == "https://cisco.example/eol"
    assert (computed["eol"], computed["source"]) == ("2030-06-30", "eosl.ai:computed")
    assert (current["eol"], current["status"]) == ("", "") and ended["status"] == "obsolete"
    rows = vendor_rows([computed, ended], source=eoslai.SOURCE)
    assert rows[0]["origin"] == "eosl.ai:computed" and rows[1]["is_eol"] == "true"
    assert eoslai.group_day({"endOfSale": "2017-13"}, "endOfSale") == ""


def test_eoslai_failed_vendor_keeps_previous_records(tmp_path, monkeypatch):
    raw = tmp_path / "snapshot"
    raw.mkdir()
    old = [dict(vendor="cisco", product="Old", label_alias=[], cycle="Old", source="eosl.ai", eol="2020-01-01")]
    (raw / eoslai.FEED_FILE).write_text(json.dumps(old))
    index = json.dumps({"families": [{"vendor": "Cisco", "slug": "a", "detail": "https://x/a.json"}]}).encode()
    monkeypatch.setattr(eoslai, "get", lambda url, **kw: index if url.endswith("families.json") else (_ for _ in ()).throw(CollectorError("boom")))
    monkeypatch.setattr(eoslai, "PAUSE_SECONDS", 0)
    with pytest.raises(CollectorError):
        eoslai.fetch(raw)
    assert json.loads((raw / eoslai.FEED_FILE).read_text()) == old


CITRIX_DATA_TABLE = """<main><div><div class="table"><div class="data-table"><div><div><ol>
<li><h3>Product/Component Name</h3><ol><li>Citrix Virtual Apps and Desktops</li><li>Citrix Virtual Apps and Desktops</li></ol></li>
<li><h3><strong>Version/Model</strong></h3><ol><li>2203 LTSR</li><li>2402 LTSR</li></ol></li>
<li><h3>EOL</h3><ol><li>23-Mar-27</li><li>17-Aug-2029</li></ol></li>
<li><h3>EOES</h3><ol><li>For eligible customers*</li><li>15-Aug-31</li></ol></li>
</ol></div></div></div></div></div></main>"""

CITRIX_ROWS = """<main><div><div class="table">
<div><div><strong>Product</strong></div><div><strong>Version</strong></div><div><strong>Language</strong></div><div><strong>EOS*</strong></div><div><strong>EOL*</strong></div></div>
<div><div>NetScaler ADC</div><div>14.1(GA: 08-Aug-23)</div><div>EN</div><div>08-Aug-29</div><div>08-Aug-30</div></div>
<div><div>NetScaler ADC</div><div>14.1(GA: 08-Aug-23)</div><div>DE</div><div>08-Aug-29</div><div>08-Aug-30</div></div>
<div><div>uberAgent</div><div>8.x</div><div>EN</div><div>N/A</div><div>N/A</div></div>
</div></div></main>"""


def test_citrix_parse_dates():
    assert citrix.parse_date("15-Aug-22") == "2022-08-15"
    assert citrix.parse_date("15-Sept-21") == "2021-09-15"
    assert citrix.parse_date("13-01-2027") == "2027-01-13"
    assert citrix.parse_date("For eligible customers*") == citrix.parse_date("N/A") == ""


def test_citrix_column_layout():
    first, second = citrix.parse_fragment(CITRIX_DATA_TABLE, "citrix-virtual-apps")
    assert (first["cycle"], first["eol"], first["end_extended"]) == ("2203 LTSR", "2027-03-23", "")
    assert (second["eol"], second["end_extended"]) == ("2029-08-17", "2031-08-15")


def test_citrix_row_layout_one_row_per_language_and_release_date_from_cycle(tmp_path):
    (record,) = citrix.parse_fragment(CITRIX_ROWS, "netscaler-adc")        # DE duplicate and undated row dropped
    assert (record["cycle"], record["release_date"], record["end_sale"], record["eol"]) == (
        "14.1", "2023-08-08", "2029-08-08", "2030-08-08")
    (tmp_path / citrix.FEED_FILE).write_text(json.dumps([record]))
    (row,) = citrix.standardize(tmp_path)
    assert list(row) == STANDARD_COLUMNS and row["source"] == "citrix" and row["vendor"] == "citrix"
    assert row["eol"] == "2030-08-08" and "Citrix NetScaler ADC" in row["aliases"].split("|")


TRELLIX_SOFTWARE = """Trellix Product,Version Number,End-of-Life
Advanced Threat Defense,5.2.X,6/30/26
Advanced Threat Defense,"4.6, 4.4",12/30/21
Agent,5.0,—
"""
TRELLIX_HARDWARE = """Trellix Product,Model Number,End-of-Sale,End-of-Life,End-of-Support
Advanced Threat Defense,ATD 3100,12/31/20,12/31/26,12/31/26
Network Security,NS 5100,—,—,6/30/27
Email Security,EX 3500,—,—,—
"""


def test_trellix_software_splits_version_lists_and_drops_undated():
    records = trellix.parse_csv(TRELLIX_SOFTWARE, "software")
    assert [(r["cycle"], r["eol"]) for r in records] == [("5.2.X", "2026-06-30"), ("4.6", "2021-12-30"), ("4.4", "2021-12-30")]


def test_trellix_hardware_dates_and_rows(tmp_path):
    records = trellix.parse_csv(TRELLIX_HARDWARE, "hardware")
    first, second = records
    assert (first["end_sale"], first["end_support_full"], first["eol"]) == ("2020-12-31", "2026-12-31", "2026-12-31")
    assert second["eol"] == "2027-06-30"                 # no End-of-Life date: end of support stands in
    (tmp_path / trellix.FEED_FILE).write_text(json.dumps(records))
    rows = trellix.standardize(tmp_path)
    assert rows[0]["vendor"] == "trellix" and rows[0]["source"] == "trellix" and "ATD 3100" in rows[0]["aliases"].split("|")
