"""e-Gov Law MCP server.

Exposes Japan's e-Gov 法令API v2 (https://laws.e-gov.go.jp/api/2) as MCP tools:
search laws, full-text keyword search, fetch law text, and pull a specific
article out of a law by number. Articles are located by parsing the law's
standard XML and matching the ``<Article Num="...">`` attribute, which is far
more reliable than scraping flattened text.

The API needs no key. Common laws (六法 + key modern legislation) and their
abbreviations are mapped directly so a name like "民法" or "労基法" resolves
without a search round-trip.
"""

import base64
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from fastmcp import FastMCP

EGOV_BASE = "https://laws.e-gov.go.jp/api/2"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

mcp = FastMCP("e-gov-law")

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


def _get(path: str, **params) -> dict | None:
    """GET an e-Gov endpoint. Returns None when the API reports zero results.

    The API signals an empty result set with HTTP 404 and body code "404001";
    that is treated as "nothing found", while any other error is raised.
    """
    resp = requests.get(f"{EGOV_BASE}{path}", params=params,
                        headers={"Accept": "application/json"}, timeout=60)
    if resp.status_code == 404:
        try:
            if resp.json().get("code") == "404001":
                return None
        except ValueError:
            pass
    if not resp.ok:
        raise RuntimeError(f"e-Gov API error (HTTP {resp.status_code}) for {path}: {resp.text[:200]}")
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


def _article_to_num(article: str) -> tuple[str | None, int | None]:
    """Parse an article reference into the XML ``Num`` attribute and an optional 項.

    "192" -> ("192", None); "325条の3" -> ("325_3", None);
    "第9条第2項" -> ("9", 2); "第七百九条" -> ("709", None).
    """
    s = article.strip()
    paragraph = None
    para_match = re.search(r"第?\s*([0-9０-９一二三四五六七八九十百千]+)\s*項", s)
    if para_match:
        paragraph = _kanji_to_int(para_match.group(1).translate(_ZEN2HAN))

    body = s.split("項")[0]
    body = body.translate(_ZEN2HAN)
    branch = re.search(r"([0-9一二三四五六七八九十百千]+)\s*条?\s*の\s*([0-9一二三四五六七八九十百千]+)", body)
    if branch:
        main, sub = _kanji_to_int(branch.group(1)), _kanji_to_int(branch.group(2))
        if main is not None and sub is not None:
            return f"{main}_{sub}", paragraph
    main_match = re.search(r"([0-9一二三四五六七八九十百千]+)", body)
    if main_match:
        main = _kanji_to_int(main_match.group(1))
        if main is not None:
            return str(main), paragraph
    return None, paragraph


def _clean(el: ET.Element) -> str:
    """Concatenate an element's visible text, dropping XML indentation."""
    return "".join(t.strip() for t in el.itertext() if t.strip())


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
    if item.get("sentences"):
        brief["matched_sentences"] = [
            {"position": s.get("position"),
             "text": re.sub(r"</?span>", "", s.get("text", ""))}
            for s in item["sentences"]
        ]
    return brief


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


def _fetch_law_xml(law_id_or_num: str) -> tuple[ET.Element, dict, dict]:
    """Fetch a law's standard XML and return (root, law_info, revision_info)."""
    data = _get(f"/law_data/{law_id_or_num}", law_full_text_format="xml")
    if not data:
        raise RuntimeError(f"No law found for '{law_id_or_num}'")
    full_text = data.get("law_full_text")
    if not isinstance(full_text, str):
        raise RuntimeError("Unexpected law_full_text format from e-Gov API")
    xml = base64.b64decode(full_text).decode("utf-8")
    return ET.fromstring(xml), data.get("law_info", {}), data.get("revision_info", {})


@mcp.tool
def search_laws(law_title: str = "", law_type: str = "Act", law_num: str = "",
                limit: int = 10, offset: int = 0) -> dict:
    """Search Japanese laws by title (partial match), type, or law number.

    Args:
        law_title: Part of the law name, e.g. "個人情報" or "民法".
        law_type: One of Act, CabinetOrder, MinisterialOrdinance, Rule, Constitution.
            Defaults to "Act"; pass "" to search all types.
        law_num: Part of the 法令番号, e.g. "平成十五年法律".
        limit: Maximum laws to return (default 10).
        offset: Number of results to skip, for pagination (default 0).

    Returns matching laws with their ``law_id`` and ``law_num`` for use with
    ``get_law_content`` or ``find_law_article``.
    """
    params = {"limit": limit, "offset": offset}
    if law_title:
        params["law_title"] = law_title
    if law_type:
        params["law_type"] = law_type
    if law_num:
        params["law_num"] = law_num

    data = _get("/laws", **params)
    if not data:
        return {"total_matches": 0, "returned": 0, "laws": []}
    return {
        "total_matches": data.get("total_count"),
        "returned": len(data.get("laws", [])),
        "next_offset": data.get("next_offset"),
        "laws": [_law_brief(l) for l in data.get("laws", [])],
    }


