"""Tests for the English-translation tools, against mocked JLT + e-Gov responses.

The Japanese Law Translation site has no API, so the fixtures below are trimmed
copies of its real markup: a CSRF-bearing search form, a search result list, a
bilingual ``/en/laws/view/{id}/je`` page and the Standard Legal Terms Dictionary
CSV. Live tests at the bottom hit the real site and are skipped by default.
"""

import re

import pytest
from fastmcp import FastMCP

import english
import server

JLT = english.JLT_BASE

# --- fixtures -----------------------------------------------------------------

CSRF = "TESTTOKEN=="

SEARCH_FORM_HTML = f"""<html><body>
<form action="/en/laws/result/" method="post">
  <input type="hidden" name="_csrfToken" autocomplete="off" value="{CSRF}">
  <input id="yo" class="keyword-input" type="text" name="yo" value="" maxlength="150">
</form></body></html>"""

SEARCH_RESULT_HTML = """<html><body><main>
<div class="result-number"><span class="label">Results: </span>Showing 1 to 2 of 2</div>
<ul class="search-result">
  <li>
    <div class="result-title">
      <div class="result-law-type">Act</div>
      <div class="result-title-text">
        <a href="/en/laws/view/4913" target="_blank" rel="noopener noreferrer">
          Labor Standards Act        </a>
      </div>
    </div>
    <div class="result-info">
      <div class="result-info-item">Law Number: Act No. 49 of 1947</div>
      <div class="result-info-item">Translated Date: August 30, 2024</div>
      <div class="result-info-item">Dictionary Version: 17.0</div>
    </div>
  </li>
  <li>
    <div class="result-title">
      <div class="result-law-type">Ministerial Order</div>
      <div class="result-title-text">
        <a href="/en/laws/view/2605" target="_blank" rel="noopener noreferrer">
          Ordinance for Enforcement of the Labor Standards Act        </a>
      </div>
    </div>
    <div class="result-info">
      <div class="result-info-item">Law Number: Ordinance of the Ministry of Health and Welfare No. 23 of 1947</div>
      <div class="result-info-item">Translated Date: December 14, 2012</div>
      <div class="result-info-item">Dictionary Version: 7.0</div>
    </div>
  </li>
</ul>
</main></body></html>"""

EMPTY_RESULT_HTML = """<html><body><main>
<div class="result-number"><span class="label">Results: </span>Showing 0 to 0 of 0</div>
<ul class="search-result"></ul>
</main></body></html>"""

