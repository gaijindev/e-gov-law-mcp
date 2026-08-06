"""English-translation tools backed by Japanese Law Translation (法令外国語訳).

The Ministry of Justice's JLT database (https://www.japaneselawtranslation.go.jp)
publishes unofficial English translations of Japanese statutes. It has no REST
API: search is a CSRF-protected POST to ``/en/laws/result/`` and a law is served
as a bilingual HTML page at ``/en/laws/view/{jlt_id}/je`` whose markup mirrors
the standard law XML schema (``div.Article`` › ``div.Paragraph`` ›
``div.ParagraphSentence``, Japanese line first, English line second). So this
module scrapes, deliberately and politely: every fetch goes through the shared
``server`` session and lands in a TTL cache.

Translations lag the Japanese text — often by years. The bilingual page states
the 法令番号 of the amendment it reflects (``最終更新``), and e-Gov states the
amendment currently in force, so staleness is *detectable*, not guessed: the two
法令番号 are compared and a warning is emitted when they differ. Every tool
returns a ``disclaimer``.

The module registers nothing at import time; ``register(mcp)`` defines the tools.
"""

import csv
import io
import re
import threading
from html.parser import HTMLParser

from cache import TTLCache, ttl_cached
from server import (
    MAX_ELEMENT_CHARS,
    _CACHE_TTL,
    _SESSION,
    _article_to_num,
    _get,
    _kanji_to_int,
    _resolve_law,
)

JLT_BASE = "https://www.japaneselawtranslation.go.jp"
JLT_TIMEOUT = 60

DISCLAIMER = (
    "Unofficial reference translation from the Japanese Law Translation database "
    "(Ministry of Justice). Only the original Japanese text has legal effect, and "
    "translations may lag the current Japanese text by several amendments. Check "
    "the Japanese article (find_law_article) before relying on any wording."
)

# Translations change far less often than the underlying statutes, and the
# dictionary is a versioned file that moves once or twice a year, so it gets a
# much longer TTL than the law pages.
CATALOGUE_CACHE = TTLCache(maxsize=256, ttl=_CACHE_TTL)
# A parsed law holds the full bilingual text of a statute, so this cache is
# deliberately shallow.
LAW_PAGE_CACHE = TTLCache(maxsize=8, ttl=_CACHE_TTL)
DICT_CACHE = TTLCache(maxsize=2, ttl=max(_CACHE_TTL, 604_800))

_HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Referer": f"{JLT_BASE}/en/laws"}

# One CSRF token is valid for the whole session cookie, so it is fetched once
# and only refreshed when the site answers 403.
_csrf_lock = threading.Lock()
_csrf_tokens: dict[str, str] = {}


# --- HTML → tiny DOM ----------------------------------------------------------
#
# The site's pages are ~500 KB of regular, well-nested divs. A full HTML parser
# would be a new dependency for what a 60-line stdlib walk does exactly as well.

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}
_SKIP_TAGS = {"script", "style"}

# Divs that hold one printed line of the statute. Their whole subtree is one line.
_LINE_CLASSES = {
    "ArticleCaption", "ParagraphSentence", "ItemSentence", "Sentence",
    "Subitem1Sentence", "Subitem2Sentence", "Subitem3Sentence",
    "PartTitle", "ChapterTitle", "SectionTitle", "SubsectionTitle",
    "DivisionTitle", "AppdxTableTitle", "SupplProvisionLabel", "RelatedArticleNum",
}
# Inline spans carrying the article/paragraph/item marker, which butts straight
# against the sentence text in the source and needs a space inserted.
_MARKER_CLASSES = {"ArticleTitle", "ParagraphNum", "ItemTitle",
                   "Subitem1Title", "Subitem2Title", "Subitem3Title"}
_DIVISION_CLASSES = ("Part", "Chapter", "Section", "Subsection", "Division")
_DIVISION_LABELS = {"Part": "編", "Chapter": "章", "Section": "節",
                    "Subsection": "款", "Division": "目"}


