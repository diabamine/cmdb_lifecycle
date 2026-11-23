"""Standardize/parse tests with tiny inline fixtures (no network)."""
import csv
import json

from cmdb.ingest import apple, eolnetwork, endoflife, ibm, manual, microsoft, redhat
from cmdb.ingest.__main__ import main
from cmdb.ingest.common import iso, month_end, rec, vendor_rows
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
