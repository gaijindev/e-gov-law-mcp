"""Live smoke tests against the real e-Gov 法令API.

Skipped by default (``addopts = -m "not live"``); run with ``pytest -m live``.
Kept to a handful of requests — the API asks callers to avoid bursts.
"""

import pytest

import server

pytestmark = pytest.mark.live


def test_live_article_via_elm():
    result = server.find_law_article("労働基準法", "32")
    assert result["found"] is True
    assert result["elm"] == "MainProvision-Article_32"
    assert "四十時間" in result["text"]
    assert result["revision"]["abbrev"] == "労基法"


def test_live_appdx_table_of_immigration_act():
    """入管法 別表第一 is the visa-status list the bundled skill needs."""
    result = server.get_law_element("326CO0000000319", "AppdxTable[1]")
    assert result["found"] is True
    assert "別表第一" in result["text"]
    assert "留学" in result["text"]


def test_live_revisions_include_status():
    result = server.get_law_revisions("労働基準法")
    assert result["revision_count"] > 0
    statuses = {r["current_revision_status"] for r in result["revisions"]}
    assert "CurrentEnforced" in statuses