class _Node:
    """One element of the mini-DOM: tag, attributes, classes, children."""

    __slots__ = ("tag", "attrs", "classes", "children")

    def __init__(self, tag: str, attrs: dict):
        self.tag = tag
        self.attrs = attrs
        self.classes = set((attrs.get("class") or "").split())
        self.children: list = []

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    def elements(self):
        """Every descendant element, depth-first."""
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.elements()

    def find(self, cls: str) -> "_Node | None":
        return next((el for el in self.elements() if cls in el.classes), None)

    def find_all(self, cls: str) -> list["_Node"]:
        return [el for el in self.elements() if cls in el.classes]

    def find_by_id(self, node_id: str) -> "_Node | None":
        return next((el for el in self.elements() if el.id == node_id), None)

    def text(self) -> str:
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return "".join(parts)


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {})
        self._stack = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth or tag in _VOID_TAGS:
            return
        node = _Node(tag, dict(attrs))
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth or tag in _VOID_TAGS:
            return
        # Close the nearest matching ancestor; stray end tags are ignored.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if not self._skip_depth:
            self._stack[-1].children.append(data)


def _parse_html(html: str) -> _Node:
    builder = _DomBuilder()
    builder.feed(html)
    return builder.root


def _norm(text: str) -> str:
    """Collapse the source's indentation without eating real English spaces."""
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\s*[\r\n]+\s*", "", text)).strip()


# --- language split -----------------------------------------------------------

_JA_CHARS = re.compile(r"[぀-ヿ㐀-鿿豈-﫿ｦ-ﾝ]")


def _is_japanese(text: str) -> bool:
    """True when a line is the Japanese half of a bilingual pair.

    English lines quote Japanese law names only rarely and never in bulk, so a
    character-ratio test separates the pair far more reliably than "contains a
    kanji" (an English line may; ``削除`` has no kana at all).
    """
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return False
    return len(_JA_CHARS.findall(stripped)) / len(stripped) >= 0.3


def _lines(node: _Node) -> list[str]:
    """Flatten an element into printed lines, markers separated from the text."""
    out: list[str] = []

    def walk(el: _Node):
        if el.classes & _LINE_CLASSES:
            markers, body = [], []
            for child in el.children:
                if isinstance(child, str):
                    body.append(child)
                elif child.classes & _MARKER_CLASSES:
                    markers.append(_norm(child.text()))
                else:
                    body.append(child.text())
            line = " ".join(p for p in (" ".join(markers), _norm("".join(body))) if p)
            if line:
                out.append(line)
            return
        for child in el.children:
            if isinstance(child, _Node):
                walk(child)

    walk(node)
    return out


def _split_bilingual(node: _Node) -> tuple[str, str]:
    """Return (japanese_text, english_text) for a bilingual element."""
    ja, en = [], []
    for line in _lines(node):
        (ja if _is_japanese(line) else en).append(line)
    return "\n".join(ja), "\n".join(en)


# --- article numbering --------------------------------------------------------

_JA_NUM = r"[0-9０-９一二三四五六七八九十百千]+"
_JA_ARTICLE_RE = re.compile(rf"第\s*({_JA_NUM})\s*条((?:の\s*{_JA_NUM})*)")
_JA_RANGE_RE = re.compile(rf"第\s*({_JA_NUM})\s*条から第\s*({_JA_NUM})\s*条まで")
_EN_ARTICLE_RE = re.compile(r"Articles?\s+(\d+(?:-\d+)*)", re.I)
_EN_RANGE_RE = re.compile(r"Articles\s+(\d+)\s+through\s+(\d+)", re.I)


def _article_key_from_ja(title: str) -> tuple[str | None, tuple[int, int] | None]:
    """Parse a Japanese ArticleTitle into ("32_2", None) or (None, (29, 31)).

    Deleted articles are collapsed into a range title
    (第二十九条から第三十一条まで), exactly as the Japanese XML collapses them
    into ``Num="29:31"``.
    """
    if not title:
        return None, None
    rng = _JA_RANGE_RE.search(title)
    if rng:
        lo, hi = _kanji_to_int(rng.group(1)), _kanji_to_int(rng.group(2))
        if lo is not None and hi is not None:
            return None, (lo, hi)
    match = _JA_ARTICLE_RE.search(title)
    if not match:
        return None, None
    main = _kanji_to_int(match.group(1))
    if main is None:
        return None, None
    parts = [str(main)]
    for branch in re.findall(_JA_NUM, match.group(2) or ""):
        value = _kanji_to_int(branch)
        if value is not None:
            parts.append(str(value))
    return "_".join(parts), None


