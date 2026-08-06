"""Tests for the 定義語 / cross-reference extractor, against a hand-built statute.

The fixture law is fictional but drafted the way real statutes are: a （定義）
article using both the 各号 form and the 「X」とは form, inline
（以下「X」という。） definitions with and without a scope qualifier, absolute
and relative references, an external law reference with its 法令番号, a 準用
clause with a 読み替え instruction, and a 第二十四条 / 第二十四条の二 pair to keep
the citation matcher honest.
"""

import base64
import re
import xml.etree.ElementTree as ET

import pytest
from fastmcp import Client

import server
import xref
from tests import fixtures

LAW_NUM = "昭和二十二年法律第四十九号"  # 労基法, so _resolve_law needs no round-trip

XREF_LAW_XML = f"""<Law Era="Showa" Year="22" LawType="Act" Num="49">
  <LawNum>{LAW_NUM}</LawNum>
  <LawBody>
    <LawTitle>労働基準法</LawTitle>
    <MainProvision>
      <Article Num="1">
        <ArticleCaption>（目的）</ArticleCaption>
        <ArticleTitle>第一条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>この法律は、労働条件の最低基準を定めることを目的とする。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="2">
        <ArticleCaption>（定義）</ArticleCaption>
        <ArticleTitle>第二条</ArticleTitle>
        <Paragraph Num="1">
          <ParagraphSentence><Sentence>この法律において、次の各号に掲げる用語の意義は、当該各号に定めるところによる。</Sentence></ParagraphSentence>
          <Item Num="1">
            <ItemTitle>一</ItemTitle>
            <ItemSentence><Sentence>「労働者」　職業の種類を問わず、事業に使用される者で、賃金を支払われる者をいう。</Sentence></ItemSentence>
          </Item>
          <Item Num="2">
            <ItemTitle>二</ItemTitle>
            <ItemSentence><Sentence>「使用者」　事業主のために行為をするすべての者をいう。</Sentence></ItemSentence>
          </Item>
        </Paragraph>
        <Paragraph Num="2">
          <ParagraphSentence><Sentence>この法律において「賃金」とは、労働の対償として使用者が労働者に支払うすべてのものをいう。</Sentence></ParagraphSentence>
        </Paragraph>
      </Article>
      <Article Num="3">
        <ArticleCaption>（適用範囲）</ArticleCaption>
        <ArticleTitle>第三条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>使用者は、一週間の所定労働時間が特に短い労働者（以下この条において「特定短時間労働者」という。）については、別段の定めをすることができる。</Sentence></ParagraphSentence></Paragraph>
        <Paragraph Num="2"><ParagraphSentence><Sentence>使用者は、通常の賃金（以下「基本給」という。）を明示しなければならない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="9">
        <ArticleCaption>（周知義務）</ArticleCaption>
        <ArticleTitle>第九条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>使用者は、この法律の要旨を労働者に周知させなければならない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="10">
        <ArticleCaption>（適用の特例）</ArticleCaption>
        <ArticleTitle>第十条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>前条の規定にかかわらず、使用者は、第三条から第五条までの規定及び第二条第一項第二号の規定を適用する。</Sentence></ParagraphSentence></Paragraph>
        <Paragraph Num="2"><ParagraphSentence><Sentence>使用者は、第九条及び第二十四条に定める措置を講ずるほか、民法（明治二十九年法律第八十九号）第九十条の趣旨に従わなければならない。</Sentence></ParagraphSentence></Paragraph>
        <Paragraph Num="3"><ParagraphSentence><Sentence>前二項の規定は、次条に定める場合には、適用しない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="24">
        <ArticleCaption>（賃金の支払）</ArticleCaption>
        <ArticleTitle>第二十四条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>賃金は、通貨で、直接労働者に、その全額を支払わなければならない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="24_2">
        <ArticleCaption>（賃金の特例）</ArticleCaption>
        <ArticleTitle>第二十四条の二</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>賃金の支払の特例は、命令で定める。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="30">
        <ArticleCaption>（賃金台帳）</ArticleCaption>
        <ArticleTitle>第三十条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>使用者は、第二十四条の規定により支払つた賃金を賃金台帳に記入しなければならない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="31">
        <ArticleCaption>（特例の届出）</ArticleCaption>
        <ArticleTitle>第三十一条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>使用者は、第二十四条の二の命令で定めるところにより、届け出なければならない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="33">
        <ArticleCaption>（適用除外）</ArticleCaption>
        <ArticleTitle>第三十三条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>第二十条から第三十条までの規定は、非常災害の場合には、適用しない。</Sentence></ParagraphSentence></Paragraph>
      </Article>
      <Article Num="40">
        <ArticleCaption>（準用）</ArticleCaption>
        <ArticleTitle>第四十条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>第二十四条及び第三十条の規定は、前条に規定する労働者について準用する。この場合において、第二十四条中「賃金」とあるのは「報酬」と読み替えるものとする。</Sentence></ParagraphSentence></Paragraph>
      </Article>
    </MainProvision>
  </LawBody>
</Law>"""


