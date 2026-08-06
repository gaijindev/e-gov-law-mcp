"""Tests for the 新旧対照 (revision diff) module, against mocked 法令API v2.

The mock serves two hand-built versions of the fixture statute so every
classification branch (added / deleted / modified / unchanged) is exercised
without touching the network: 第1条 is untouched, 第32条 is reworded, 第36条 only
exists in the new version, 第40条 only in the old.
"""

import base64
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastmcp import Client

import revision_diff
import server
from tests import fixtures

LAW_ID = fixtures.LAW_ID
LAW_NUM = fixtures.LAW_NUM
OLD_REV = f"{LAW_ID}_20200401_501AC0000000071"
NEW_REV = f"{LAW_ID}_20260717_508AC0000000060"


def _article(num: str, title: str, caption: str, *sentences: str) -> str:
    body = "".join(
        f'<Paragraph Num="{i}"><ParagraphSentence><Sentence Num="1">{s}</Sentence>'
        "</ParagraphSentence></Paragraph>"
        for i, s in enumerate(sentences, start=1)
    )
    return (f'<Article Num="{num}"><ArticleCaption>{caption}</ArticleCaption>'
            f"<ArticleTitle>{title}</ArticleTitle>{body}</Article>")


ART_1 = _article("1", "第一条", "（労働条件の原則）",
                 "労働条件は、労働者が人たるに値する生活を営むためのものでなければならない。")
ART_32_OLD = _article("32", "第三十二条", "（労働時間）",
                      "使用者は、労働者に、休憩時間を除き一週間について四十時間を超えて、労働させてはならない。",
                      "使用者は、一日について八時間を超えて、労働させてはならない。")
ART_32_NEW = _article("32", "第三十二条", "（労働時間）",
                      "使用者は、労働者に、休憩時間を除き一週間について四十時間を超えて、労働させてはならない。",
                      "使用者は、一日について七時間を超えて、労働させてはならない。")
ART_36_NEW = _article("36", "第三十六条", "（時間外及び休日の労働）",
                      "時間外労働の上限は、月四十五時間及び年三百六十時間とする。")
ART_40_OLD = _article("40", "第四十条", "（労働時間及び休憩の特例）",
                      "別表第一に掲げる事業については、命令で別段の定めをすることができる。")


def _law(*articles: str) -> str:
    return (f'<Law Era="Showa" Year="22" LawType="Act" Num="49">'
            f"<LawNum>{LAW_NUM}</LawNum><LawBody>"
            f"<LawTitle>{fixtures.LAW_TITLE}</LawTitle>"
            f'<MainProvision><Chapter Num="1"><ChapterTitle>第一章</ChapterTitle>'
            f'{"".join(articles)}</Chapter></MainProvision>'
            f'<SupplProvision><SupplProvisionLabel>附　則</SupplProvisionLabel>'
            f'<Article Num="32"><ArticleTitle>第三十二条</ArticleTitle>'
            f"<Paragraph Num=\"1\"><ParagraphSentence><Sentence>附則の全く別の文言。"
            f"</Sentence></ParagraphSentence></Paragraph></Article></SupplProvision>"
            f"</LawBody></Law>")


OLD_XML = _law(ART_1, ART_32_OLD, ART_40_OLD)
NEW_XML = _law(ART_1, ART_32_NEW, ART_36_NEW)


def _response(xml: str, revision_id: str) -> dict:
    return {
        "law_info": fixtures.LAW_INFO,
        "revision_info": {**fixtures.REVISION_INFO, "law_revision_id": revision_id},
        "law_full_text": base64.b64encode(xml.encode("utf-8")).decode("ascii"),
    }


@pytest.fixture(autouse=True)
def clear_revision_cache():
    revision_diff.REVISIONS_CACHE.clear()
    yield


@pytest.fixture
def egov_two_versions(requests_mock):
    """Serve OLD_XML / NEW_XML depending on the revision id or ``asof`` asked for."""

    def law_data(request, context):
        parts = urlsplit(request.url)
        key = unquote(parts.path.rsplit("/", 1)[-1])
        asof = parse_qs(parts.query).get("asof", [""])[0]
        if key == OLD_REV or (asof and asof < "2025-01-01"):
            return _response(OLD_XML, OLD_REV)
        if key in (NEW_REV, LAW_ID, LAW_NUM):
            return _response(NEW_XML, NEW_REV)
        context.status_code = 404
        return fixtures.NOT_FOUND_BODY

    def laws(request, context):
        return fixtures.LAWS_RESPONSE

    base = re.escape(server.EGOV_BASE)
    requests_mock.get(re.compile(rf"{base}/law_data/"), json=law_data)
    requests_mock.get(re.compile(rf"{base}/law_revisions/"),
                      json=fixtures.REVISIONS_RESPONSE)
    requests_mock.get(re.compile(rf"{base}/laws"), json=laws)
    return requests_mock