LAW_VIEW_HTML = """<html><head><title>Labor Standards Act</title></head><body><main>
<div class="law-contents">
  <div id="lawInfo" class="law-info">
    <div class="title">労働基準法（昭和二十二年法律第四十九号）</div>
    <div class="title">Labor Standards Act（Act No. 49 of 1947）</div>
    <div class="last-version"><span class="label">最終更新：</span>令和二年法律第十三号</div>
    <div class="last-version"><span class="label">Last Version： </span>Act No. 13 of 2020</div>
  </div>
  <div id="lawBody" class="law-body">
    <div class="Laws">
      <div class="LawBody anchor" id="je_lb">
        <div class="Chapter anchor" id="je_ch3">
          <div class="ChapterTitle">第三章　賃金</div>
          <div class="ChapterTitle">Chapter III Wages</div>
          <div class="Article anchor" id="je_ch3at6">
            <div class="Paragraph">
              <div class="ParagraphSentence"><span class="ArticleTitle">第二十九条から第三十一条まで</span>削除</div>
              <div class="ParagraphSentence"><span class="ArticleTitle">Articles 29 through 31</span>Deleted</div>
            </div>
          </div>
        </div>
        <div class="Chapter anchor" id="je_ch4">
          <div class="ChapterTitle">第四章　労働時間、休憩、休日及び年次有給休暇</div>
          <div class="ChapterTitle">Chapter IV Working Hours, Breaks, Days Off, and Annual Paid Leave</div>
          <div class="Article anchor" id="je_ch4at1">
            <div class="ArticleCaption">（労働時間）</div>
            <div class="ArticleCaption">(Working Hours)</div>
            <div class="Paragraph">
              <div class="ParagraphSentence"><span class="ArticleTitle">第三十二条</span>使用者は、労働者に、休憩時間を除き一週間について四十時間を超えて、労働させてはならない。</div>
              <div class="ParagraphSentence"><span class="ArticleTitle">Article 32</span><span class="ParagraphNum">(1)</span>An employer must not have workers work more than 40 hours per week, excluding break time.</div>
            </div>
            <div class="Paragraph">
              <div class="ParagraphSentence"><span class="ParagraphNum">２</span>使用者は、一週間の各日については、労働者に、休憩時間を除き一日について八時間を超えて、労働させてはならない。</div>
              <div class="ParagraphSentence"><span class="ParagraphNum">(2)</span>An employer must not have workers work more than 8 hours per day for each day of the week, excluding break time.</div>
            </div>
          </div>
          <div class="Article anchor" id="je_ch4at2">
            <div class="Paragraph">
              <div class="ParagraphSentence"><span class="ArticleTitle">第三十二条の二</span>使用者は、書面による協定により、一箇月以内の期間を平均し労働させることができる。</div>
              <div class="ParagraphSentence"><span class="ArticleTitle">Article 32-2</span><span class="ParagraphNum">(1)</span>An employer may, pursuant to a written agreement, have a worker work on average over a period of not more than one month.</div>
              <div class="Item">
                <div class="ItemSentence"><span class="ItemTitle">一</span>賃金が、労働した日によつて算定された場合</div>
                <div class="ItemSentence"><span class="ItemTitle">(i)</span>if wages are calculated on the basis of days worked;</div>
              </div>
            </div>
          </div>
        </div>
        <div class="SupplProvision anchor" id="je_s1">
          <div class="SupplProvisionLabel"><span>附　則　〔抄〕</span><br><span>Supplementary Provisions [Extract]</span></div>
          <div class="Article anchor" id="je_s1at1">
            <div class="Paragraph">
              <div class="ParagraphSentence"><span class="ArticleTitle">第百二十二条</span>この法律施行の期日は、勅令で、これを定める。</div>
              <div class="ParagraphSentence"><span class="ArticleTitle">Article 122</span>The effective date of this Act is specified by Imperial Order.</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
</main></body></html>"""

DICT_PAGE_HTML = f"""<html><body>
<form method="post">
  <input type="hidden" name="_csrfToken" autocomplete="off" value="{CSRF}">
  <select id="version" name="version">
    <option value="19.0" selected>19.0 (newest)</option>
    <option value="18.0">18.0</option>
  </select>
  <select id="ftype" name="ftype"><option value="4">CSV(UTF-8)</option></select>
</form></body></html>"""

DICT_CSV = (
    "用語,読み,訳語候補番号,訳語候補,使い分け基準,用例(和文),用例(英文),用例出典,注釈1,注釈2\n"
    "善意,ぜんい,1,good faith,一般的な場合,善意の第三者,a third party in good faith,民法第94条,,\n"
    "善意,ぜんい,2,without knowledge,知らないことを指す場合,善意で占有する,possess without knowledge,民法第189条,,\n"
    "労働者,ろうどうしゃ,1,worker,,労働者に労働させてはならない,must not have workers work,労働基準法第32条,,\n"
    "使用者,しようしゃ,1,employer,労働法の場合,使用者は、,an employer must,労働基準法第32条,,\n"
)

EGOV_LAWS_RESPONSE = {
    "total_count": 1,
    "laws": [{
        "law_info": {"law_id": "322AC0000000049",
                     "law_num": "昭和二十二年法律第四十九号"},
        "revision_info": {"law_title": "労働基準法",
                          "amendment_law_num": "令和八年法律第六十号",
                          "amendment_enforcement_date": "2026-07-17",
                          "current_revision_status": "CurrentEnforced"},
    }],
}


# --- wiring -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_caches():
    """Caches and the CSRF token are module-global; tests must not share them."""
    for cache in (english.CATALOGUE_CACHE, english.LAW_PAGE_CACHE,
                  english.DICT_CACHE, server.RESOLVE_LAW_CACHE):
        cache.clear()
    english._csrf_tokens.clear()
    yield