@mcp.tool
def search_laws_by_keyword(keyword: str, law_type: str = "", limit: int = 100,
                           offset: int = 0) -> dict:
    """Full-text search across the body of every Japanese law.

    Args:
        keyword: Phrase to find in law text. Supports the e-Gov search syntax,
            including wildcards (``第*条``) and AND/OR/NOT operators.
        law_type: Optional type filter, e.g. "Act" (comma-separated for several).
        limit: Cap on the total number of matched sentences returned across all
            laws (e-Gov default 100, max 1000) — not a cap on the number of laws.
            Note: the e-Gov API returns no results when this is smaller than the
            sentence count of the first matching laws, so keep it >= 20.
        offset: Result offset for pagination (default 0).

    Returns the matching laws, each with the sentences where the keyword occurs.
    ``total_matches`` is the total count of matching sentence positions.
    """
    if not keyword or not keyword.strip():
        raise ValueError("keyword is required")
    params = {"keyword": keyword.strip(), "limit": limit, "offset": offset}
    if law_type:
        params["law_type"] = law_type

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
        "laws": [_law_brief(it) for it in data.get("items", [])],
    }


@mcp.tool
def find_law_article(law_name: str, article: str) -> dict:
    """Find a specific article within a Japanese law.

    Resolves common law names and abbreviations directly (e.g. "民法", "労基法"),
    then locates the article by its number in the law's XML.

    Args:
        law_name: Law name or abbreviation, e.g. "民法", "会社法", "道交法".
        article: Article number. Accepts plain numbers ("192"), branch articles
            ("325条の3"), kanji ("第七百九条"), and a paragraph ("第9条第2項").

    Returns the article's caption, title and full text. If a 項 (paragraph) is
    given, the matching paragraph text is included separately.
    """
    if not law_name.strip():
        raise ValueError("law_name is required")
    if not article.strip():
        raise ValueError("article is required")

    law_num, resolved_title, alias_source = _resolve_law(law_name)
    if not law_num:
        return {"found": False, "error": f"Law '{law_name}' not found",
                "search_law_name": law_name}

    num_attr, paragraph = _article_to_num(article)
    if not num_attr:
        return {"found": False, "error": f"Could not parse article '{article}'",
                "law_title": resolved_title, "law_num": law_num}

    root, info, rev = _fetch_law_xml(law_num)
    title = rev.get("law_title") or resolved_title
    article_el = root.find(f".//Article[@Num='{num_attr}']")

    result = {
        "found": article_el is not None,
        "search_law_name": law_name,
        "resolved_law_title": title,
        "law_num": info.get("law_num", law_num),
        "law_id": info.get("law_id"),
        "alias_expanded_from": alias_source,
        "search_article": article,
        "article_num": num_attr,
    }
    if article_el is None:
        result["error"] = f"Article '{article}' not found in {title}"
        return result

    result["article_caption"] = article_el.findtext("ArticleCaption")
    result["article_title"] = article_el.findtext("ArticleTitle")
    result["text"] = _clean(article_el)
    if paragraph is not None:
        para_el = article_el.find(f".//Paragraph[@Num='{paragraph}']")
        result["paragraph"] = paragraph
        result["paragraph_text"] = _clean(para_el) if para_el is not None else None
    return result


@mcp.tool
def get_law_content(law_id: str, full_text: bool = False, save: bool = False) -> dict:
    """Fetch a law's metadata, table of contents, and optionally its full text.

    Args:
        law_id: A ``law_id`` or 法令番号 from ``search_laws`` (e.g. "129AC0000000089"
            or "明治二十九年法律第八十九号").
        full_text: When True, include the full plain text of the law. Large laws
            are written to ``data/`` instead of returned inline (use a preview +
            ``read_saved_law``, or ``find_law_article`` for one article).
        save: When True, always write the full text to ``data/{law_num}.txt``.

    Returns law metadata, the chapter/section table of contents, and the article
    count. Full text is included or saved per the flags above.
    """
    root, info, rev = _fetch_law_xml(law_id)
    articles = root.findall(".//Article")
    toc = [_clean(t) for t in root.findall(".//Chapter/ChapterTitle")]

    out = {
        "law_title": rev.get("law_title"),
        "law_num": info.get("law_num"),
        "law_id": info.get("law_id"),
        "promulgation_date": info.get("promulgation_date"),
        "category": rev.get("category"),
        "article_count": len(articles),
        "table_of_contents": toc,
    }

    if not full_text and not save:
        return out

    body = root.find(".//LawBody")
    text = _clean(body) if body is not None else _clean(root)
    safe_num = re.sub(r"[^\w]", "_", info.get("law_num") or law_id)

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


@mcp.tool
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


@mcp.tool
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


if __name__ == "__main__":
    mcp.run()
