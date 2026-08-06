"""Tests for the e-Gov Law MCP server, against mocked 法令API v2 responses."""

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client

import server
from tests import fixtures


def elm_params(history):
    """Every ``elm`` value requested across a mocked exchange."""
    return [parse_qs(urlsplit(r.url).query).get("elm", [""])[0]
            for r in history if "/law_data/" in r.url]


# --- article reference parsing ------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("192", ("192", None, None)),
    ("第192条", ("192", None, None)),
    ("325条の3", ("325_3", None, None)),
    ("第二十四条の二", ("24_2", None, None)),
    ("第七百九条", ("709", None, None)),
    ("第9条第2項", ("9", 2, None)),
    ("第24条第1項第4号", ("24", 1, 4)),
    ("第三十二条第二項", ("32", 2, None)),
    ("１９２", ("192", None, None)),
    ("あ", (None, None, None)),
])
def test_article_to_num(text, expected):
    assert server._article_to_num(text) == expected


def test_kanji_to_int():
    assert server._kanji_to_int("百九十二") == 192
    assert server._kanji_to_int("七百九") == 709
    assert server._kanji_to_int("abc") is None


@pytest.mark.parametrize("value,expected", [
    ("322AC0000000049", True),
    ("322AC0000000049_20260717_508AC0000000060", True),
    ("昭和二十二年法律第四十九号", True),
    ("昭和二十一年憲法", True),
    ("労働基準法", False),
    ("個人情報", False),
])
def test_looks_like_law_key(value, expected):
    assert server._looks_like_law_key(value) is expected


def test_validate_asof_rejects_bad_dates():
    assert server._validate_asof("2020-04-01") == "2020-04-01"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        server._validate_asof("2020/04/01")


# --- find_law_article ---------------------------------------------------------

def test_find_article_uses_elm_path(egov):
    result = server.find_law_article("労働基準法", "32")
    assert result["found"] is True
    assert result["elm"] == "MainProvision-Article_32"
    assert elm_params(egov.request_history) == ["MainProvision-Article_32"]
    assert result["article_caption"] == "（労働時間）"
    assert "四十時間" in result["text"]
    assert result["revision"]["abbrev"] == "労基法"
    assert result["revision"]["current_revision_status"] == "CurrentEnforced"


def test_find_article_branch_number_elm(egov):
    result = server.find_law_article("労働基準法", "第24条の2")
    assert result["found"] is True
    assert result["elm"] == "MainProvision-Article_24_2"


def test_find_article_returns_paragraph_and_item(egov):
    result = server.find_law_article("労働基準法", "第32条第1項第2号")
    assert result["paragraph"] == 1
    assert "四十時間" in result["paragraph_text"]
    assert result["item"] == 2
    assert result["item_text"] == "二危険有害業務"


def test_find_article_suppl_provision(egov):
    """附則 article 32 must not be confused with main-body article 32."""
    result = server.find_law_article("労働基準法", "32", provision="suppl")
    assert result["found"] is True
    assert result["elm"] == "SupplProvision[1]"
    assert "この附則の規定は" in result["text"]
    assert "四十時間" not in result["text"]


def test_find_article_suppl_falls_back_to_later_blocks(egov):
    """An article only present in the amending law's 附則 needs the full scan."""
    result = server.find_law_article("労働基準法", "7", provision="suppl")
    assert result["found"] is True
    assert result["elm"] == "SupplProvision[2]"
    assert "経過措置" in result["text"]


def test_find_article_deleted_range_message(egov):
    result = server.find_law_article("労働基準法", "30")
    assert result["found"] is False
    assert result["deleted_range"] == "29:31"
    assert "29–31" in result["error"] and "削除" in result["error"]
    # elm is tried first, then the whole-law fetch resolves the merged range.
    assert elm_params(egov.request_history) == ["MainProvision-Article_30", ""]


def test_find_article_missing_hints_at_suppl_and_toc(egov):
    result = server.find_law_article("労働基準法", "999")
    assert result["found"] is False
    assert "provision='suppl'" in result["hint"]
    assert "get_law_content" in result["hint"]


