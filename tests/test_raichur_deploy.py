"""Checks for hosting the Raichur copy next to the Kalaburagi one on one free Render database."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "complaint_tools"))


def test_two_portals_can_share_one_database_with_their_own_table(monkeypatch):
    from complaint_portal import durable   # pylint: disable=import-outside-toplevel
    monkeypatch.delenv("SNAPSHOT_TABLE", raising=False)
    assert durable._table_name() == "portal_snapshot"          # the default stays as it was
    monkeypatch.setenv("SNAPSHOT_TABLE", "raichur_snapshot")
    assert durable._table_name() == "raichur_snapshot"
    monkeypatch.setenv("SNAPSHOT_TABLE", "x; drop table y")
    with pytest.raises(ValueError):
        durable._table_name()


def test_the_blueprint_shares_the_existing_free_database_in_its_own_table():
    text = (ROOT / "render.yaml").read_text()
    assert "databases:" not in text and "kalaburagi-data" in text and "raichur_snapshot" in text
    assert "name: raichur-grievance-tracker" in text


def test_no_page_still_names_another_district_or_the_old_hackathon():
    leftovers = []
    for folder in ("portal",):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in (".html", ".js", ".webmanifest"):
                text = path.read_text(encoding="utf-8")
                if "Kalaburagi" in text or "Solvathon" in text or "Goa" in text or "Konkani" in text:
                    leftovers.append(str(path.relative_to(ROOT)))
    assert leftovers == []
