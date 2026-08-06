"""e-Gov Law MCP server.

Exposes Japan's e-Gov 法令API v2 (https://laws.e-gov.go.jp/api/2) as MCP tools:
search laws, full-text keyword search, fetch law text or any single element of
a law, pull a specific article out of a law by number, and list a law's
amendment history (including 未施行 amendments not yet in force).

Articles are fetched with the API's ``elm`` parameter — an element path such as
``MainProvision-Article_24_2`` or ``AppdxTable[1]`` — so the server asks for the
one provision it needs instead of downloading and scanning a whole statute.
That also makes 附則 (SupplProvision) and 別表 (AppdxTable) addressable, which a
global ``Article[@Num=]`` search cannot do: 附則 articles reuse main-body
numbers.

The API needs no key. Common laws (六法 + key modern legislation) and their
abbreviations are mapped directly so a name like "民法" or "労基法" resolves
without a search round-trip.
"""

import base64
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastmcp import FastMCP

from cache import TTLCache, ttl_cached

EGOV_BASE = "https://laws.e-gov.go.jp/api/2"
USER_AGENT = "e-gov-law-mcp (+https://github.com/gaijindev/e-gov-law-mcp)"


def _default_data_dir() -> Path:
    """Where to save law text files.

    Running from a checked-out repo (dev/stdio-from-source use), a `data/`
    folder next to server.py is the obvious, visible place. But once this is
    pip/uvx-installed, `__file__` resolves inside site-packages — writing
    there would silently pollute the installed package instead of the
    user's own files. Detect that case and fall back to a per-user data
    directory instead. `LAW_MCP_DATA_DIR` always overrides both.
    """
    override = os.environ.get("LAW_MCP_DATA_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    if "site-packages" in here.parts or "dist-packages" in here.parts:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(base) / "e-gov-law-mcp"
    return here.parent / "data"


DATA_DIR = _default_data_dir()

# The hosted HTTP deployment runs on a read-only/ephemeral filesystem, so the
# save-to-disk path has to degrade instead of crashing the tool.
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_WRITABLE = os.access(DATA_DIR, os.W_OK)
except OSError:
    DATA_WRITABLE = False

# Operator tools (cache introspection, local file listing) are noise in an
# agent's tool list, so they are only registered when explicitly enabled.
ADMIN_TOOLS = bool(os.environ.get("LAW_MCP_ADMIN_TOOLS"))

mcp = FastMCP("e-gov-law")

# Statute text and law-name resolution both change rarely; caching them turns
# repeat find_law_article/get_law_content calls against the same law (a common
# pattern for callers pulling several articles from one statute) into cache
# hits after the first fetch. TTL is configurable since "how stale is
# acceptable" is an operational choice, not a code one.
_CACHE_TTL = float(os.environ.get("LAW_CACHE_TTL_SECONDS", 21_600))
RESOLVE_LAW_CACHE = TTLCache(maxsize=128, ttl=_CACHE_TTL)
LAW_XML_CACHE = TTLCache(maxsize=64, ttl=_CACHE_TTL)
ELEMENT_CACHE = TTLCache(maxsize=512, ttl=_CACHE_TTL)

# Element responses are returned inline, so they need a hard cap; agents page
# with `offset` when they hit it.
MAX_ELEMENT_CHARS = 15_000

# Common laws mapped straight to their 法令番号 so no search is needed.
BASIC_LAWS = {
    # 六法
    "民法": "明治二十九年法律第八十九号",
    "憲法": "昭和二十一年憲法",
    "日本国憲法": "昭和二十一年憲法",
    "刑法": "明治四十年法律第四十五号",
    "商法": "明治三十二年法律第四十八号",
    "民事訴訟法": "平成八年法律第百九号",
    "刑事訴訟法": "昭和二十三年法律第百三十一号",
    # 現代重要法
    "会社法": "平成十七年法律第八十六号",
    "労働基準法": "昭和二十二年法律第四十九号",
    "所得税法": "昭和四十年法律第三十三号",
    "法人税法": "昭和四十年法律第三十四号",
    "著作権法": "昭和四十五年法律第四十八号",
    "特許法": "昭和三十四年法律第百二十一号",
    "道路交通法": "昭和三十五年法律第百五号",
    "建築基準法": "昭和二十五年法律第二百一号",
    "独占禁止法": "昭和二十二年法律第五十四号",
    "消費者契約法": "平成十二年法律第六十一号",
}

# Everyday abbreviations / shorthand mapped to a formal law name.
LAW_ALIASES = {
    "道交法": "道路交通法",
    "労基法": "労働基準法",
    "独禁法": "独占禁止法",
    "消契法": "消費者契約法",
    "著作権": "著作権法",
    "特許": "特許法",
    "建基法": "建築基準法",
    "税法": "所得税法",
    "労働法": "労働基準法",
    "知財法": "著作権法",
    "交通法": "道路交通法",
    "会社": "会社法",
    "民事": "民法",
    "刑事": "刑法",
    "訴訟": "民事訴訟法",
}

_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# Structural containers inside MainProvision, outermost first.
_DIVISION_TAGS = ("Part", "Chapter", "Section", "Subsection", "Division")
_DIVISION_LABELS = {"Part": "編", "Chapter": "章", "Section": "節",
                    "Subsection": "款", "Division": "目"}

# 404001 = "no results"; 400021 = "no element matches elm". Both mean "nothing
# there" rather than a malformed request, so they surface as None, not an error.
_EMPTY_RESULT_CODES = {"404001", "400021"}

_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
_SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]), raise_on_status=False,
)))