def test_find_article_asof_is_passed_through(egov):
    server.find_law_article("労働基準法", "32", asof="2020-04-01")
    query = parse_qs(urlsplit(egov.request_history[0].url).query)
    assert query["asof"] == ["2020-04-01"]


def test_find_article_rejects_bad_provision():
    with pytest.raises(ValueError, match="provision"):
        server.find_law_article("労働基準法", "32", provision="appendix")


def test_find_article_unknown_law_suggests_search(egov):
    result = server.find_law_article("存在しない法", "1")
    assert result["found"] is False
    assert "search_laws" in result["hint"]


# --- get_law_element ----------------------------------------------------------

def test_get_law_element_appdx_table(egov):
    result = server.get_law_element(fixtures.LAW_ID, "AppdxTable[1]")
    assert result["found"] is True
    assert result["element_tag"] == "AppdxTable"
    assert "別表第一" in result["text"]
    assert result["truncated"] is False


def test_get_law_element_paginates(egov, monkeypatch):
    monkeypatch.setattr(server, "MAX_ELEMENT_CHARS", 10)
    result = server.get_law_element(fixtures.LAW_ID, "AppdxTable[1]")
    assert result["truncated"] is True
    assert result["returned_chars"] == 10
    assert "offset=10" in result["note"]


def test_get_law_element_missing_path(egov):
    result = server.get_law_element(fixtures.LAW_ID, "AppdxTable[9]")
    assert result["found"] is False
    assert "Article_24_2" in result["hint"]


def test_get_law_element_accepts_keyword_position(egov):
    """A /keyword hit position is an elm path, so it must round-trip verbatim."""
    hits = server.search_laws_by_keyword("休憩時間")
    position = hits["laws"][0]["matched_sentences"][0]["position"]
    article_elm = position.rsplit("-Paragraph_", 1)[0]
    result = server.get_law_element(fixtures.LAW_ID, article_elm)
    assert result["found"] is True
    assert result["element_tag"] == "Article"


# --- revisions ----------------------------------------------------------------

def test_get_law_revisions_lists_all(egov):
    result = server.get_law_revisions("労働基準法")
    assert result["revision_count"] == 3
    assert result["revisions"][0]["current_revision_status"] == "UnEnforced"
    assert result["revisions"][0]["abbrev"] == "労基法"


def test_get_law_revisions_unenforced_only(egov):
    result = server.get_law_revisions("労働基準法", unenforced_only=True)
    assert result["revision_count"] == 1
    rev = result["revisions"][0]
    assert rev["amendment_scheduled_enforcement_date"] == "2028-12-23"
    assert "民法等の一部を改正する法律" in rev["amendment_enforcement_comment"]


# --- content / TOC ------------------------------------------------------------

def test_get_law_content_toc_hierarchy(egov):
    out = server.get_law_content(fixtures.LAW_ID)
    chapters = out["table_of_contents"]
    assert [c["title"] for c in chapters] == ["第一章　総則",
                                              "第四章　労働時間、休憩、休日及び年次有給休暇"]
    assert chapters[0]["level_ja"] == "章"
    assert chapters[0]["article_range"] == "1–29:31"
    assert out["suppl_provision_count"] == 2
    assert out["appdx_tables"] == ["別表第一"]
    assert out["revision"]["abbrev"] == "労基法"
    # 附則 articles must not inflate the main-body count.
    assert out["article_count"] == 3


# --- search tools -------------------------------------------------------------

def test_search_laws_passes_optional_filters(egov):
    server.search_laws(law_title="労働", category_cd="27", order="-revision_info.amendment_promulgate_date",
                       promulgation_date_from="1947-01-01", asof="2020-04-01")
    query = parse_qs(urlsplit(egov.request_history[0].url).query)
    assert query["category_cd"] == ["27"]
    assert query["order"] == ["-revision_info.amendment_promulgate_date"]
    assert query["promulgation_date_from"] == ["1947-01-01"]
    assert query["asof"] == ["2020-04-01"]


def test_search_laws_surfaces_revision_metadata(egov):
    out = server.search_laws(law_title="労働基準法")
    assert out["laws"][0]["abbrev"] == "労基法"
    assert out["laws"][0]["law_revision_id"].startswith(fixtures.LAW_ID)