@pytest.fixture
def egov_xref(requests_mock):
    """Serve the fixture statute for the whole-law fetch the xref tools make."""
    body = {
        "law_info": fixtures.LAW_INFO,
        "revision_info": fixtures.REVISION_INFO,
        "law_full_text": base64.b64encode(XREF_LAW_XML.encode("utf-8")).decode("ascii"),
    }
    requests_mock.get(re.compile(rf"{re.escape(server.EGOV_BASE)}/law_data/"), json=body)
    requests_mock.get(re.compile(rf"{re.escape(server.EGOV_BASE)}/laws"),
                      json=fixtures.LAWS_RESPONSE)
    return requests_mock


def terms(result):
    return [d["term"] for d in result["definitions"]]


def by_term(result, term):
    return next(d for d in result["definitions"] if d["term"] == term)


# --- numeral conversion -------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (1, "一"), (10, "十"), (11, "十一"), (24, "二十四"), (36, "三十六"),
    (100, "百"), (192, "百九十二"), (709, "七百九"), (1000, "千"),
])
def test_int_to_kanji(value, expected):
    assert xref._int_to_kanji(value) == expected
    assert server._kanji_to_int(expected) == value


def test_num_attr_to_kanji_article():
    assert xref._num_to_kanji_article("24") == "第二十四条"
    assert xref._num_to_kanji_article("24_2") == "第二十四条の二"


# --- definitions --------------------------------------------------------------

def test_definitions_from_kakugo_article(egov_xref):
    result = xref.get_law_definitions("労基法")
    assert result["found"] is True

    worker = by_term(result, "労働者")
    assert worker["definition_kind"] == "enumerated_item"
    assert worker["defined_in"]["article"] == "2"
    assert worker["defined_in"]["caption"] == "（定義）"
    assert worker["defined_in"]["item"] == 1
    assert "賃金を支払われる者をいう" in worker["definition_snippet"]
    assert worker["scope"] == "この法律"
    assert "使用者" in terms(result)


def test_definitions_from_towa_sentence(egov_xref):
    wage = by_term(xref.get_law_definitions("労基法"), "賃金")
    assert wage["definition_kind"] == "definition_article"
    assert wage["defined_in"]["article"] == "2"
    assert wage["defined_in"]["paragraph"] == 2
    assert "労働の対償" in wage["definition_snippet"]


def test_inline_definition_records_scope(egov_xref):
    result = xref.get_law_definitions("労基法")

    scoped = by_term(result, "特定短時間労働者")
    assert scoped["definition_kind"] == "inline"
    assert scoped["scope"] == "この条"
    assert scoped["defined_in"]["article"] == "3"
    assert "所定労働時間が特に短い労働者" in scoped["definition_snippet"]

    law_wide = by_term(result, "基本給")
    assert law_wide["scope"] == "この法律"


def test_definition_snippets_are_capped(egov_xref):
    for entry in xref.get_law_definitions("労基法")["definitions"]:
        assert len(entry["definition_snippet"]) <= xref.MAX_DEF_CHARS