def _get(path: str, **params) -> dict | None:
    """GET an e-Gov endpoint. Returns None when the API reports zero results.

    The API signals an empty result set with HTTP 404 body code "404001" and a
    non-existent ``elm`` path with HTTP 400 body code "400021"; both are treated
    as "nothing found", while any other error is raised.
    """
    resp = _SESSION.get(f"{EGOV_BASE}{path}", params=params, timeout=60)
    if resp.status_code in (400, 404):
        try:
            if resp.json().get("code") in _EMPTY_RESULT_CODES:
                return None
        except ValueError:
            pass
    if not resp.ok:
        content_type = resp.headers.get("content-type", "")
        body = resp.text[:200] if "json" in content_type or "text" in content_type \
            else "(non-text error body)"
        raise RuntimeError(f"e-Gov API error (HTTP {resp.status_code}) for {path}: {body}")
    return resp.json()


def _kanji_to_int(text: str) -> int | None:
    """Convert a Japanese numeral like '百九十二' to 192. Returns None if not numeric."""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, current = 0, 0, 0
    for ch in text:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            unit = _KANJI_UNITS[ch]
            if unit >= 1000:
                total += (current or 1) * unit
                section = 0
            else:
                section += (current or 1) * unit
            current = 0
        else:
            return None
    return total + section + current


def _article_to_num(article: str) -> tuple[str | None, int | None, int | None]:
    """Parse an article reference into the XML ``Num`` attribute, 項 and 号.

    "192" -> ("192", None, None); "325条の3" -> ("325_3", None, None);
    "第9条第2項" -> ("9", 2, None); "第七百九条" -> ("709", None, None);
    "第24条第1項第4号" -> ("24", 1, 4).
    """
    s = article.strip()
    numeral = r"[0-9０-９一二三四五六七八九十百千]+"

    paragraph = item = None
    para_match = re.search(rf"第?\s*({numeral})\s*項", s)
    if para_match:
        paragraph = _kanji_to_int(para_match.group(1).translate(_ZEN2HAN))
    item_match = re.search(rf"第?\s*({numeral})\s*号", s)
    if item_match:
        item = _kanji_to_int(item_match.group(1).translate(_ZEN2HAN))

    body = re.split(r"[項号]", s)[0].translate(_ZEN2HAN)
    branch = re.search(r"([0-9一二三四五六七八九十百千]+)\s*条?\s*の\s*([0-9一二三四五六七八九十百千]+)", body)
    if branch:
        main, sub = _kanji_to_int(branch.group(1)), _kanji_to_int(branch.group(2))
        if main is not None and sub is not None:
            return f"{main}_{sub}", paragraph, item
    main_match = re.search(r"([0-9一二三四五六七八九十百千]+)", body)
    if main_match:
        main = _kanji_to_int(main_match.group(1))
        if main is not None:
            return str(main), paragraph, item
    return None, paragraph, item


def _validate_asof(asof: str) -> str:
    """Return a validated ``asof`` date, or raise with a usable hint."""
    if not asof:
        return ""
    try:
        date.fromisoformat(asof.strip())
    except ValueError:
        raise ValueError(
            f"asof must be a date in YYYY-MM-DD form (got '{asof}'), "
            "e.g. asof='2020-04-01' for the law as it stood on 1 April 2020."
        ) from None
    return asof.strip()


def _clean(el: ET.Element) -> str:
    """Concatenate an element's visible text, dropping XML indentation."""
    return "".join(t.strip() for t in el.itertext() if t.strip())


def _rev_meta(rev: dict) -> dict:
    """Revision fields an agent needs to judge which version it is looking at."""
    return {
        "law_revision_id": rev.get("law_revision_id"),
        "abbrev": rev.get("abbrev"),
        "current_revision_status": rev.get("current_revision_status"),
        "amendment_enforcement_date": rev.get("amendment_enforcement_date"),
        "amendment_scheduled_enforcement_date": rev.get("amendment_scheduled_enforcement_date"),
    }


def _law_brief(item: dict) -> dict:
    """Trim a /laws or /keyword result item to the useful fields."""
    info = item.get("law_info", {})
    rev = item.get("revision_info", {})
    brief = {
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num"),
        "law_title": rev.get("law_title"),
        "law_title_kana": rev.get("law_title_kana"),
        "category": rev.get("category"),
        "promulgation_date": info.get("promulgation_date"),
        "repeal_status": rev.get("repeal_status"),
    }
    brief.update(_rev_meta(rev))
    if item.get("sentences"):
        brief["matched_sentences"] = [
            {"position": s.get("position"),
             "text": re.sub(r"</?span>", "", s.get("text", ""))}
            for s in item["sentences"]
        ]
    return brief


def _revision_brief(rev: dict) -> dict:
    """Trim a /law_revisions revision_info to the fields callers reason about."""
    return {
        "law_revision_id": rev.get("law_revision_id"),
        "law_title": rev.get("law_title"),
        "law_title_kana": rev.get("law_title_kana"),
        "abbrev": rev.get("abbrev"),
        "category": rev.get("category"),
        "amendment_law_title": rev.get("amendment_law_title"),
        "amendment_law_num": rev.get("amendment_law_num"),
        "amendment_promulgate_date": rev.get("amendment_promulgate_date"),
        "amendment_enforcement_date": rev.get("amendment_enforcement_date"),
        "amendment_scheduled_enforcement_date": rev.get("amendment_scheduled_enforcement_date"),
        "amendment_enforcement_comment": rev.get("amendment_enforcement_comment"),
        "current_revision_status": rev.get("current_revision_status"),
        "repeal_status": rev.get("repeal_status"),
    }