def test_keyword_search_defaults(egov):
    out = server.search_laws_by_keyword("休憩時間")
    query = parse_qs(urlsplit(egov.request_history[0].url).query)
    assert query["limit"] == ["50"]
    assert query["sentences_limit"] == ["5"]
    assert query["sentence_text_size"] == ["100"]
    assert "get_law_element" in out["position_usage"]
    sentences = out["laws"][0]["matched_sentences"]
    assert sentences[0]["position"] == "MainProvision-Article_32-Paragraph_1"
    assert "<span>" not in sentences[0]["text"]


def test_keyword_search_requires_keyword():
    with pytest.raises(ValueError, match="keyword"):
        server.search_laws_by_keyword("  ")


# --- ChatGPT Deep Research compatibility --------------------------------------

def test_search_returns_addressable_ids(egov):
    results = server.search("休憩時間")["results"]
    assert results[0]["id"] == f"{fixtures.LAW_ID}::MainProvision-Article_32-Paragraph_1"
    assert results[0]["url"] == f"https://laws.e-gov.go.jp/law/{fixtures.LAW_ID}"


def test_search_fetch_round_trip(egov):
    result = server.search("休憩時間")["results"][0]
    # Trim the paragraph suffix to address the whole article, as documented.
    article_id = result["id"].rsplit("-Paragraph_", 1)[0]
    doc = server.fetch(article_id)
    assert doc["id"] == article_id
    assert doc["metadata"]["element_tag"] == "Article"
    assert "四十時間" in doc["text"]


def test_fetch_bare_law_id_returns_toc(egov):
    doc = server.fetch(fixtures.LAW_ID)
    assert doc["title"] == fixtures.LAW_TITLE
    assert doc["metadata"]["appdx_tables"] == ["別表第一"]
    assert doc["metadata"]["abbrev"] == "労基法"
    assert doc["text"].startswith("労働基準法")


def test_fetch_unknown_element_errors(egov):
    with pytest.raises(ValueError, match="Re-run search"):
        server.fetch(f"{fixtures.LAW_ID}::AppdxTable[9]")


# --- MCP surface --------------------------------------------------------------

async def test_every_tool_has_title_and_read_only_hint():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    assert tools, "no tools registered"
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.title, tool.name
        assert tool.annotations.readOnlyHint is not None, tool.name
        assert tool.description, tool.name


async def test_admin_tools_hidden_by_default():
    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"search_laws", "search_laws_by_keyword", "find_law_article",
                     "get_law_element", "get_law_revisions", "get_law_content",
                     "search", "fetch"}


async def test_admin_tools_registered_when_enabled(monkeypatch):
    monkeypatch.setenv("LAW_MCP_ADMIN_TOOLS", "1")
    admin_server = importlib.reload(server)
    try:
        async with Client(admin_server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert {"get_cache_stats", "clear_cache", "list_saved_laws",
                "read_saved_law"} <= set(tools)
        clear = tools["clear_cache"].annotations
        assert clear.readOnlyHint is False
        assert clear.destructiveHint is True
        assert clear.idempotentHint is True
        assert tools["list_saved_laws"].annotations.openWorldHint is False
    finally:
        monkeypatch.delenv("LAW_MCP_ADMIN_TOOLS")
        importlib.reload(server)


async def test_tool_call_through_mcp_client(egov):
    async with Client(server.mcp) as client:
        result = await client.call_tool("find_law_article",
                                        {"law_name": "労働基準法", "article": "32"})
    assert result.data["found"] is True
    assert result.data["elm"] == "MainProvision-Article_32"


# --- HTTP client hardening ----------------------------------------------------

def test_session_sends_user_agent(egov):
    server.search_laws(law_title="労働基準法")
    assert "e-gov-law-mcp" in egov.request_history[0].headers["User-Agent"]


def test_session_retries_on_server_error():
    adapter = server._SESSION.get_adapter("https://laws.e-gov.go.jp")
    retry = adapter.max_retries
    assert retry.total == 3
    assert retry.backoff_factor == 1
    assert 429 in retry.status_forcelist and 503 in retry.status_forcelist