# --- version-key parsing ------------------------------------------------------

def test_version_key_current_and_revision_id(egov_two_versions):
    assert revision_diff._version_key(LAW_ID, "current", "new") == (LAW_ID, "", "current")
    assert revision_diff._version_key(LAW_ID, "", "new") == (LAW_ID, "", "current")
    assert revision_diff._version_key(LAW_ID, OLD_REV, "old") == (OLD_REV, "", "law_revision_id")


def test_version_key_date(egov_two_versions):
    assert revision_diff._version_key(LAW_ID, "2015-01-01", "old") == \
        (LAW_ID, "2015-01-01", "asof")


def test_version_key_previous_skips_unenforced(egov_two_versions):
    """The newest revision is 未施行, so 'previous' must not be the second entry."""
    key, asof, how = revision_diff._version_key(LAW_ID, "previous", "old")
    assert (key, asof, how) == (f"{LAW_ID}_20200401_501AC0000000071", "", "previous")


def test_version_key_rejects_garbage():
    with pytest.raises(ValueError, match="not a version"):
        revision_diff._version_key(LAW_ID, "last year", "old")
    with pytest.raises(ValueError, match="not a version"):
        revision_diff._version_key(LAW_ID, "2015/01/01", "old")


def test_num_sort_key_orders_branch_articles():
    nums = ["100", "2", "24_2", "24", "29:31"]
    assert sorted(nums, key=revision_diff._num_sort_key) == \
        ["2", "24", "24_2", "29:31", "100"]


# --- classification -----------------------------------------------------------

def test_classifies_added_deleted_modified(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV)
    assert result["found"] is True
    assert result["identical"] is False
    assert result["summary"] == {
        "old_article_count": 3, "new_article_count": 3,
        "added": 1, "deleted": 1, "modified": 1, "unchanged": 1,
        "changed_total": 3,
    }
    by_num = {a["num"]: a for a in result["changed_articles"]}
    assert by_num["32"]["status"] == "modified"
    assert by_num["36"]["status"] == "added"
    assert by_num["40"]["status"] == "deleted"
    assert "八時間" in by_num["32"]["diff"] and "七時間" in by_num["32"]["diff"]
    assert by_num["32"]["diff"].startswith("--- old 第32条")
    assert "月四十五時間" in by_num["36"]["new_text"]
    assert "別表第一" in by_num["40"]["old_text"]
    # 第1条 is untouched and must not surface as a change.
    assert "1" not in by_num


def test_suppl_provision_articles_are_not_diffed(egov_two_versions):
    """附則第三十二条 differs from 本則第三十二条 but shares its Num."""
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV)
    assert "附則" not in result["changed_articles"][0].get("diff", "")
    assert result["summary"]["old_article_count"] == 3


def test_version_metadata_reported(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", "2015-01-01", "current")
    assert result["old_version"]["interpreted_as"] == "asof"
    assert result["old_version"]["asof"] == "2015-01-01"
    assert result["old_version"]["law_revision_id"] == OLD_REV
    assert result["new_version"]["interpreted_as"] == "current"
    assert result["new_version"]["law_revision_id"] == NEW_REV
    assert result["new_version"]["amendment_law_title"]
    assert result["new_version"]["amendment_enforcement_date"] == "2026-07-17"


def test_scope_all_lists_unchanged(egov_two_versions):
    changed = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV)
    assert "unchanged_article_nums" not in changed
    everything = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                     scope="all")
    assert everything["unchanged_article_nums"] == ["1"]


def test_scope_validated():
    with pytest.raises(ValueError, match="scope must be"):
        revision_diff.compare_law_revisions("労働基準法", OLD_REV, scope="everything")


def test_missing_law_name_rejected():
    with pytest.raises(ValueError, match="law_name_or_id is required"):
        revision_diff.compare_law_revisions("  ", OLD_REV)
    with pytest.raises(ValueError, match="old is required"):
        revision_diff.compare_law_revisions("労働基準法", "")


# --- identical versions -------------------------------------------------------