@pytest.fixture
def jlt(requests_mock):
    """Serve the JLT fixtures, rejecting POSTs that carry no CSRF token."""

    def result(request, context):
        if "_csrfToken" not in (request.text or ""):
            context.status_code = 403
            return "forbidden"
        query = re.search(r"yo=([^&]*)", request.text or "")
        if query and "nosuch" in query.group(1).lower():
            return EMPTY_RESULT_HTML
        return SEARCH_RESULT_HTML

    def dict_download(request, context):
        if "_csrfToken" not in (request.text or ""):
            context.status_code = 403
            return "forbidden"
        return DICT_CSV

    requests_mock.get(f"{JLT}/en/laws", text=SEARCH_FORM_HTML)
    requests_mock.post(f"{JLT}/en/laws/result/", text=result)
    requests_mock.get(re.compile(re.escape(f"{JLT}/en/laws/view/")), text=LAW_VIEW_HTML)
    requests_mock.get(f"{JLT}/en/dicts/download", text=DICT_PAGE_HTML)
    requests_mock.post(f"{JLT}/en/dicts/download", text=dict_download)
    def egov_laws(request, context):
        # Title lookups only resolve the fixture statute; number lookups (the
        # staleness check) always do.
        title = re.search(r"law_title=([^&]*)", request.url)
        if title and "%E5%8A%B4%E5%83%8D" not in title.group(1):
            return {"total_count": 0, "laws": []}
        return EGOV_LAWS_RESPONSE

    requests_mock.get(re.compile(re.escape(f"{server.EGOV_BASE}/laws")),
                      json=egov_laws)
    return requests_mock


@pytest.fixture
def tools():
    """The three tools, registered on a throwaway FastMCP instance."""
    return english.register(FastMCP("test"))


# --- HTML parsing -------------------------------------------------------------

def test_dom_builder_nests_and_skips_scripts():
    root = english._parse_html(
        '<div class="a"><script>var x = "<div>";</script>'
        '<span class="b">hi</span><br>there</div>')
    outer = root.find_all("a")[0]
    assert outer.tag == "div"
    assert [n.tag for n in outer.elements()] == ["span"]
    assert outer.text() == "hithere"


def test_dom_builder_ignores_stray_end_tags():
    root = english._parse_html("<div class='a'>x</p></div>")
    assert root.find("a").text() == "x"


@pytest.mark.parametrize("text,japanese", [
    ("使用者は、労働者に、", True),
    ("削除", True),
    ("An employer must not have workers work more than 40 hours per week.", False),
    ("Minimum standards are as prescribed in the Minimum Wage Act (Act No. 137).", False),
    ("", False),
])
def test_is_japanese(text, japanese):
    assert english._is_japanese(text) is japanese


def test_lines_separates_markers_from_sentence_text():
    root = english._parse_html(LAW_VIEW_HTML)
    article = root.find_by_id("je_ch4at1")
    lines = english._lines(article)
    assert lines[0] == "（労働時間）"
    assert lines[1] == "(Working Hours)"
    assert lines[2].startswith("第三十二条 使用者は、")
    assert lines[3].startswith("Article 32 (1) An employer must not")


def test_split_bilingual_keeps_items_with_their_language():
    root = english._parse_html(LAW_VIEW_HTML)
    ja, en = english._split_bilingual(root.find_by_id("je_ch4at2"))
    assert "第三十二条の二" in ja
    assert "一 賃金が、労働した日によつて算定された場合" in ja
    assert "Article 32-2" in en
    assert "(i) if wages are calculated on the basis of days worked;" in en
    assert "賃金" not in en


# --- article numbering --------------------------------------------------------

@pytest.mark.parametrize("title,key,rng", [
    ("第三十二条", "32", None),
    ("第三十二条の二", "32_2", None),
    ("第百二十二条", "122", None),
    ("第二十九条から第三十一条まで", None, (29, 31)),
    ("", None, None),
])
def test_article_key_from_ja(title, key, rng):
    assert english._article_key_from_ja(title) == (key, rng)