@ttl_cached(RESOLVE_LAW_CACHE)
def _resolve_law(law_name: str) -> tuple[str | None, str | None, str | None]:
    """Resolve a law name to (law_num, resolved_title, alias_source).

    Aliases are expanded first, then a direct mapping is tried, then the /laws
    search endpoint (preferring an exact title match).
    """
    name = law_name.strip()
    alias_source = None
    if name in LAW_ALIASES:
        alias_source, name = name, LAW_ALIASES[name]
    if name in BASIC_LAWS:
        return BASIC_LAWS[name], name, alias_source

    data = _get("/laws", law_title=name, law_type="Act", limit=20)
    if not data or not data.get("laws"):
        return None, name, alias_source
    laws = data["laws"]
    exact = [l for l in laws if l.get("revision_info", {}).get("law_title") == name]
    chosen = exact[0] if exact else laws[0]
    info, rev = chosen.get("law_info", {}), chosen.get("revision_info", {})
    return info.get("law_num"), rev.get("law_title"), alias_source


def _resolution_error(law_name: str) -> dict:
    """A law-not-found payload that tells the agent what to try next."""
    return {
        "found": False,
        "error": f"Law '{law_name}' not found",
        "search_law_name": law_name,
        "hint": ("Try search_laws(law_title=...) with a distinctive fragment of the "
                 "name, or pass the formal title (e.g. '出入国管理及び難民認定法' "
                 "rather than '入管法') or a law_id / 法令番号."),
    }


@ttl_cached(LAW_XML_CACHE)
def _fetch_law_xml(law_id_or_num: str, asof: str = "") -> tuple[ET.Element, dict, dict]:
    """Fetch a law's standard XML and return (root, law_info, revision_info)."""
    params = {"law_full_text_format": "xml"}
    if asof:
        params["asof"] = asof
    data = _get(f"/law_data/{law_id_or_num}", **params)
    if not data:
        raise RuntimeError(f"No law found for '{law_id_or_num}'")
    full_text = data.get("law_full_text")
    if not isinstance(full_text, str):
        raise RuntimeError("Unexpected law_full_text format from e-Gov API")
    xml = base64.b64decode(full_text).decode("utf-8")
    return ET.fromstring(xml), data.get("law_info", {}), data.get("revision_info", {})


@ttl_cached(ELEMENT_CACHE)
def _fetch_element(law_key: str, elm: str,
                   asof: str = "") -> tuple[ET.Element, dict, dict] | None:
    """Fetch one element of a law via ``elm``. None when the path doesn't exist.

    The response body is the fragment's own root (``<Article Num="32">``,
    ``<SupplProvision>``, ``<AppdxTable>`` …), not the whole ``<Law>``.
    """
    params = {"law_full_text_format": "xml", "elm": elm}
    if asof:
        params["asof"] = asof
    data = _get(f"/law_data/{law_key}", **params)
    if not data:
        return None
    full_text = data.get("law_full_text")
    if not isinstance(full_text, str):
        raise RuntimeError("Unexpected law_full_text format from e-Gov API")
    xml = base64.b64decode(full_text).decode("utf-8")
    return ET.fromstring(xml), data.get("law_info", {}), data.get("revision_info", {})


def _num_as_int(num: str | None) -> int | None:
    if not num:
        return None
    head = num.split(":")[0].split("_")[0]
    return int(head) if head.isdigit() else None


def _find_merged_range(root: ET.Element, target: str) -> ET.Element | None:
    """Find a merged-deletion Article (``Num="3:5"``) whose range covers target.

    Deleted articles are collapsed into one element spanning a range, so an
    exact ``Num`` match misses them entirely.
    """
    wanted = _num_as_int(target)
    if wanted is None or "_" in target:
        return None
    for el in root.iter("Article"):
        num = el.get("Num") or ""
        if ":" not in num:
            continue
        lo, _, hi = num.partition(":")
        lo_i, hi_i = _num_as_int(lo), _num_as_int(hi)
        if lo_i is not None and hi_i is not None and lo_i <= wanted <= hi_i:
            return el
    return None


def _article_payload(el: ET.Element, paragraph: int | None, item: int | None) -> dict:
    """Caption/title/text of an Article, plus the requested 項 and 号 if given."""
    out = {
        "article_caption": el.findtext("ArticleCaption"),
        "article_title": el.findtext("ArticleTitle"),
        "text": _clean(el),
    }
    scope = el
    if paragraph is not None:
        para_el = el.find(f".//Paragraph[@Num='{paragraph}']")
        out["paragraph"] = paragraph
        out["paragraph_text"] = _clean(para_el) if para_el is not None else None
        if para_el is not None:
            scope = para_el
    if item is not None:
        item_el = scope.find(f".//Item[@Num='{item}']")
        out["item"] = item
        out["item_text"] = _clean(item_el) if item_el is not None else None
    return out


def _division_toc(el: ET.Element) -> list[dict]:
    """Walk 編/章/節/款/目 under an element into a nested table of contents."""
    entries = []
    for child in el:
        if child.tag not in _DIVISION_TAGS:
            continue
        articles = child.findall(".//Article")
        entry = {
            "level": child.tag,
            "level_ja": _DIVISION_LABELS[child.tag],
            "num": child.get("Num"),
            "title": (child.findtext(f"{child.tag}Title") or "").strip(),
            "article_count": len(articles),
        }
        article_range = child.findtext("ArticleRange")
        if article_range:
            entry["article_range"] = article_range.strip()
        elif articles:
            first, last = articles[0].get("Num"), articles[-1].get("Num")
            entry["article_range"] = first if first == last else f"{first}–{last}"
        children = _division_toc(child)
        if children:
            entry["children"] = children
        entries.append(entry)
    return entries