def _article_key_from_en(title: str) -> tuple[str | None, tuple[int, int] | None]:
    """Parse an English ArticleTitle: "Article 32-2" or "Articles 29 through 31"."""
    if not title:
        return None, None
    rng = _EN_RANGE_RE.search(title)
    if rng:
        return None, (int(rng.group(1)), int(rng.group(2)))
    match = _EN_ARTICLE_RE.search(title)
    if not match:
        return None, None
    return match.group(1).replace("-", "_"), None


def _key_matches(wanted: str, key: str | None, rng: tuple[int, int] | None) -> bool:
    if key is not None and key == wanted:
        return True
    if rng is None or "_" in wanted or not wanted.isdigit():
        return False
    return rng[0] <= int(wanted) <= rng[1]


# --- HTTP ---------------------------------------------------------------------


def _jlt_get(path: str) -> str:
    resp = _SESSION.get(f"{JLT_BASE}{path}", headers=_HTML_HEADERS, timeout=JLT_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(
            f"Japanese Law Translation error (HTTP {resp.status_code}) for {path}")
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def _csrf_token(form_path: str, refresh: bool = False) -> str:
    """Fetch (and remember) the CSRF token a POST form on ``form_path`` needs.

    The token is bound to the session cookie the same GET sets, so both live on
    the shared ``server`` session and one token serves many POSTs.
    """
    with _csrf_lock:
        if not refresh and form_path in _csrf_tokens:
            return _csrf_tokens[form_path]
    html = _jlt_get(form_path)
    match = re.search(r'name="_csrfToken"[^>]*value="([^"]+)"', html)
    if not match:
        raise RuntimeError(f"No CSRF token on {form_path}; the site's forms changed.")
    token = match.group(1)
    with _csrf_lock:
        _csrf_tokens[form_path] = token
    return token


def _jlt_post(path: str, form_path: str, data: dict):
    """POST a JLT form, refreshing the CSRF token once if the site rejects it."""
    for attempt in range(2):
        payload = dict(data, _csrfToken=_csrf_token(form_path, refresh=attempt > 0))
        resp = _SESSION.post(f"{JLT_BASE}{path}", data=payload,
                             headers=_HTML_HEADERS, timeout=JLT_TIMEOUT)
        if resp.status_code == 403 and attempt == 0:
            continue
        if not resp.ok:
            raise RuntimeError(
                f"Japanese Law Translation error (HTTP {resp.status_code}) for {path}")
        return resp
    raise RuntimeError(f"Japanese Law Translation rejected the request to {path} (403)")


# --- catalogue search ---------------------------------------------------------


def _parse_search_results(html: str) -> list[dict]:
    """Pull the result list off a /en/laws/result/ page."""
    root = _parse_html(html)
    results = []
    for item in root.find_all("search-result"):
        for li in (el for el in item.children if isinstance(el, _Node) and el.tag == "li"):
            link = next((el for el in li.elements() if el.tag == "a"), None)
            if link is None:
                continue
            href = link.attrs.get("href", "")
            match = re.search(r"/laws/view/(\d+)", href)
            if not match:
                continue
            entry = {
                "jlt_id": match.group(1),
                "english_title": _norm(link.text()),
                "url": f"{JLT_BASE}/en/laws/view/{match.group(1)}",
            }
            law_type = li.find("result-law-type")
            if law_type is not None:
                entry["law_type"] = _norm(law_type.text())
            for info in li.find_all("result-info-item"):
                text = _norm(info.text())
                label, _, value = text.partition(":")
                key = {"Law Number": "law_num_en",
                       "Translated Date": "translated_date",
                       "Dictionary Version": "dictionary_version"}.get(label.strip())
                if key:
                    entry[key] = value.strip()
            results.append(entry)
    return results


def _parse_result_total(html: str) -> int | None:
    match = re.search(r"Showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)", html)
    return int(match.group(1).replace(",", "")) if match else None


@ttl_cached(CATALOGUE_CACHE)
def _search_catalogue(query: str, include_untranslated: bool = False) -> dict:
    """Search JLT by law title (the ``yo`` field accepts English or Japanese)."""
    data = {"yo": query, "amb": "on", "ia": "03", "ja": "04"}
    if include_untranslated:
        data["la"] = "01"
    resp = _jlt_post("/en/laws/result/", "/en/laws", data)
    html = resp.text
    return {"results": _parse_search_results(html), "total": _parse_result_total(html)}


# --- law page -----------------------------------------------------------------


def _parse_law_page(html: str) -> dict:
    """Parse a bilingual /en/laws/view/{id}/je page into titles + articles."""
    root = _parse_html(html)
    info = root.find_by_id("lawInfo")
    titles = [_norm(t.text()) for t in info.find_all("title")] if info else []
    versions = [_norm(v.text()) for v in info.find_all("last-version")] if info else []

    def pick(values, japanese: bool):
        return next((v for v in values if _is_japanese(v) == japanese), None)

    out = {
        "law_title_ja": pick(titles, True),
        "law_title_en": pick(titles, False),
        "translated_version_ja": _strip_label(pick(versions, True)),
        "translated_version_en": _strip_label(pick(versions, False)),
        "table_of_contents": [],
        "articles": [],
    }

    body = root.find_by_id("lawBody")
    if body is None:
        return out

    out["table_of_contents"] = _division_toc(body)
    for article in body.find_all("Article"):
        out["articles"].append(_article_entry(article))
    return out


def _strip_label(value: str | None) -> str | None:
    """Drop the "最終更新：" / "Last Version： " label off a version line."""
    if not value:
        return None
    return re.sub(r"^(最終更新|Last Version)\s*[:：]\s*", "", value).strip() or None


def _section_of(article: _Node, body: _Node) -> str:
    """Which part of the statute an article sits in, from its anchor id.

    Anchor ids encode the containing block (``je_ch4at1`` main provision,
    ``je_s1at2`` 附則), which is cheaper and steadier than re-walking ancestors.
    """
    del body
    node_id = article.id
    if re.match(r"^\w*_?s\d+at", node_id):
        return "SupplProvision"
    return "MainProvision"


def _article_entry(article: _Node) -> dict:
    titles = [_norm(t.text()) for t in article.find_all("ArticleTitle")]
    captions = [_norm(c.text()) for c in article.find_all("ArticleCaption")]
    title_ja = next((t for t in titles if _is_japanese(t)), None)
    title_en = next((t for t in titles if not _is_japanese(t)), None)
    key, rng = _article_key_from_ja(title_ja or "")
    if key is None and rng is None:
        key, rng = _article_key_from_en(title_en or "")
    ja_text, en_text = _split_bilingual(article)
    return {
        "article_key": key,
        "article_range": list(rng) if rng else None,
        "article_title_ja": title_ja,
        "article_title_en": title_en,
        "article_caption_ja": next((c for c in captions if _is_japanese(c)), None),
        "article_caption_en": next((c for c in captions if not _is_japanese(c)), None),
        "section": _section_of(article, article),
        "text_ja": ja_text,
        "text_en": en_text,
    }


def _division_toc(node: _Node) -> list[dict]:
    """Walk 編/章/節/款/目 divs into a nested bilingual table of contents."""
    entries = []
    for child in node.children:
        if not isinstance(child, _Node):
            continue
        level = next((c for c in _DIVISION_CLASSES if c in child.classes), None)
        if level is None:
            entries.extend(_division_toc(child))
            continue
        titles = [_norm(t.text()) for t in child.find_all(f"{level}Title")]
        articles = child.find_all("Article")
        keys = [k for k in (_article_key_from_ja(
            next((_norm(t.text()) for t in a.find_all("ArticleTitle")
                  if _is_japanese(_norm(t.text()))), ""))[0] for a in articles) if k]
        entry = {
            "level": level,
            "level_ja": _DIVISION_LABELS[level],
            "title_ja": next((t for t in titles if _is_japanese(t)), None),
            "title_en": next((t for t in titles if not _is_japanese(t)), None),
            "article_count": len(articles),
        }
        if keys:
            entry["article_range"] = (keys[0] if keys[0] == keys[-1]
                                      else f"{keys[0]}–{keys[-1]}")
        children = _division_toc(child)
        if children:
            entry["children"] = children
        entries.append(entry)
    return entries


@ttl_cached(LAW_PAGE_CACHE)
def _fetch_law(jlt_id: str) -> dict:
    """Fetch and parse the bilingual page for a JLT law id."""
    parsed = _parse_law_page(_jlt_get(f"/en/laws/view/{jlt_id}/je"))
    parsed["jlt_id"] = jlt_id
    parsed["url"] = f"{JLT_BASE}/en/laws/view/{jlt_id}"
    return parsed


# --- staleness ----------------------------------------------------------------


def _current_amendment(law_num: str) -> dict | None:
    """The amendment 法令番号 e-Gov currently has in force for a law."""
    try:
        data = _get("/laws", law_num=law_num, limit=1)
    except Exception:
        return None
    if not data or not data.get("laws"):
        return None
    return data["laws"][0].get("revision_info") or None


def _staleness(translated_version_ja: str | None, law_num: str | None) -> dict:
    """Compare the amendment the translation reflects with the one in force."""
    out = {
        "translation_reflects_amendment": translated_version_ja,
        "current_amendment": None,
        "is_stale": None,
        "warning": None,
    }
    if not law_num:
        out["warning"] = ("Could not identify the Japanese law on e-Gov, so the "
                          "translation's currency could not be checked.")
        return out
    rev = _current_amendment(law_num)
    if not rev:
        out["warning"] = ("e-Gov did not return a current revision, so the "
                          "translation's currency could not be checked.")
        return out
    current = rev.get("amendment_law_num")
    out["current_amendment"] = current
    out["current_amendment_enforced"] = rev.get("amendment_enforcement_date")
    if not translated_version_ja or not current:
        out["warning"] = ("The translation does not state which amendment it "
                          "reflects; assume it may be out of date.")
        return out
    out["is_stale"] = translated_version_ja != current
    if out["is_stale"]:
        out["warning"] = (
            f"STALE: the translation reflects {translated_version_ja}, but the "
            f"Japanese law now in force reflects {current} (enforced "
            f"{rev.get('amendment_enforcement_date')}). Articles amended since "
            "then are NOT in this translation — verify against the Japanese text.")
    return out


# --- dictionary ---------------------------------------------------------------

_DICT_COLUMNS = {
    "用語": "term_ja",
    "読み": "reading",
    "訳語候補番号": "candidate_no",
    "訳語候補": "term_en",
    "使い分け基準": "usage_note",
    "用例(和文)": "example_ja",
    "用例(英文)": "example_en",
    "用例出典": "example_source",
    "注釈1": "note_1",
    "注釈2": "note_2",
}


def _parse_dictionary(csv_text: str) -> list[dict]:
    """Parse the Standard Legal Terms Dictionary CSV into entry dicts."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    entries = []
    for row in rows:
        entry = {out_key: (row.get(src) or "").strip()
                 for src, out_key in _DICT_COLUMNS.items()}
        if entry["term_ja"] or entry["term_en"]:
            entries.append(entry)
    return entries


@ttl_cached(DICT_CACHE)
def _dictionary(version: str = "") -> dict:
    """Download and parse the newest Standard Legal Terms Dictionary (UTF-8 CSV)."""
    page = _jlt_get("/en/dicts/download")
    versions = re.findall(r'<option value="([\d.]+)"', page)
    chosen = version or (versions[0] if versions else "19.0")
    resp = _jlt_post("/en/dicts/download", "/en/dicts/download",
                     {"version": chosen, "ftype": "4"})
    resp.encoding = "utf-8"
    return {"version": chosen, "entries": _parse_dictionary(resp.text)}


def _lookup(entries: list[dict], term: str, direction: str) -> list[dict]:
    """Rank dictionary entries against a term: exact, then prefix, then substring."""
    needle = term.strip().lower()
    fields = (["term_ja", "reading"] if direction == "ja"
              else ["term_en"] if direction == "en"
              else ["term_ja", "reading", "term_en"])
    scored = []
    for entry in entries:
        best = None
        for field in fields:
            value = (entry.get(field) or "").lower()
            if not value:
                continue
            if value == needle:
                rank = 0
            elif value.startswith(needle):
                rank = 1
            elif needle in value:
                rank = 2
            else:
                continue
            best = rank if best is None else min(best, rank)
        if best is not None:
            scored.append((best, len(entry.get("term_ja") or ""), entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored]


def _detect_direction(term: str) -> str:
    return "ja" if _JA_CHARS.search(term) else "en"


# --- shared response helpers --------------------------------------------------


def _page(text: str, offset: int) -> dict:
    """Cap a text field at MAX_ELEMENT_CHARS with offset paging, house style."""
    chunk = text[offset:offset + MAX_ELEMENT_CHARS]
    out = {
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(chunk),
        "truncated": offset + MAX_ELEMENT_CHARS < len(text),
        "text": chunk,
    }
    if out["truncated"]:
        out["note"] = (f"Truncated at {MAX_ELEMENT_CHARS} characters. Call again "
                       f"with offset={offset + MAX_ELEMENT_CHARS} for the next page.")
    return out


_ERA_FIRST_YEAR = {"明治": 1868, "大正": 1912, "昭和": 1926, "平成": 1989, "令和": 2019}
_JA_LAW_NUM_RE = re.compile(rf"^(明治|大正|昭和|平成|令和)({_JA_NUM})年.*?第({_JA_NUM})号")
_EN_LAW_NUM_RE = re.compile(r"No\.\s*(\d+)\s+of\s+(\d{4})", re.I)


def _ja_law_num_signature(law_num: str | None) -> tuple[int, int] | None:
    """Reduce "昭和二十六年政令第三百十九号" to its (number, Gregorian year)."""
    match = _JA_LAW_NUM_RE.match((law_num or "").strip())
    if not match:
        return None
    year, number = _kanji_to_int(match.group(2)), _kanji_to_int(match.group(3))
    if year is None or number is None:
        return None
    return number, _ERA_FIRST_YEAR[match.group(1)] + year - 1


def _en_law_num_signature(law_num_en: str | None) -> tuple[int, int] | None:
    """Reduce "Cabinet Order No. 319 of 1951" to its (number, year)."""
    match = _EN_LAW_NUM_RE.search(law_num_en or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _rank_candidates(candidates: list[dict], law_num: str | None) -> list[dict]:
    """Order catalogue hits so the statute itself beats everything about it.

    A title search for 入管法 also returns its enforcement order, four ministerial
    orders defining its criteria, and outlines of acts that amended it. Two
    signals separate them: the 法令番号, which pins the exact law when e-Gov gave
    us one, and the absence of a translation date, which marks the "outline" and
    untranslated-title entries as not-a-translation.
    """
    wanted = _ja_law_num_signature(law_num)
    return sorted(candidates, key=lambda c: (
        0 if wanted and _en_law_num_signature(c.get("law_num_en")) == wanted else 1,
        0 if c.get("translated_date") else 1,
        len(c.get("english_title") or ""),
    ))


def _match_translation(law_name: str) -> tuple[dict | None, list[dict], str | None, str | None]:
    """Find the JLT entry for a Japanese law name.

    Returns (best_match, ranked_candidates, resolved_japanese_title, law_num).
    The Japanese side is resolved through e-Gov first so aliases ("労基法") and
    partial names reach JLT as the formal title it indexes, and so the 法令番号
    is available to disambiguate the catalogue hits.
    """
    law_num, resolved_title, _ = _resolve_law(law_name)
    query = resolved_title or law_name.strip()
    candidates = _search_catalogue(query)["results"]
    if not candidates and query != law_name.strip():
        candidates = _search_catalogue(law_name.strip())["results"]
    if not candidates:
        return None, [], resolved_title, law_num

    ranked = _rank_candidates(candidates, law_num)
    return ranked[0], ranked, resolved_title, law_num


def _same_law(candidates: list[dict], law_num: str | None) -> list[dict]:
    """Catalogue hits that are the *same* statute as ``law_num``.

    Long codes are translated in instalments — the Civil Code is four separate
    JLT entries ("Part I, Part II and Part III", "Part IV and Part V", …) sharing
    one 法令番号 — so an article missing from the first entry may simply live in
    a sibling.
    """
    wanted = _ja_law_num_signature(law_num)
    if not wanted:
        return []
    return [c for c in candidates
            if _en_law_num_signature(c.get("law_num_en")) == wanted]


def _find_article(law: dict, wanted: str) -> dict | None:
    """The article entry matching ``wanted``, preferring the main provision."""
    matches = [a for a in law.get("articles", [])
               if _key_matches(wanted, a["article_key"],
                               tuple(a["article_range"]) if a["article_range"] else None)]
    main = [a for a in matches if a["section"] == "MainProvision"]
    return (main or matches or [None])[0]


def register(mcp) -> None:
    """Define the English-translation tools on a FastMCP instance."""

    @mcp.tool(annotations={"title": "Fetch a law's official English translation",
                           "readOnlyHint": True, "openWorldHint": True})
    def get_english_law(law_name: str, article: str = "", offset: int = 0) -> dict:
        """Fetch the English translation of a Japanese law, whole or by article.

        Source is the Ministry of Justice's Japanese Law Translation database —
        unofficial reference translations that often lag the Japanese text. The
        result states which amendment the translation reflects and warns when
        e-Gov shows a newer amendment in force.

        Use this when the user needs Japanese law *in English*; use
        ``find_law_article`` for the authoritative Japanese text.

        Args:
            law_name: Japanese law name, abbreviation or 法令番号 — "労働基準法",
                "労基法", "昭和二十二年法律第四十九号". English titles work too.
            article: Article to return, e.g. "32", "第32条", "32条の2"
                (条の枝番 supported). Empty returns the table of contents and
                structure summary instead of body text.
            offset: Character offset into the returned text, for paging.

        Returns the Japanese and English titles, the translation's date and the
        amendment it reflects, a staleness warning when detectable, and either a
        bilingual table of contents (``article=""``) or the requested article's
        English and Japanese text (capped at 15,000 characters — page with
        ``offset``).
        """
        if not law_name.strip():
            raise ValueError("law_name is required, e.g. '労働基準法' or '労基法'")

        best, candidates, resolved_title, law_num = _match_translation(law_name)
        if best is None:
            return {
                "found": False,
                "search_law_name": law_name,
                "resolved_japanese_title": resolved_title,
                "error": (f"No English translation found for '{law_name}' in the "
                          "Japanese Law Translation database."),
                "hint": ("Only a fraction of Japanese laws are translated. Try "
                         "search_english_laws with a distinctive English or "
                         "Japanese keyword, or read the Japanese text with "
                         "find_law_article."),
                "disclaimer": DISCLAIMER,
            }

        law = _fetch_law(best["jlt_id"])
        result = {
            "found": True,
            "search_law_name": law_name,
            "jlt_id": best["jlt_id"],
            "url": law["url"],
            "law_title_ja": law.get("law_title_ja") or resolved_title,
            "law_title_en": law.get("law_title_en") or best.get("english_title"),
            "law_type": best.get("law_type"),
            "law_num_ja": law_num,
            "law_num_en": best.get("law_num_en"),
            "translated_date": best.get("translated_date"),
            "dictionary_version": best.get("dictionary_version"),
            "translated_version_ja": law.get("translated_version_ja"),
            "translated_version_en": law.get("translated_version_en"),
            "staleness": _staleness(law.get("translated_version_ja"), law_num),
            "disclaimer": DISCLAIMER,
        }
        if len(candidates) > 1:
            result["other_translations"] = [
                {"jlt_id": c["jlt_id"], "english_title": c["english_title"]}
                for c in candidates if c["jlt_id"] != best["jlt_id"]
            ][:10]

        articles = law.get("articles", [])
        if not article.strip():
            result["table_of_contents"] = law.get("table_of_contents")
            result["article_count"] = len(articles)
            result["articles_index"] = [
                {"article": a["article_key"] or (
                    f"{a['article_range'][0]}–{a['article_range'][1]}"
                    if a["article_range"] else None),
                 "caption_en": a["article_caption_en"],
                 "section": a["section"]}
                for a in articles
            ][:400]
            result["hint"] = ("Call again with article='32' (条の枝番 such as "
                              "'32条の2' work) for a provision's English text.")
            return result

        wanted, paragraph, item = _article_to_num(article)
        result["search_article"] = article
        result["article_num"] = wanted
        if not wanted:
            result["found"] = False
            result["error"] = f"Could not parse article reference '{article}'."
            return result

        chosen = _find_article(law, wanted)
        if chosen is None:
            # A long code is translated in instalments under one 法令番号; the
            # article may be in a sibling entry rather than absent.
            for sibling in _same_law(candidates, law_num)[:4]:
                if sibling["jlt_id"] == best["jlt_id"]:
                    continue
                sibling_law = _fetch_law(sibling["jlt_id"])
                chosen = _find_article(sibling_law, wanted)
                if chosen is not None:
                    law = sibling_law
                    result.update({
                        "jlt_id": sibling["jlt_id"],
                        "url": sibling_law["url"],
                        "law_title_en": sibling_law.get("law_title_en"),
                        "translated_date": sibling.get("translated_date"),
                        "note": (f"Found in a companion translation volume: "
                                 f"{sibling.get('english_title')}."),
                    })
                    break
        if chosen is None:
            result["found"] = False
            result["error"] = (f"Article {wanted} is not in the English translation "
                               f"of {result['law_title_ja']}.")
            result["hint"] = ("Translations are sometimes extracts (［抄］) covering "
                              "only part of a statute. Call with article='' for the "
                              "table of contents, or use find_law_article for the "
                              "Japanese text.")
            return result

        result.update({
            "article_title_ja": chosen["article_title_ja"],
            "article_title_en": chosen["article_title_en"],
            "article_caption_ja": chosen["article_caption_ja"],
            "article_caption_en": chosen["article_caption_en"],
            "section": chosen["section"],
            "text_ja": chosen["text_ja"][:MAX_ELEMENT_CHARS],
        })
        if paragraph is not None:
            result["requested_paragraph"] = paragraph
        if item is not None:
            result["requested_item"] = item
        if chosen["article_range"]:
            result["deleted_range"] = chosen["article_range"]
        result["english"] = _page(chosen["text_en"], offset)
        return result

    @mcp.tool(annotations={"title": "Search translated laws (English catalogue)",
                           "readOnlyHint": True, "openWorldHint": True})
    def search_english_laws(query: str, limit: int = 20,
                            include_untranslated: bool = False) -> dict:
        """Search the Japanese Law Translation catalogue by law title.

        Accepts English ("labor standards", "immigration") or Japanese
        ("労働基準法") keywords and returns the ``jlt_id`` plus the titles needed
        to pick a law, which ``get_english_law`` then fetches.

        Args:
            query: Title keyword(s), English or Japanese.
            limit: Maximum matches to return (default 20).
            include_untranslated: Also list laws whose titles are indexed but
                which have no translation yet — useful for confirming that a law
                genuinely has not been translated.

        Returns matching laws with their English title, law type, English law
        number, translation date and ``jlt_id``.
        """
        if not query.strip():
            raise ValueError("query is required, e.g. 'labor standards' or '入管'")
        found = _search_catalogue(query.strip(), include_untranslated)
        results = found["results"][:max(1, limit)]
        return {
            "query": query,
            "total_matches": found["total"],
            "returned": len(results),
            "laws": results,
            "hint": ("Pass a law's Japanese or English title to get_english_law "
                     "for its translated text."),
            "disclaimer": DISCLAIMER,
        }

    @mcp.tool(annotations={"title": "Look up a Japanese legal term in English",
                           "readOnlyHint": True, "openWorldHint": True})
    def lookup_legal_term(term: str, direction: str = "auto", limit: int = 15) -> dict:
        """Translate a Japanese legal term using the official standard dictionary.

        Backed by the Ministry of Justice's Standard Legal Terms Dictionary
        (標準対訳辞書) — the controlled vocabulary every JLT translation is
        produced against, so its renderings match the statute translations.

        Args:
            term: The term to look up — Japanese ("善意", "労働者") or English
                ("good faith", "worker").
            direction: "ja" (Japanese in), "en" (English in) or "auto" (default,
                detects from the script used).
            limit: Maximum entries to return (default 15).

        Returns matched entries with the Japanese term, its reading, the approved
        English rendering, the usage criterion that distinguishes competing
        renderings (使い分け基準) and a bilingual example with its source article.
        """
        if not term.strip():
            raise ValueError("term is required, e.g. '善意' or 'good faith'")
        direction = (direction or "auto").strip().lower()
        if direction not in ("auto", "ja", "en"):
            raise ValueError("direction must be 'auto', 'ja' or 'en'")
        resolved = _detect_direction(term) if direction == "auto" else direction

        dictionary = _dictionary()
        matches = _lookup(dictionary["entries"], term, resolved)
        return {
            "term": term,
            "direction": resolved,
            "dictionary_version": dictionary["version"],
            "total_matches": len(matches),
            "returned": len(matches[:max(1, limit)]),
            "entries": matches[:max(1, limit)],
            "found": bool(matches),
            "hint": None if matches else (
                "No dictionary entry. Try a shorter stem (辞書 is indexed by "
                "headword, not inflected form) or the other direction."),
            "disclaimer": DISCLAIMER,
        }

    return {"get_english_law": get_english_law,
            "search_english_laws": search_english_laws,
            "lookup_legal_term": lookup_legal_term}