@pytest.mark.parametrize("title,key,rng", [
    ("Article 32", "32", None),
    ("Article 32-2", "32_2", None),
    ("Articles 29 through 31", None, (29, 31)),
    ("Supplementary Provisions", None, None),
])
def test_article_key_from_en(title, key, rng):
    assert english._article_key_from_en(title) == (key, rng)


@pytest.mark.parametrize("wanted,key,rng,matched", [
    ("32", "32", None, True),
    ("32", "32_2", None, False),
    ("32_2", "32_2", None, True),
    ("30", None, (29, 31), True),
    ("32", None, (29, 31), False),
    ("32_2", None, (29, 31), False),
])
def test_key_matches(wanted, key, rng, matched):
    assert english._key_matches(wanted, key, rng) is matched


# --- law page parsing ---------------------------------------------------------

def test_parse_law_page_titles_and_version():
    law = english._parse_law_page(LAW_VIEW_HTML)
    assert law["law_title_ja"] == "労働基準法（昭和二十二年法律第四十九号）"
    assert law["law_title_en"] == "Labor Standards Act（Act No. 49 of 1947）"
    assert law["translated_version_ja"] == "令和二年法律第十三号"
    assert law["translated_version_en"] == "Act No. 13 of 2020"


def test_parse_law_page_table_of_contents():
    toc = english._parse_law_page(LAW_VIEW_HTML)["table_of_contents"]
    assert [c["title_en"] for c in toc] == [
        "Chapter III Wages",
        "Chapter IV Working Hours, Breaks, Days Off, and Annual Paid Leave",
    ]
    assert toc[0]["level_ja"] == "章"
    assert toc[1]["article_range"] == "32–32_2"


def test_parse_law_page_articles_and_sections():
    articles = english._parse_law_page(LAW_VIEW_HTML)["articles"]
    keys = [a["article_key"] for a in articles]
    assert keys == [None, "32", "32_2", "122"]
    assert articles[0]["article_range"] == [29, 31]
    assert articles[1]["article_caption_en"] == "(Working Hours)"
    assert articles[1]["section"] == "MainProvision"
    assert articles[3]["section"] == "SupplProvision"


# --- search parsing -----------------------------------------------------------

def test_parse_search_results():
    results = english._parse_search_results(SEARCH_RESULT_HTML)
    assert [r["jlt_id"] for r in results] == ["4913", "2605"]
    assert results[0] == {
        "jlt_id": "4913",
        "english_title": "Labor Standards Act",
        "url": f"{JLT}/en/laws/view/4913",
        "law_type": "Act",
        "law_num_en": "Act No. 49 of 1947",
        "translated_date": "August 30, 2024",
        "dictionary_version": "17.0",
    }
    assert english._parse_result_total(SEARCH_RESULT_HTML) == 2


# --- candidate ranking --------------------------------------------------------

@pytest.mark.parametrize("law_num,signature", [
    ("昭和二十二年法律第四十九号", (49, 1947)),
    ("昭和二十六年政令第三百十九号", (319, 1951)),
    ("明治二十九年法律第八十九号", (89, 1896)),
    ("令和四年法律第百二号", (102, 2022)),
    ("昭和二十一年憲法", None),
    ("", None),
])
def test_ja_law_num_signature(law_num, signature):
    assert english._ja_law_num_signature(law_num) == signature


@pytest.mark.parametrize("law_num_en,signature", [
    ("Cabinet Order No. 319 of 1951", (319, 1951)),
    ("Act No. 49 of 1947", (49, 1947)),
    ("", None),
])
def test_en_law_num_signature(law_num_en, signature):
    assert english._en_law_num_signature(law_num_en) == signature