def _build_toc(root: ET.Element) -> dict:
    """Table of contents for a whole-law XML root: structure + 附則/別表 summary."""
    body = root.find(".//LawBody")
    main = root.find(".//MainProvision")
    toc = _division_toc(main) if main is not None else []
    if not toc and main is not None:
        articles = main.findall(".//Article")
        if articles:
            toc = [{
                "level": "MainProvision", "level_ja": "本則", "num": None,
                "title": "本則", "article_count": len(articles),
                "article_range": f"{articles[0].get('Num')}–{articles[-1].get('Num')}",
            }]
    out = {"table_of_contents": toc}
    if body is not None:
        out["has_preamble"] = body.find("Preamble") is not None
        out["suppl_provision_count"] = len(body.findall("SupplProvision"))
        out["appdx_tables"] = [
            (t.findtext("AppdxTableTitle") or "").strip()
            for t in body.findall("AppdxTable")
        ]
    return out


def _looks_like_law_key(value: str) -> bool:
    """True when a string is already a /law_data path key, not a law name.

    law_id ("129AC0000000089"), law_revision_id ("..._20260717_...") and 法令番号
    ("明治二十九年法律第八十九号") are all valid path keys; sending them through
    the title search would fail.
    """
    v = value.strip()
    if re.fullmatch(r"[0-9A-Za-z]+_\d{8}_[0-9A-Za-z]+", v):
        return True
    if re.fullmatch(r"\d{3}[A-Z]{2}\d{10}", v):
        return True
    return bool(re.search(r"(法律|勅令|政令|省令|規則|条例|命令)第|憲法", v))


def _law_key(law_name_or_id: str, revision_id: str) -> tuple[str | None, str | None, str | None]:
    """Pick the /law_data path key: an explicit revision id wins over resolution."""
    if revision_id.strip():
        return revision_id.strip(), None, None
    name = law_name_or_id.strip()
    if name in LAW_ALIASES or name in BASIC_LAWS:
        return _resolve_law(name)
    if _looks_like_law_key(law_name_or_id):
        return law_name_or_id.strip(), None, None
    return _resolve_law(law_name_or_id)


@mcp.tool(annotations={"title": "Search laws by title or number",
                       "readOnlyHint": True, "openWorldHint": True})
def search_laws(law_title: str = "", law_type: str = "Act", law_num: str = "",
                limit: int = 10, offset: int = 0, category_cd: str = "",
                promulgation_date_from: str = "", promulgation_date_to: str = "",
                order: str = "", asof: str = "") -> dict:
    """Search Japanese laws by TITLE, number or category — not by body text.

    Use this to find a law and its ``law_id``; use ``search_laws_by_keyword``
    to search the text inside laws.

    Args:
        law_title: Part of the law name, e.g. "個人情報" or "民法".
        law_type: One of Act, CabinetOrder, MinisterialOrdinance, Rule, Constitution.
            Defaults to "Act"; pass "" to search all types.
        law_num: Part of the 法令番号, e.g. "平成十五年法律".
        limit: Maximum laws to return (default 10).
        offset: Number of results to skip, for pagination (default 0).
        category_cd: e-Gov subject category code(s), comma-separated (e.g. "27" 労働).
        promulgation_date_from: Only laws promulgated on/after this YYYY-MM-DD.
        promulgation_date_to: Only laws promulgated on/before this YYYY-MM-DD.
        order: Sort key, e.g. "-revision_info.amendment_promulgate_date" for
            most-recently-amended first.
        asof: YYYY-MM-DD — return each law as it stood on that date.

    Returns matching laws with their ``law_id``, ``law_num``, official ``abbrev``
    and revision status, for use with ``get_law_content`` or ``find_law_article``.
    """
    params = {"limit": limit, "offset": offset}
    if law_title:
        params["law_title"] = law_title
    if law_type:
        params["law_type"] = law_type
    if law_num:
        params["law_num"] = law_num
    if category_cd:
        params["category_cd"] = category_cd
    if promulgation_date_from:
        params["promulgation_date_from"] = _validate_asof(promulgation_date_from)
    if promulgation_date_to:
        params["promulgation_date_to"] = _validate_asof(promulgation_date_to)
    if order:
        params["order"] = order
    if asof:
        params["asof"] = _validate_asof(asof)

    data = _get("/laws", **params)
    if not data:
        return {"total_matches": 0, "returned": 0, "laws": []}
    return {
        "total_matches": data.get("total_count"),
        "returned": len(data.get("laws", [])),
        "next_offset": data.get("next_offset"),
        "laws": [_law_brief(l) for l in data.get("laws", [])],
    }


@mcp.tool(annotations={"title": "Full-text search across all laws",
                       "readOnlyHint": True, "openWorldHint": True})
