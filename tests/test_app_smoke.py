"""Smoke tests: the app runs end to end on a tiny catalog and CMDB file, with the real matcher (no fakes)."""
import io
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.delta_generator import DeltaGenerator
from streamlit.testing.v1 import AppTest

from cmdb.catalog import write_catalog
from cmdb.schema import STANDARD_COLUMNS

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
CMDB = "vendor,product,version\nPython Software Foundation,Python,3.8.10\nPSF,Python,3.11.2\nAcme,Homegrown Tool,1.0\n"
CUSTOM = "vendor,product,version,eol\nPSF,Python,3.8,2030-01-01\n"


def catalog_row(version, eol):
    row = dict.fromkeys(STANDARD_COLUMNS, "")
    row.update(id=f"eol:python@{version}", product_id="eol:python", source="endoflife.date", vendor="psf",
               product="Python", category="lang", version=version, eol=eol)
    return row


class FakeUpload:
    def __init__(self, name, text):
        self.name = name
        self._data = text.encode()

    def getvalue(self):
        return self._data


@pytest.fixture
def app(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    write_catalog([catalog_row("3.8", "2024-10-07"), catalog_row("3.11", "2027-10-24")], tmp_path / "data" / "master_catalog.csv")
    (tmp_path / "cmdb_extract.csv").write_text(CMDB)
    monkeypatch.chdir(tmp_path)
    return AppTest.from_file(APP, default_timeout=60)


def test_default_matches_sample_against_master(app):
    app.run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Matches", "Metrics", "Compare catalogs", "Explore"]
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Rows"] == "3"
    assert metrics["Matched"] == "66.7%"          # the in-house tool stays unmatched
    assert metrics["EOL date found"] == "100.0%"


def test_missing_catalog_shows_error(app, tmp_path):
    (tmp_path / "data" / "master_catalog.csv").unlink()
    app.run()
    assert "python -m cmdb.ingest" in app.error[0].value


def test_custom_catalog_is_compared_with_master(app, monkeypatch):
    original = DeltaGenerator.file_uploader

    def fake_uploader(self, label, *args, **kwargs):
        # AppTest cannot upload files: answer only the custom catalog uploader
        if "Custom catalog" in label:
            return [FakeUpload("mine.csv", CUSTOM)]
        return original(self, label, *args, **kwargs)

    monkeypatch.setattr(DeltaGenerator, "file_uploader", fake_uploader)
    app.run()
    assert not app.exception
    labels = [m.label for m in app.metric]
    assert "All agree" in labels and "Conflict" in labels


def test_explore_filters_the_input_file_by_vendor_product_version(app):
    app.run()
    # Matches tab and Explore tab each show a "Rows" metric: 3 input rows before any search
    assert [m.value for m in app.metric if m.label == "Rows"] == ["3", "3"]
    app.text_input(key="explore_product").set_value("python").run()
    assert not app.exception
    assert [m.value for m in app.metric if m.label == "Rows"] == ["3", "2"]      # only the two Python rows remain
    app.text_input(key="explore_version").set_value("3.11").run()
    assert [m.value for m in app.metric if m.label == "Rows"] == ["3", "1"]
    app.text_input(key="explore_product").set_value("zzz-nothing").run()
    assert not app.exception
    assert "No input row" in app.info[-1].value or "Nothing in this catalog" in app.info[0].value


def test_explore_lookup_lists_every_cycle_for_a_partial_version(app):
    app.run()
    app.text_input(key="explore_product").set_value("python").run()
    app.text_input(key="explore_version").set_value("3").run()
    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Cycles fitting"] == "2"        # 3.8 and 3.11 both start with 3


def test_explore_detects_version_in_product_name(app):
    app.run()
    app.text_input(key="explore_product").set_value("Python 3.11").run()
    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Cycles fitting"] == "1"
    assert any("detected in the product name" in c.value for c in app.caption)