def test_definition_term_filter_searches_whole_law(egov_xref):
    result = xref.get_law_definitions("労基法", term="労働者")
    assert terms(result) == ["労働者", "特定短時間労働者"]
    assert result["term_filter"] == "労働者"
    # The unfiltered count is still reported, so the agent knows what it missed.
    assert result["total_definitions_found"] > len(result["definitions"])


def test_definition_term_filter_miss_hints(egov_xref):
    result = xref.get_law_definitions("労基法", term="存在しない語")
    assert result["found"] is False
    assert result["definitions"] == []
    assert "hint" in result


def test_definitions_cap_at_max_terms(egov_xref, monkeypatch):
    monkeypatch.setattr(xref, "MAX_TERMS", 2)
    result = xref.get_law_definitions("労基法")
    assert len(result["definitions"]) == 2
    assert "note" in result


# --- outbound references ------------------------------------------------------

def refs_of(article, **kwargs):
    return xref.get_article_references("労基法", article, **kwargs)["references"]


def test_reference_range_and_article_with_paragraph_and_item(egov_xref):
    refs = refs_of("10")

    ranges = [r for r in refs if r["type"] == "internal_range"]
    assert len(ranges) == 1
    assert ranges[0]["target"]["article_from"] == "3"
    assert ranges[0]["target"]["article_to"] == "5"
    assert ranges[0]["source_text"] == "第三条から第五条まで"

    detailed = next(r for r in refs if r["target"].get("article") == "2")
    assert detailed["target"]["paragraph"] == 1
    assert detailed["target"]["item"] == 2
    assert detailed["source_paragraph"] == 1


def test_reference_list_yields_one_entry_per_member(egov_xref):
    refs = refs_of("10")
    para2 = [r["target"].get("article") for r in refs
             if r["source_paragraph"] == 2 and r["type"] == "internal"]
    assert para2 == ["9", "24"]


def test_relative_references_resolve_to_absolute_numbers(egov_xref):
    refs = refs_of("10")

    prev = next(r for r in refs if r.get("relative_expression") == "前条")
    assert prev["type"] == "relative_resolved"
    assert prev["resolved_from_relative"] is True
    assert prev["target"]["article"] == "9"          # 第10条 の前条 = 第九条
    assert prev["target"]["article_display"] == "第九条"

    nxt = next(r for r in refs if r.get("relative_expression") == "次条")
    assert nxt["target"]["article"] == "24"          # the next article in the law

    two_paras = next(r for r in refs if r.get("relative_expression") == "前二項")
    assert two_paras["resolved_from_relative"] is True
    assert [t["paragraph"] for t in two_paras["targets"]] == [1, 2]
    assert {t["article"] for t in two_paras["targets"]} == {"10"}


def test_relative_references_left_alone_when_disabled(egov_xref):
    refs = refs_of("10", resolve_relative=False)
    prev = next(r for r in refs if r.get("relative_expression") == "前条")
    assert prev["type"] == "relative"
    assert prev["resolved_from_relative"] is False
    assert "target" not in prev
    assert "note" in prev


def test_external_law_reference_carries_law_num(egov_xref):
    refs = refs_of("10")
    external = [r for r in refs if r["type"] == "external"]
    assert len(external) == 1
    assert external[0]["target"]["law"] == "民法"
    assert external[0]["target"]["law_num"] == "明治二十九年法律第八十九号"
    assert external[0]["target"]["article"] == "90"
    assert external[0]["source_text"].endswith("第九十条")

    # The external article number must not double as an internal reference.
    assert all(r["target"].get("article") != "90"
               for r in refs if r["type"] == "internal")


def test_junyo_clause_and_yomikae_flag(egov_xref):
    result = xref.get_article_references("労基法", "40")
    assert result["is_junyo_provision"] is True
    assert result["has_yomikae"] is True

    clause = result["junyo_clauses"][0]
    assert clause["source_provisions"] == "第二十四条及び第三十条"
    assert clause["applied_to"] == "前条に規定する労働者"
    assert clause["has_yomikae"] is True


def test_non_junyo_article_is_not_flagged(egov_xref):
    result = xref.get_article_references("労基法", "10")
    assert result["is_junyo_provision"] is False
    assert result["junyo_clauses"] == []
    assert result["has_yomikae"] is False