def search_laws_by_keyword(keyword: str, law_type: str = "", limit: int = 50,
                           offset: int = 0, sentences_limit: int = 5,
                           sentence_text_size: int = 100, asof: str = "",
                           category_cd: str = "") -> dict:
    """Full-text search across the BODY of every Japanese law.

    Use this when you don't know which law says something; use ``search_laws``
    when you know the law's name.

    Args:
        keyword: Phrase to find in law text. Supports the e-Gov search syntax,
            including wildcards (``第*条``) and AND/OR/NOT operators.
        law_type: Optional type filter, e.g. "Act" (comma-separated for several).
        limit: Cap on the total number of matched sentences returned across all
            laws (default 50, e-Gov max 1000) — not a cap on the number of laws.
            Note: the e-Gov API returns no results when this is smaller than the
            sentence count of the first matching laws, so keep it >= 20.
        offset: Result offset for pagination (default 0); pass back ``next_offset``.
        sentences_limit: Max matched sentences kept per law (default 5).
        sentence_text_size: Characters of context per matched sentence (default 100).
        asof: YYYY-MM-DD — search the laws as they stood on that date.
        category_cd: e-Gov subject category code(s), comma-separated.

    Returns matching laws, each with the sentences where the keyword occurs.
    Every sentence carries a ``position`` such as
    ``MainProvision-Article_21-Paragraph_3``: that string is an element path —
    pass it straight to ``get_law_element(law_id, elm=position)``, or drop the
    trailing ``-Paragraph_n`` to pull the whole article.
    ``total_matches`` is the total count of matching sentence positions.
    """
    if not keyword or not keyword.strip():
        raise ValueError("keyword is required")
    params = {"keyword": keyword.strip(), "limit": limit, "offset": offset,
              "sentences_limit": sentences_limit,
              "sentence_text_size": sentence_text_size}
    if law_type:
        params["law_type"] = law_type
    if category_cd:
        params["category_cd"] = category_cd
    if asof:
        params["asof"] = _validate_asof(asof)

    data = _get("/keyword", **params)
    if not data:
        result = {"total_matches": 0, "returned": 0, "laws": []}
        if limit < 20:
            result["note"] = ("No results. A low limit can suppress matches on this "
                              "endpoint; retry with limit >= 20.")
        return result
    return {
        "total_matches": data.get("total_count"),
        "returned": len(data.get("items", [])),
        "next_offset": data.get("next_offset"),
        "position_usage": ("Each matched sentence's `position` is an elm path usable "
                           "with get_law_element(law_id, elm=position)."),
        "laws": [_law_brief(it) for it in data.get("items", [])],
    }


@mcp.tool(annotations={"title": "Find one article of a law",
                       "readOnlyHint": True, "openWorldHint": True})
def find_law_article(law_name: str, article: str, provision: str = "main",
                     asof: str = "", revision_id: str = "") -> dict:
    """Find a specific article within a Japanese law.

    Resolves common law names and abbreviations directly (e.g. "民法", "労基法"),
    then asks e-Gov for that one article via its element path — the whole statute
    is never downloaded.

    Args:
        law_name: Law name or abbreviation, e.g. "民法", "会社法", "道交法".
        article: Article number. Accepts plain numbers ("192"), branch articles
            ("325条の3"), kanji ("第七百九条"), a paragraph ("第9条第2項") and an
            item ("第24条第1項第4号").
        provision: "main" for 本則 (default) or "suppl" for 附則 (supplementary
            provisions). 附則 articles reuse main-body numbers, so this matters.
        asof: YYYY-MM-DD — the law as it stood on that date.
        revision_id: An exact ``law_revision_id`` from ``get_law_revisions``;
            overrides name resolution and ``asof``.

    Returns the article's caption, title and full text. A 項 (paragraph) or 号
    (item) given in ``article`` is also returned separately.
    """
    if not law_name.strip():
        raise ValueError("law_name is required")
    if not article.strip():
        raise ValueError("article is required")
    if provision not in ("main", "suppl"):
        raise ValueError("provision must be 'main' (本則) or 'suppl' (附則)")
    asof = _validate_asof(asof)

    law_key, resolved_title, alias_source = _law_key(law_name, revision_id)
    if not law_key:
        return _resolution_error(law_name)

    num_attr, paragraph, item = _article_to_num(article)
    if not num_attr:
        return {"found": False,
                "error": f"Could not parse article '{article}'",
                "law_title": resolved_title, "law_num": law_key,
                "hint": "Use a form like '32', '第32条', '第24条の2' or '第24条第1項第4号'."}

    result = {
        "found": False,
        "search_law_name": law_name,
        "resolved_law_title": resolved_title,
        "law_num": law_key,
        "alias_expanded_from": alias_source,
        "search_article": article,
        "article_num": num_attr,
        "provision": provision,
    }
    if asof:
        result["asof"] = asof

    if provision == "main":
        elm = f"MainProvision-Article_{num_attr}"
        fetched = _fetch_element(law_key, elm, asof)
        if fetched is not None:
            el, info, rev = fetched
            result.update(_finalize_article(el, info, rev, resolved_title, elm))
            result.update(_article_payload(el, paragraph, item))
            result["found"] = True
            return result
    else:
        # 附則 blocks are addressable individually; the law's own 附則 is [1] and
        # covers the common case, so try it before pulling the whole statute.
        fetched = _fetch_element(law_key, "SupplProvision[1]", asof)
        if fetched is not None:
            block, info, rev = fetched
            el = block.find(f".//Article[@Num='{num_attr}']")
            if el is not None:
                result.update(_finalize_article(el, info, rev, resolved_title,
                                                "SupplProvision[1]"))
                result.update(_article_payload(el, paragraph, item))
                result["found"] = True
                return result

    # Fall back to the whole-law XML (cached): it resolves merged-deletion ranges
    # and 附則 blocks beyond the first.
    root, info, rev = _fetch_law_xml(law_key, asof)
    title = rev.get("law_title") or resolved_title
    scope_root = root.find(".//MainProvision") if provision == "main" else root
    if scope_root is None:
        scope_root = root

    if provision == "suppl":
        for idx, block in enumerate(root.findall(".//SupplProvision"), start=1):
            el = block.find(f".//Article[@Num='{num_attr}']")
            if el is not None:
                result.update(_finalize_article(el, info, rev, title,
                                                f"SupplProvision[{idx}]"))
                result.update(_article_payload(el, paragraph, item))
                result["found"] = True
                return result
    else:
        el = scope_root.find(f".//Article[@Num='{num_attr}']")
        if el is not None:
            result.update(_finalize_article(el, info, rev, title,
                                            f"MainProvision-Article_{num_attr}"))
            result.update(_article_payload(el, paragraph, item))
            result["found"] = True
            return result

    merged = _find_merged_range(scope_root, num_attr)
    if merged is not None:
        lo, _, hi = (merged.get("Num") or "").partition(":")
        result.update(_finalize_article(merged, info, rev, title,
                                        f"MainProvision-Article_{merged.get('Num')}"))
        result["deleted_range"] = merged.get("Num")
        result["text"] = _clean(merged)
        result["error"] = (f"Article {num_attr} of {title} no longer exists: "
                           f"articles {lo}–{hi} are deleted (削除).")
        return result

    result["law_id"] = info.get("law_id")
    result["law_num"] = info.get("law_num", law_key)
    result["resolved_law_title"] = title
    result["error"] = f"Article '{article}' not found in {title} ({provision})"
    result["hint"] = (
        "If the article is in the 附則 (supplementary provisions), retry with "
        "provision='suppl'. Deleted articles are merged into ranges such as "
        "Num='155:157' and report as 削除. Call get_law_content(law_id) for the "
        "table of contents and article ranges to confirm the number exists."
    )
    return result