def test_identical_versions_say_so(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", NEW_REV, NEW_REV)
    assert result["identical"] is True
    assert result["changed_articles"] == []
    assert result["summary"]["changed_total"] == 0
    assert NEW_REV in result["message"]
    assert result["message"].count(NEW_REV) == 2


# --- single-article mode ------------------------------------------------------

def test_single_article_returns_full_texts(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                 article="第32条")
    art = result["article"]
    assert art["num"] == "32" and art["status"] == "modified"
    assert "八時間" in art["old"]["text"] and "七時間" in art["new"]["text"]
    assert art["old"]["article_caption"] == "（労働時間）"
    assert "+" in art["diff"]
    assert "changed_articles" not in result


def test_single_article_branch_number_and_added(egov_two_versions):
    added = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                article="36")
    assert added["article"]["status"] == "added"
    assert added["article"]["old"]["text"] is None
    assert added["article"]["diff"] is None

    unchanged = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                    article="第一条")
    assert unchanged["article"]["status"] == "unchanged"
    assert "identical" in unchanged["note"]


def test_single_article_not_found(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                 article="999")
    assert result["found"] is False
    assert "not in the 本則" in result["error"]


def test_single_article_unparseable(egov_two_versions):
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                 article="あ")
    assert result["found"] is False
    assert "Could not parse" in result["error"]


# --- truncation ---------------------------------------------------------------

def test_max_articles_paginates(egov_two_versions):
    first = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                max_articles=2)
    assert first["returned"] == 2
    assert first["truncated"] is True
    assert first["next_offset"] == 2
    assert "offset=2" in first["note"]

    second = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV,
                                                 max_articles=2, offset=2)
    assert second["returned"] == 1
    assert second["truncated"] is False
    assert second["changed_articles"][0]["num"] == "40"


def test_total_response_budget_caps_output(egov_two_versions, monkeypatch):
    monkeypatch.setattr(revision_diff, "MAX_TOTAL_CHARS", 50)
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV)
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert "character response budget" in result["note"]


def test_per_article_diff_capped(egov_two_versions, monkeypatch):
    monkeypatch.setattr(revision_diff, "MAX_DIFF_CHARS", 40)
    result = revision_diff.compare_law_revisions("労働基準法", OLD_REV, NEW_REV)
    diff = next(a for a in result["changed_articles"] if a["num"] == "32")["diff"]
    assert "diff truncated at 40 chars" in diff
    assert "article='32'" in diff


# --- registration -------------------------------------------------------------

async def test_wired_into_server():
    """server.py wires this module in via register(); the tool must be live.

    The module itself still must not self-register on import — that property
    is covered by test_registered_tool_has_annotations, which registers onto
    a fresh FastMCP instance.
    """
    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "compare_law_revisions" in names


async def test_registered_tool_has_annotations():
    from fastmcp import FastMCP

    fresh = FastMCP("probe")
    revision_diff.register(fresh)
    async with Client(fresh) as client:
        tool = next(t for t in await client.list_tools()
                    if t.name == "compare_law_revisions")
    assert tool.annotations.title == "Compare two versions of a law (新旧対照)"
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.openWorldHint is True


# --- live ---------------------------------------------------------------------

@pytest.mark.live
def test_live_working_style_reform_changed_article_36():
    """第36条 (時間外労働) gained the 上限規制 in the 働き方改革 reform of 2019.

    2017-04-01 is the earliest point-in-time date e-Gov serves.
    """
    result = revision_diff.compare_law_revisions(
        "労働基準法", "2017-04-01", "current", article="第36条")
    assert result["found"] is not False
    assert result["article"]["status"] == "modified"
    # The 上限規制 (45h/month, 360h/year) exists only in the current text.
    assert "四十五時間" in result["article"]["new"]["text"]
    assert "三百六十時間" in result["article"]["new"]["text"]
    assert "四十五時間" not in result["article"]["old"]["text"]
    assert result["old_version"]["law_revision_id"] != \
        result["new_version"]["law_revision_id"]


@pytest.mark.live
def test_live_asof_before_2017_reports_the_api_floor():
    result = revision_diff.compare_law_revisions("労働基準法", "2015-01-01", "current")
    assert result["found"] is False
    assert "2017-04-01" in result["hint"]


@pytest.mark.live
def test_live_summary_lists_36_as_modified():
    result = revision_diff.compare_law_revisions("労働基準法", "2017-04-01", "current")
    assert result["identical"] is False
    assert result["summary"]["modified"] > 0
    # 第36条 is among the changed articles even if the page is truncated.
    nums = [a["num"] for a in result["changed_articles"]]
    assert "36" in nums or result.get("truncated") is True