def test_references_report_missing_article(egov_xref):
    result = xref.get_article_references("労基法", "999")
    assert result["found"] is False
    assert "hint" in result


# --- reverse lookup -----------------------------------------------------------

def citing_nums(result):
    return [c["article"] for c in result["citing_articles"]]


def test_where_cited_matches_exact_article_only(egov_xref):
    result = xref.find_where_cited("労基法", "24")
    nums = citing_nums(result)
    assert "30" in nums          # 第二十四条の規定により
    assert "40" in nums          # 準用 clause
    assert "31" not in nums      # 第二十四条の二 must not count as 第二十四条
    assert result["cited_article"]["article_display"] == "第二十四条"


def test_where_cited_branch_article_is_distinct(egov_xref):
    nums = citing_nums(xref.find_where_cited("労基法", "第24条の2"))
    assert nums == ["31"]


def test_where_cited_accepts_kanji_input_and_reports_snippets(egov_xref):
    result = xref.find_where_cited("労基法", "第二十四条")
    entry = next(c for c in result["citing_articles"] if c["article"] == "30")
    assert entry["citations"][0]["source_text"] == "第二十四条"
    assert "賃金台帳" in entry["citations"][0]["snippet"]
    assert entry["caption"] == "（賃金台帳）"


def test_where_cited_finds_covering_range(egov_xref):
    entry = next(c for c in xref.find_where_cited("労基法", "24")["citing_articles"]
                 if c["article"] == "33")
    assert entry["match_kind"] == "range"
    assert entry["citations"][0]["source_text"] == "第二十条から第三十条まで"


def test_where_cited_excludes_the_article_itself(egov_xref):
    assert "24" not in citing_nums(xref.find_where_cited("労基法", "24"))


def test_where_cited_respects_max_results(egov_xref):
    result = xref.find_where_cited("労基法", "24", max_results=1)
    assert len(result["citing_articles"]) == 1
    assert result["citing_article_count"] >= 2


# --- registration -------------------------------------------------------------

def test_import_registers_nothing(egov_xref):
    """Importing the module must not attach tools to the server's own mcp."""
    names = {t.name for t in server.mcp._tool_manager._all_tools.values()} \
        if hasattr(server.mcp, "_tool_manager") else set()
    assert "get_law_definitions" not in names


async def test_register_exposes_three_tools():
    from fastmcp import FastMCP

    standalone = FastMCP("xref-test")
    xref.register(standalone)
    async with Client(standalone) as client:
        tools = {t.name: t for t in await client.list_tools()}

    assert {"get_law_definitions", "get_article_references",
            "find_where_cited"} <= set(tools)
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is True
        assert tool.annotations.title


# --- live ---------------------------------------------------------------------

@pytest.mark.live
def test_live_definitions_of_labour_contract_act():
    """労働契約法第二条 defines 労働者 and 使用者 in the 「Xとは」 form."""
    result = xref.get_law_definitions("労働契約法", term="労働者")
    assert result["found"] is True
    worker = next(d for d in result["definitions"] if d["term"] == "労働者")
    assert worker["defined_in"]["article"] == "2"
    assert "使用者に使用されて労働" in worker["definition_snippet"]


@pytest.mark.live
def test_live_references_of_labour_standards_act_article_37():
    """労基法第37条 (割増賃金) turns on 第三十三条 and 第三十六条."""
    result = xref.get_article_references("労働基準法", "37")
    assert result["found"] is True
    cited = {r["target"]["article"] for r in result["references"]
             if r["type"] in ("internal", "relative_resolved") and "target" in r}
    assert {"33", "36"} <= cited


@pytest.mark.live
def test_live_where_cited_article_36():
    """第三十六条 (三六協定) is leaned on from several other articles."""
    result = xref.find_where_cited("労働基準法", "36")
    assert result["found"] is True
    assert result["citing_article_count"] >= 2
    assert all("第三十六条" in c["citations"][0]["source_text"]
               or c["match_kind"] == "range"
               for c in result["citing_articles"])