def test_rank_candidates_prefers_the_law_itself():
    """The 入管法 case: the statute is a 政令, and outlines have no date."""
    candidates = [
        {"english_title": "Regulation for Enforcement of the Immigration Control Act",
         "law_num_en": "Ministry of Justice Order No. 54 of 1981",
         "translated_date": "September 31, 2021"},
        {"english_title": "Act Partially Amending the Immigration Control Act",
         "law_num_en": "", "translated_date": ""},
        {"english_title": "Immigration Control and Refugee Recognition Act",
         "law_num_en": "Cabinet Order No. 319 of 1951",
         "translated_date": "March 31, 2020"},
    ]
    ranked = english._rank_candidates(candidates, "昭和二十六年政令第三百十九号")
    assert ranked[0]["law_num_en"] == "Cabinet Order No. 319 of 1951"
    assert ranked[-1]["translated_date"] == ""


def test_rank_candidates_demotes_untranslated_without_a_law_num():
    candidates = [
        {"english_title": "Outline", "law_num_en": "", "translated_date": ""},
        {"english_title": "Some Longer Act Title", "law_num_en": "Act No. 1 of 2000",
         "translated_date": "May 1, 2020"},
    ]
    ranked = english._rank_candidates(candidates, None)
    assert ranked[0]["english_title"] == "Some Longer Act Title"


def test_same_law_groups_translation_volumes():
    candidates = [
        {"jlt_id": "1", "law_num_en": "Act No. 89 of 1896"},
        {"jlt_id": "2", "law_num_en": "Act No. 89 of 1896"},
        {"jlt_id": "3", "law_num_en": "Act No. 90 of 1896"},
    ]
    same = english._same_law(candidates, "明治二十九年法律第八十九号")
    assert [c["jlt_id"] for c in same] == ["1", "2"]
    assert english._same_law(candidates, None) == []


# --- staleness ----------------------------------------------------------------

def test_staleness_flags_a_lagging_translation(jlt):
    out = english._staleness("令和二年法律第十三号", "昭和二十二年法律第四十九号")
    assert out["is_stale"] is True
    assert out["current_amendment"] == "令和八年法律第六十号"
    assert "STALE" in out["warning"]


def test_staleness_clears_a_current_translation(jlt):
    out = english._staleness("令和八年法律第六十号", "昭和二十二年法律第四十九号")
    assert out["is_stale"] is False
    assert out["warning"] is None


def test_staleness_without_a_japanese_law_num():
    out = english._staleness("令和二年法律第十三号", None)
    assert out["is_stale"] is None
    assert "could not be checked" in out["warning"]


# --- dictionary ---------------------------------------------------------------

def test_parse_dictionary_maps_columns():
    entries = english._parse_dictionary(DICT_CSV)
    assert len(entries) == 4
    assert entries[0] == {
        "term_ja": "善意", "reading": "ぜんい", "candidate_no": "1",
        "term_en": "good faith", "usage_note": "一般的な場合",
        "example_ja": "善意の第三者", "example_en": "a third party in good faith",
        "example_source": "民法第94条", "note_1": "", "note_2": "",
    }


def test_lookup_ranks_exact_before_substring():
    entries = english._parse_dictionary(DICT_CSV)
    hits = english._lookup(entries, "善意", "ja")
    assert [h["term_en"] for h in hits] == ["good faith", "without knowledge"]


def test_lookup_english_direction():
    entries = english._parse_dictionary(DICT_CSV)
    assert [h["term_ja"] for h in english._lookup(entries, "employer", "en")] == ["使用者"]


@pytest.mark.parametrize("term,direction", [
    ("善意", "ja"), ("good faith", "en"), ("ぜんい", "ja"), ("worker", "en"),
])
def test_detect_direction(term, direction):
    assert english._detect_direction(term) == direction


# --- tools --------------------------------------------------------------------

def test_search_english_laws(jlt, tools):
    result = tools["search_english_laws"]("labor standards")
    assert result["total_matches"] == 2
    assert result["laws"][0]["jlt_id"] == "4913"
    assert "unofficial" in result["disclaimer"].lower()


def test_search_english_laws_rejects_empty_query(tools):
    with pytest.raises(ValueError, match="query is required"):
        tools["search_english_laws"]("")