def _finalize_article(el: ET.Element, info: dict, rev: dict,
                      fallback_title: str | None, elm: str) -> dict:
    """Shared identity/metadata block for an article-shaped result."""
    return {
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num"),
        "resolved_law_title": rev.get("law_title") or fallback_title,
        "elm": elm,
        "revision": _rev_meta(rev),
    }


@mcp.tool(annotations={"title": "Fetch any element of a law (別表・附則・目次)",
                       "readOnlyHint": True, "openWorldHint": True})
def get_law_element(law_id: str, elm: str, asof: str = "", revision_id: str = "",
                    offset: int = 0) -> dict:
    """Fetch one element of a law by its element path — the raw ``elm`` escape hatch.

    Args:
        law_id: A ``law_id``, 法令番号 or law name (e.g. "326CO0000000319").
        elm: Element path. Examples: ``AppdxTable[1]`` (別表第一),
            ``SupplProvision[1]`` (附則), ``Preamble[1]`` (前文), ``TOC[1]`` (目次),
            ``MainProvision-Article_24`` (第24条),
            ``MainProvision-Article_21-Paragraph_3``. Ordinals are 1-based
            (``Tag[n]``); ``Tag_num`` addresses by the element's Num attribute and
            branch numbers join with ``_`` (第24条の2 → ``Article_24_2``). A
            ``position`` from ``search_laws_by_keyword`` can be passed verbatim.
        asof: YYYY-MM-DD — the law as it stood on that date.
        revision_id: An exact ``law_revision_id``; overrides ``law_id``/``asof``.
        offset: Character offset into the cleaned text, for paging long elements.

    Returns the element's tag, attributes and cleaned text (capped at 15,000
    characters per call — page with ``offset``).

    ``elm`` is forwarded to a single fixed e-Gov endpoint
    (``GET /law_data/{key}?elm=...``) — see the e-Gov 法令API v2 docs
    (https://laws.e-gov.go.jp/api/2) for the full element-path grammar.
    """
    if not law_id.strip() and not revision_id.strip():
        raise ValueError("law_id is required (or pass revision_id)")
    if not elm.strip():
        raise ValueError("elm is required, e.g. 'AppdxTable[1]' or 'MainProvision-Article_24'")
    asof = _validate_asof(asof)

    law_key, resolved_title, _ = _law_key(law_id, revision_id)
    if not law_key:
        return _resolution_error(law_id)

    fetched = _fetch_element(law_key, elm.strip(), asof)
    if fetched is None:
        return {
            "found": False,
            "law_num": law_key,
            "elm": elm,
            "error": f"No element '{elm}' in {resolved_title or law_key}",
            "hint": ("Check the path against get_law_content(law_id)'s table of "
                     "contents. Ordinals are 1-based and Num-addressed tags use "
                     "Tag_num (Article_24_2 for 第24条の2)."),
        }

    el, info, rev = fetched
    text = _clean(el)
    chunk = text[offset:offset + MAX_ELEMENT_CHARS]
    out = {
        "found": True,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num"),
        "law_title": rev.get("law_title") or resolved_title,
        "revision": _rev_meta(rev),
        "elm": elm,
        "element_tag": el.tag,
        "element_attributes": dict(el.attrib),
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(chunk),
        "truncated": offset + MAX_ELEMENT_CHARS < len(text),
        "text": chunk,
    }
    if asof:
        out["asof"] = asof
    if out["truncated"]:
        out["note"] = (f"Truncated at {MAX_ELEMENT_CHARS} characters. Call again with "
                       f"offset={offset + MAX_ELEMENT_CHARS} for the next page.")
    return out