def test_search_refreshes_csrf_token_after_403(jlt, tools):
    english._csrf_tokens["/en/laws"] = "STALE-TOKEN"
    calls = {"n": 0}

    def result(request, context):
        calls["n"] += 1
        if "STALE-TOKEN" in (request.text or ""):
            context.status_code = 403
            return "forbidden"
        return SEARCH_RESULT_HTML

    jlt.post(f"{JLT}/en/laws/result/", text=result)
    assert tools["search_english_laws"]("labor")["returned"] == 2
    assert calls["n"] == 2


def test_get_english_law_toc(jlt, tools):
    result = tools["get_english_law"]("労基法")
    assert result["found"] is True
    assert result["jlt_id"] == "4913"
    assert result["law_title_en"].startswith("Labor Standards Act")
    assert result["translated_date"] == "August 30, 2024"
    assert result["staleness"]["is_stale"] is True
    assert result["article_count"] == 4
    assert len(result["table_of_contents"]) == 2
    assert "32_2" in [a["article"] for a in result["articles_index"]]
    assert "disclaimer" in result


def test_get_english_law_article(jlt, tools):
    result = tools["get_english_law"]("労働基準法", "32")
    assert result["found"] is True
    assert result["article_num"] == "32"
    assert result["article_caption_en"] == "(Working Hours)"
    assert "40 hours per week" in result["english"]["text"]
    assert "8 hours per day" in result["english"]["text"]
    assert "四十時間" in result["text_ja"]
    assert result["english"]["truncated"] is False


def test_get_english_law_branch_article(jlt, tools):
    result = tools["get_english_law"]("労働基準法", "第32条の2")
    assert result["article_num"] == "32_2"
    assert result["article_title_en"] == "Article 32-2"
    assert "not more than one month" in result["english"]["text"]


def test_get_english_law_prefers_main_provision_over_suppl(jlt, tools):
    """122 exists only in the 附則 here, so it must still resolve — and say so."""
    result = tools["get_english_law"]("労働基準法", "122")
    assert result["found"] is True
    assert result["section"] == "SupplProvision"


def test_get_english_law_reports_deleted_range(jlt, tools):
    result = tools["get_english_law"]("労働基準法", "30")
    assert result["deleted_range"] == [29, 31]
    assert "Deleted" in result["english"]["text"]


def test_get_english_law_missing_article(jlt, tools):
    result = tools["get_english_law"]("労働基準法", "999")
    assert result["found"] is False
    assert "not in the English translation" in result["error"]


def test_get_english_law_paging(jlt, tools, monkeypatch):
    monkeypatch.setattr(english, "MAX_ELEMENT_CHARS", 40)
    result = tools["get_english_law"]("労働基準法", "32")
    assert result["english"]["returned_chars"] == 40
    assert result["english"]["truncated"] is True
    assert "offset=40" in result["english"]["note"]
    tail = tools["get_english_law"]("労働基準法", "32", offset=40)
    assert tail["english"]["offset"] == 40


def test_get_english_law_untranslated(jlt, tools):
    result = tools["get_english_law"]("nosuchlaw")
    assert result["found"] is False
    assert "No English translation" in result["error"]
    assert "disclaimer" in result


def test_get_english_law_caches_the_law_page(jlt, tools):
    tools["get_english_law"]("労働基準法", "32")
    tools["get_english_law"]("労働基準法", "第32条の2")
    views = [r for r in jlt.request_history if "/laws/view/" in r.url]
    assert len(views) == 1


VOLUMES_SEARCH_HTML = """<html><body><main>
<div class="result-number">Showing 1 to 2 of 2</div>
<ul class="search-result">
  <li><div class="result-law-type">Act</div>
      <div class="result-title-text"><a href="/en/laws/view/100">Civil Code (Part IV and Part V)</a></div>
      <div class="result-info-item">Law Number: Act No. 89 of 1896</div>
      <div class="result-info-item">Translated Date: January 30, 2014</div></li>
  <li><div class="result-law-type">Act</div>
      <div class="result-title-text"><a href="/en/laws/view/101">Civil Code (Part I, Part II and Part III)</a></div>
      <div class="result-info-item">Law Number: Act No. 89 of 1896</div>
      <div class="result-info-item">Translated Date: April 1, 2020</div></li>
</ul></main></body></html>"""