@mcp.tool(annotations={"title": "List a law's amendment history",
                       "readOnlyHint": True, "openWorldHint": True})
def get_law_revisions(law_name_or_id: str, unenforced_only: bool = False) -> dict:
    """List every revision of a law, newest first — including 未施行 amendments.

    Use this to answer "has this law been amended?", "what changes are pending?"
    and "when does the amendment take effect?", and to get a ``law_revision_id``
    for an exact version (pass it to ``find_law_article``/``get_law_element``).

    Args:
        law_name_or_id: Law name, abbreviation, ``law_id`` or 法令番号.
        unenforced_only: When True, return only revisions whose
            ``current_revision_status`` is "UnEnforced" (未施行 — promulgated but
            not yet in force).

    Each revision carries the amending law's title and number, its promulgation
    and enforcement dates, any enforcement comment (e.g. a conditional
    commencement date), and ``current_revision_status``
    (CurrentEnforced / UnEnforced / PreviousEnforced / Repeal).
    """
    if not law_name_or_id.strip():
        raise ValueError("law_name_or_id is required")

    law_key, resolved_title, alias_source = _law_key(law_name_or_id, "")
    if not law_key:
        return _resolution_error(law_name_or_id)

    data = _get(f"/law_revisions/{law_key}")
    if not data:
        return {"found": False, "law_num": law_key,
                "error": f"No revision history found for '{law_name_or_id}'"}

    info = data.get("law_info", {})
    revisions = [_revision_brief(r) for r in data.get("revisions", [])]
    if unenforced_only:
        revisions = [r for r in revisions
                     if r.get("current_revision_status") == "UnEnforced"]
    return {
        "found": True,
        "search_law_name": law_name_or_id,
        "resolved_law_title": resolved_title,
        "alias_expanded_from": alias_source,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num", law_key),
        "unenforced_only": unenforced_only,
        "revision_count": len(revisions),
        "revisions": revisions,
    }


@mcp.tool(annotations={"title": "Law metadata and table of contents",
                       "readOnlyHint": True, "openWorldHint": True})
def get_law_content(law_id: str, full_text: bool = False, save: bool = False,
                    asof: str = "") -> dict:
    """Fetch a law's metadata, table of contents, and optionally its full text.

    Args:
        law_id: A ``law_id`` or 法令番号 from ``search_laws`` (e.g. "129AC0000000089"
            or "明治二十九年法律第八十九号").
        full_text: When True, include the full plain text of the law. Large laws
            are written to ``data/`` instead of returned inline (use a preview +
            ``read_saved_law``, or ``find_law_article`` for one article).
        save: When True, always write the full text to ``data/{law_num}.txt``.
        asof: YYYY-MM-DD — the law as it stood on that date.

    Returns law metadata (including official ``abbrev`` and revision status), the
    nested 編/章/節 table of contents with article ranges, the number of 附則
    blocks and the titles of any 別表 — all of which can be addressed with
    ``get_law_element``.
    """
    asof = _validate_asof(asof)
    root, info, rev = _fetch_law_xml(law_id, asof)
    articles = root.findall(".//MainProvision//Article")

    out = {
        "law_title": rev.get("law_title"),
        "law_num": info.get("law_num"),
        "law_id": info.get("law_id"),
        "promulgation_date": info.get("promulgation_date"),
        "category": rev.get("category"),
        "article_count": len(articles),
        "revision": _rev_meta(rev),
    }
    if asof:
        out["asof"] = asof
    out.update(_build_toc(root))

    if not full_text and not save:
        return out

    body = root.find(".//LawBody")
    text = _clean(body) if body is not None else _clean(root)
    safe_num = re.sub(r"[^\w]", "_", info.get("law_num") or law_id)

    if not DATA_WRITABLE:
        # Hosted mode: no disk, so cap inline instead of saving.
        out["full_text_chars"] = len(text)
        out["text"] = text[:MAX_ELEMENT_CHARS]
        if len(text) > MAX_ELEMENT_CHARS:
            out["truncated"] = True
            out["note"] = ("Local saving is unavailable; text truncated. Use "
                           "find_law_article or get_law_element for the part you need.")
        return out

    if save or len(text) > 50000:
        path = DATA_DIR / f"{safe_num}.txt"
        path.write_text(text, encoding="utf-8")
        out["saved_to"] = path.name
        out["full_text_chars"] = len(text)
        if full_text and len(text) > 50000:
            out["note"] = ("Full text is large; saved to data/. Read it with "
                          "read_saved_law, or use find_law_article for one article.")
            out["preview"] = text[:2000]
        elif full_text:
            out["full_text"] = text
    elif full_text:
        out["full_text"] = text
    return out


@mcp.tool(annotations={"title": "Search (Deep Research)",
                       "readOnlyHint": True, "openWorldHint": True})
def search(query: str) -> dict:
    """Search Japanese law and return citable result ids (Deep Research convention).

    Runs a full-text search over every law's body, falling back to a title
    search when nothing matches. Each result ``id`` can be passed to ``fetch``:
    ``"{law_id}"`` for a whole law, ``"{law_id}::{elm_path}"`` for one provision.
    """
    if not query or not query.strip():
        raise ValueError("query is required")

    results: list[dict] = []
    data = _get("/keyword", keyword=query.strip(), limit=50, sentences_limit=3,
                sentence_text_size=100)
    for item in (data or {}).get("items", []):
        info, rev = item.get("law_info", {}), item.get("revision_info", {})
        law_id, title = info.get("law_id"), rev.get("law_title")
        url = f"https://laws.e-gov.go.jp/law/{law_id}"
        sentences = item.get("sentences") or []
        if not sentences:
            results.append({"id": law_id, "title": title, "url": url})
            continue
        for s in sentences:
            position = s.get("position")
            results.append({
                "id": f"{law_id}::{position}" if position else law_id,
                "title": f"{title} {position}" if position else title,
                "url": url,
            })

    if not results:
        titles = _get("/laws", law_title=query.strip(), law_type="Act", limit=10)
        for law in (titles or {}).get("laws", []):
            info, rev = law.get("law_info", {}), law.get("revision_info", {})
            results.append({
                "id": info.get("law_id"),
                "title": rev.get("law_title"),
                "url": f"https://laws.e-gov.go.jp/law/{info.get('law_id')}",
            })
    return {"results": results}


@mcp.tool(annotations={"title": "Fetch (Deep Research)",
                       "readOnlyHint": True, "openWorldHint": True})
def fetch(id: str) -> dict:
    """Fetch the text behind a ``search`` result id (Deep Research convention).

    ``id`` is either a bare ``law_id`` (returns metadata, table of contents and
    the opening of the law) or ``"{law_id}::{elm_path}"`` (returns that one
    provision's text).
    """
    if not id or not id.strip():
        raise ValueError("id is required")
    law_id, _, elm = id.strip().partition("::")
    url = f"https://laws.e-gov.go.jp/law/{law_id}"

    if elm:
        fetched = _fetch_element(law_id, elm)
        if fetched is None:
            raise ValueError(f"No element '{elm}' in law {law_id}. "
                             "Re-run search to get a valid id.")
        el, info, rev = fetched
        return {
            "id": id,
            "title": f"{rev.get('law_title')} {elm}",
            "text": _clean(el)[:MAX_ELEMENT_CHARS],
            "url": url,
            "metadata": {"law_num": info.get("law_num"), "elm": elm,
                         "element_tag": el.tag, **_rev_meta(rev)},
        }

    root, info, rev = _fetch_law_xml(law_id)
    body = root.find(".//LawBody")
    text = _clean(body) if body is not None else _clean(root)
    toc = _build_toc(root)
    return {
        "id": id,
        "title": rev.get("law_title"),
        "text": text[:10_000],
        "url": url,
        "metadata": {
            "law_num": info.get("law_num"),
            "promulgation_date": info.get("promulgation_date"),
            "category": rev.get("category"),
            "total_chars": len(text),
            **_rev_meta(rev),
            **toc,
        },
    }


def list_saved_laws() -> dict:
    """List law text files saved locally in the project's ``data/`` directory."""
    files = []
    for p in sorted(DATA_DIR.glob("*.txt")):
        st = p.stat()
        files.append({
            "name": p.name,
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return {"data_dir": str(DATA_DIR), "count": len(files), "files": files}


def read_saved_law(filename: str, offset: int = 0, max_chars: int = 5000) -> dict:
    """Read a slice of a law text file previously saved under ``data/``.

    Args:
        filename: A plain file name within ``data/`` (e.g. "明治二十九年法律第八十九号.txt").
            Paths and subdirectories are rejected.
        offset: Character offset to start from, for paging through long laws.
        max_chars: Maximum characters to return (default 5000).
    """
    if Path(filename).name != filename:
        raise ValueError("filename must be a plain file name within data/, not a path.")
    path = DATA_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"{filename} not found in {DATA_DIR}")
    text = path.read_text(encoding="utf-8")
    chunk = text[offset:offset + max_chars]
    return {
        "filename": filename,
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(chunk),
        "truncated": offset + max_chars < len(text),
        "text": chunk,
    }


def get_cache_stats() -> dict:
    """Report hit/miss counts for the internal law-resolution and law-XML caches."""
    return {
        "resolve_law": RESOLVE_LAW_CACHE.stats(),
        "law_xml": LAW_XML_CACHE.stats(),
        "law_element": ELEMENT_CACHE.stats(),
    }


def clear_cache() -> dict:
    """Clear the internal law-resolution, law-XML and element caches.

    Use after an amendment you need reflected before the TTL naturally
    expires; normal operation never requires this.
    """
    return {
        "resolve_law_entries_cleared": RESOLVE_LAW_CACHE.clear(),
        "law_xml_entries_cleared": LAW_XML_CACHE.clear(),
        "law_element_entries_cleared": ELEMENT_CACHE.clear(),
    }


if ADMIN_TOOLS:
    mcp.tool(annotations={"title": "List saved law files", "readOnlyHint": True,
                          "openWorldHint": False})(list_saved_laws)
    mcp.tool(annotations={"title": "Read a saved law file", "readOnlyHint": True,
                          "openWorldHint": False})(read_saved_law)
    mcp.tool(annotations={"title": "Cache statistics", "readOnlyHint": True,
                          "openWorldHint": False})(get_cache_stats)
    mcp.tool(annotations={"title": "Clear caches", "readOnlyHint": False,
                          "destructiveHint": True, "idempotentHint": True,
                          "openWorldHint": False})(clear_cache)


# Feature modules import helpers from this module, so they must be wired in
# after everything above is defined (bottom-of-file import avoids the cycle).
import english  # noqa: E402
import revision_diff  # noqa: E402
import xref  # noqa: E402

english.register(mcp)
revision_diff.register(mcp)
xref.register(mcp)


def main() -> None:
    """Console-script entrypoint (``e-gov-law-mcp``, see pyproject.toml)."""
    mcp.run()


if __name__ == "__main__":
    main()