VOLUME_II_HTML = """<html><body><main>
<div id="lawInfo" class="law-info">
  <div class="title">民法（明治二十九年法律第八十九号）</div>
  <div class="title">Civil Code（Act No. 89 of 1896）</div>
</div>
<div id="lawBody" class="law-body">
  <div class="Article anchor" id="je_ch5at1">
    <div class="ArticleCaption">（公序良俗）</div>
    <div class="ArticleCaption">(Public Policy)</div>
    <div class="Paragraph">
      <div class="ParagraphSentence"><span class="ArticleTitle">第九十条</span>公の秩序又は善良の風俗に反する法律行為は、無効とする。</div>
      <div class="ParagraphSentence"><span class="ArticleTitle">Article 90</span>A juridical act that is against public policy is void.</div>
    </div>
  </div>
</div></main></body></html>"""


def test_get_english_law_falls_back_to_a_companion_volume(jlt, tools):
    """The Civil Code ships as several JLT entries under one 法令番号."""
    jlt.post(f"{JLT}/en/laws/result/", text=VOLUMES_SEARCH_HTML)
    jlt.get(f"{JLT}/en/laws/view/100/je", text=LAW_VIEW_HTML)
    jlt.get(f"{JLT}/en/laws/view/101/je", text=VOLUME_II_HTML)

    result = tools["get_english_law"]("民法", "90")
    assert result["found"] is True
    assert result["jlt_id"] == "101"
    assert "Part I" in result["note"]
    assert "against public policy is void" in result["english"]["text"]


def test_lookup_legal_term_auto_direction(jlt, tools):
    result = tools["lookup_legal_term"]("善意")
    assert result["direction"] == "ja"
    assert result["dictionary_version"] == "19.0"
    assert [e["term_en"] for e in result["entries"]] == ["good faith", "without knowledge"]
    assert result["entries"][0]["usage_note"] == "一般的な場合"
    assert result["entries"][0]["example_source"] == "民法第94条"
    assert "disclaimer" in result


def test_lookup_legal_term_english_input(jlt, tools):
    result = tools["lookup_legal_term"]("good faith")
    assert result["direction"] == "en"
    assert result["entries"][0]["term_ja"] == "善意"


def test_lookup_legal_term_no_match(jlt, tools):
    result = tools["lookup_legal_term"]("完全に存在しない語")
    assert result["found"] is False
    assert result["hint"]


def test_lookup_legal_term_rejects_bad_direction(tools):
    with pytest.raises(ValueError, match="direction must be"):
        tools["lookup_legal_term"]("善意", direction="sideways")


def test_dictionary_downloaded_once(jlt, tools):
    tools["lookup_legal_term"]("善意")
    tools["lookup_legal_term"]("労働者")
    downloads = [r for r in jlt.request_history
                 if r.method == "POST" and "/dicts/download" in r.url]
    assert len(downloads) == 1


def test_register_defines_three_tools():
    mcp = FastMCP("test")
    registered = english.register(mcp)
    assert set(registered) == {"get_english_law", "search_english_laws",
                               "lookup_legal_term"}


def test_import_registers_nothing():
    """The module must not touch a FastMCP instance at import time."""
    assert not hasattr(english, "mcp")


# --- live ---------------------------------------------------------------------

@pytest.mark.live
def test_live_labor_standards_act_article_32():
    tools = english.register(FastMCP("live"))
    result = tools["get_english_law"]("労働基準法", "32")
    assert result["found"] is True
    assert "40 hours per week" in result["english"]["text"]
    assert result["law_title_ja"].startswith("労働基準法")
    assert result["staleness"]["translation_reflects_amendment"]


@pytest.mark.live
def test_live_search_english_laws():
    tools = english.register(FastMCP("live"))
    result = tools["search_english_laws"]("labor standards")
    assert result["returned"] > 0
    assert any("Labor Standards Act" == law["english_title"] for law in result["laws"])


@pytest.mark.live
def test_live_dictionary_lookup():
    tools = english.register(FastMCP("live"))
    result = tools["lookup_legal_term"]("善意")
    assert result["found"] is True
    assert any("good faith" in (e["term_en"] or "") for e in result["entries"])
