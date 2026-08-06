"""定義語 and 準用/cross-reference extraction for the e-Gov Law MCP server.

Japanese statutes carry two kinds of structure that a plain article fetch
loses. The first is vocabulary: a term used in 第三十九条 is often defined in
第二条 (（定義）) or parenthetically somewhere else entirely
(``（以下「特定技能雇用契約」という。）``), so reading one article without its
definitions misreads it. The second is the reference graph: articles point at
each other with absolute (``第三十六条``), relative (``前条``/``前二項``) and
準用 ("apply mutatis mutandis") references, and at other laws by name plus
法令番号.

This module extracts both with regexes over the law's own XML — the whole
statute is fetched once (through ``server``'s cached ``_fetch_law_xml``) and
scanned locally. It is deliberately a pattern matcher, not a parser: the
grammar of statutory drafting is regular enough that precision is achievable,
but recall is not guaranteed. Every extraction therefore carries the source
text snippet it came from, so the calling agent can verify a hit and notice a
miss.

Nothing is registered at import time; ``register(mcp)`` attaches the tools.
"""

import re
import xml.etree.ElementTree as ET

from server import (
    _article_to_num,
    _clean,
    _fetch_law_xml,
    _kanji_to_int,
    _law_key,
    _resolution_error,
    _rev_meta,
)

# Terms found per call, and the character budget for a definition snippet.
MAX_TERMS = 100
MAX_DEF_CHARS = 300
MAX_SNIPPET_CHARS = 160

_KANJI_DIGIT_CHARS = "〇一二三四五六七八九"

# Any statutory numeral: kanji, ASCII or full-width.
_NUM = r"[0-9０-９一二三四五六七八九十百千]+"

_BEST_EFFORT = ("Regex-based best-effort extraction over the statute's own XML. "
                "Every entry carries the source snippet — verify it before "
                "relying on it, and expect misses on unusual drafting.")


def _int_to_kanji(n: int) -> str:
    """Render an integer as a statutory kanji numeral (24 -> '二十四').

    ``server`` has ``_kanji_to_int`` but not its inverse, and both directions
    are needed: XML ``Num`` attributes are ASCII while body text is kanji.
    """
    if n < 0:
        raise ValueError("negative article numbers do not occur in statutes")
    if n == 0:
        return "〇"
    out, rest = "", n
    for value, label in ((1000, "千"), (100, "百"), (10, "十")):
        digit, rest = divmod(rest, value)
        if digit:
            out += ("" if digit == 1 else _KANJI_DIGIT_CHARS[digit]) + label
    if rest:
        out += _KANJI_DIGIT_CHARS[rest]
    return out


def _num_to_kanji_article(num_attr: str) -> str:
    """XML ``Num`` -> the way the article is written in body text.

    "24" -> 第二十四条; "24_2" -> 第二十四条の二.
    """
    parts = num_attr.split("_")
    try:
        head = _int_to_kanji(int(parts[0]))
    except ValueError:
        return f"第{num_attr}条"
    tail = "".join(f"の{_int_to_kanji(int(p))}" for p in parts[1:] if p.isdigit())
    return f"第{head}条{tail}"


def _norm_num(text: str) -> int | None:
    """Parse a numeral written in kanji, ASCII or full-width digits."""
    return _kanji_to_int(text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))


# --- definitions --------------------------------------------------------------

# 「この法律において「X」とは、…をいう。」 and its 「Xとは」 variants.
_TOWA_RE = re.compile(r"「(?P<term>[^「」]{1,40})」とは[、,]?(?P<def>[^。]{0,300}?)(?:をいう|という)")

# 「（以下「X」という。）」 / 「（以下この条において単に「X」という。）」 /
# 「（以下「X等」と総称する。）」 — the scope qualifier is optional and is what
# limits the definition to a 条/章/節 instead of the whole law.
_INLINE_DEF_RE = re.compile(
    r"（以下[、]?(?P<scope>この[^「」（）]{0,12}?において)?(?:単に)?"
    r"「(?P<term>[^「」]{1,40})」と(?:いう|総称する)。?）"
)

# The lead-in of a 各号-style definition article.
_KAKUGO_LEAD = re.compile(r"次の各号に掲げる用語の意義は[、]?当該各号に定めるところによる")


def _sentence_snippet(text: str, start: int, end: int, limit: int) -> str:
    """The 。-delimited sentence containing [start, end), capped at ``limit``."""
    begin = text.rfind("。", 0, start) + 1
    stop = text.find("。", end)
    stop = len(text) if stop == -1 else stop + 1
    snippet = text[begin:stop].strip()
    return snippet[:limit]


def _main_articles(root: ET.Element) -> list[ET.Element]:
    """Main-body articles in document order (附則 and 別表 excluded)."""
    main = root.find(".//MainProvision")
    return main.findall(".//Article") if main is not None else []


def _article_ident(article: ET.Element) -> dict:
    num = article.get("Num") or ""
    return {
        "article": num,
        "article_display": (article.findtext("ArticleTitle")
                            or _num_to_kanji_article(num)).strip(),
        "caption": (article.findtext("ArticleCaption") or "").strip() or None,
    }


def _paragraphs(article: ET.Element) -> list[tuple[int | None, ET.Element]]:
    """(項番号, element) pairs; a Paragraph without a numeric Num sorts as None."""
    out = []
    for para in article.findall("Paragraph"):
        num = para.get("Num")
        out.append((int(num) if num and num.isdigit() else None, para))
    return out


def _item_definition(item: ET.Element) -> tuple[str, str] | None:
    """Definiendum and definiens of one 号 in a 各号-style definition article.

    The definiendum is the item's first 「quoted」 term; 定義 items drafted as
    two Columns (term | meaning) instead of quoting are handled as a fallback.
    """
    # ItemTitle ("一") would otherwise be glued onto the front of the snippet.
    sentence = item.find("ItemSentence")
    text = _clean(sentence if sentence is not None else item)
    quoted = re.search(r"「(?P<term>[^「」]{1,40})」", text)
    if quoted:
        return quoted.group("term"), text
    columns = item.findall(".//Column")
    if len(columns) >= 2:
        term = _clean(columns[0]).strip()
        if 0 < len(term) <= 40:
            return term, text
    return None


def _collect_definitions(root: ET.Element) -> list[dict]:
    """Every defined term the patterns can find, in document order."""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(term: str, snippet: str, ident: dict, scope: str, kind: str,
            paragraph: int | None = None, item: int | None = None) -> None:
        term = term.strip()
        if not term or (term, ident["article"]) in seen:
            return
        seen.add((term, ident["article"]))
        entry = {
            "term": term,
            "definition_snippet": snippet[:MAX_DEF_CHARS],
            "defined_in": dict(ident),
            "scope": scope,
            "definition_kind": kind,
        }
        if paragraph is not None:
            entry["defined_in"]["paragraph"] = paragraph
        if item is not None:
            entry["defined_in"]["item"] = item
        found.append(entry)

    for article in _main_articles(root):
        ident = _article_ident(article)
        is_definition_article = "定義" in (ident["caption"] or "")

        for para_num, para in _paragraphs(article):
            para_text = _clean(para)

            # 各号-style: the paragraph announces the list, each 号 defines a term.
            if _KAKUGO_LEAD.search(para_text):
                for item in para.findall(".//Item"):
                    pair = _item_definition(item)
                    if pair is None:
                        continue
                    term, item_text = pair
                    item_num = item.get("Num")
                    add(term, item_text, ident, "この法律", "enumerated_item",
                        paragraph=para_num,
                        item=int(item_num) if item_num and item_num.isdigit() else None)
                continue

            # 「X」とは、…をいう。 — a definition proper. Restricted to articles
            # captioned （定義） unless the paragraph opens with この法律において,
            # since 「…」とは also appears in ordinary substantive drafting.
            if is_definition_article or "この法律において" in para_text:
                for m in _TOWA_RE.finditer(para_text):
                    add(m.group("term"),
                        _sentence_snippet(para_text, m.start(), m.end(), MAX_DEF_CHARS),
                        ident, "この法律", "definition_article", paragraph=para_num)

        # Inline （以下「X」という。） anywhere in the law, including inside
        # definition articles' own text.
        article_text = _clean(article)
        for m in _INLINE_DEF_RE.finditer(article_text):
            scope = m.group("scope")
            add(m.group("term"),
                _sentence_snippet(article_text, m.start(), m.end(), MAX_DEF_CHARS),
                ident,
                scope[:-4] if scope else "この法律",  # strip trailing 「において」
                "inline")

    return found


# --- references ---------------------------------------------------------------

_RANGE_RE = re.compile(
    rf"第(?P<a1>{_NUM})条(?:の(?P<b1>{_NUM}))?から第(?P<a2>{_NUM})条(?:の(?P<b2>{_NUM}))?まで")

_ARTICLE_RE = re.compile(
    rf"第(?P<a>{_NUM})条(?:の(?P<b>{_NUM}))?"
    rf"(?:第(?P<p>{_NUM})項)?"
    rf"(?:第(?P<i>{_NUM})号)?")

# A 項/号 reference with no 条 in front of it: same article.
_BARE_PARA_RE = re.compile(rf"第(?P<p>{_NUM})項(?:第(?P<i>{_NUM})号)?")

# The 項/号 tail that hangs off a relative 条 reference (前条第一項第二号).
_TAIL_RE = re.compile(rf"(?:第(?P<p>{_NUM})項)?(?:第(?P<i>{_NUM})号)?")

# 前条 / 次条 / 前二項 / 前各号 / 同条 …
_RELATIVE_RE = re.compile(r"(?P<dir>前|次|同)(?P<count>各|[二三四五六七八九十]+)?(?P<unit>条|項|号)")

# 民法（明治二十九年法律第八十九号）
_LAW_NUM_RE = re.compile(
    r"（(?P<num>(?:明治|大正|昭和|平成|令和)[元0-9０-９一二三四五六七八九十]{1,6}年"
    r"[^（）]{0,24}第[^（）]{0,12}号)）")
_LAW_NAME_RE = re.compile(r"([一-龥ぁ-んァ-ヶー々ａ-ｚA-Za-z0-9]{1,40}?(?:法|法律|令|規則|条例))$")

# 「第○条の規定は、…について準用する」. Anchored to a sentence boundary so the
# source provisions don't absorb the whole run-up to the clause.
_JUNYO_CLAUSE_RE = re.compile(
    r"(?:^|。)(?P<src>[^。]{0,150}?)の規定は[、]?(?P<tgt>[^。]{0,200}?)"
    r"(?:について|に対して|に)[、]?(?:これを)?準用する")


def _num_attr(main: str, branch: str | None) -> str | None:
    head = _norm_num(main)
    if head is None:
        return None
    if branch is None:
        return str(head)
    sub = _norm_num(branch)
    return f"{head}_{sub}" if sub is not None else str(head)


def _target(num_attr: str, paragraph: str | None = None, item: str | None = None) -> dict:
    out = {"article": num_attr, "article_display": _num_to_kanji_article(num_attr)}
    if paragraph:
        out["paragraph"] = _norm_num(paragraph)
    if item:
        out["item"] = _norm_num(item)
    return out


def _relative_offsets(direction: str, count: str | None) -> list[int] | None:
    """Offsets a relative reference points at (前二項 from 第3項 -> [-2, -1]).

    None means "not resolvable positionally" (同条/同項, 前各項).
    """
    if direction == "同":
        return None
    if count == "各":
        return None
    n = _norm_num(count) if count else 1
    if n is None or n < 1 or n > 20:
        return None
    step = -1 if direction == "前" else 1
    return [step * k for k in range(n, 0, -1)] if step < 0 else [k for k in range(1, n + 1)]


def _extract_references(text: str, ident: dict, para_num: int | None,
                        articles: list[ET.Element], index: int,
                        resolve_relative: bool) -> list[dict]:
    """All outbound references in one paragraph's text."""
    found: list[tuple[int, dict]] = []
    consumed: list[tuple[int, int]] = []

    def snippet(start: int, end: int) -> str:
        return _sentence_snippet(text, start, end, MAX_SNIPPET_CHARS)

    def overlaps(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in consumed)

    # External law references first: they swallow the article number that
    # follows the 法令番号, which would otherwise read as an internal reference.
    for m in _LAW_NUM_RE.finditer(text):
        name_match = _LAW_NAME_RE.search(text[:m.start()])
        if not name_match:
            continue
        start = name_match.start(1)
        tail = _ARTICLE_RE.match(text, m.end())
        ref = {
            "type": "external",
            "target": {"law": name_match.group(1), "law_num": m.group("num")},
            "resolved_from_relative": False,
        }
        end = m.end()
        if tail:
            num = _num_attr(tail.group("a"), tail.group("b"))
            if num:
                ref["target"].update(_target(num, tail.group("p"), tail.group("i")))
                end = tail.end()
        consumed.append((start, end))
        ref["source_text"] = text[start:end]
        ref["snippet"] = snippet(start, end)
        found.append((start, ref))

    # 第N条から第M条まで — a span, not two references.
    for m in _RANGE_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        lo = _num_attr(m.group("a1"), m.group("b1"))
        hi = _num_attr(m.group("a2"), m.group("b2"))
        if not lo or not hi:
            continue
        consumed.append((m.start(), m.end()))
        found.append((m.start(), {
            "type": "internal_range",
            "target": {"article_from": lo, "article_to": hi,
                       "article_from_display": _num_to_kanji_article(lo),
                       "article_to_display": _num_to_kanji_article(hi)},
            "resolved_from_relative": False,
            "source_text": m.group(0),
            "snippet": snippet(m.start(), m.end()),
        }))

    # Plain 第N条(第M項)(第K号). Lists (第N条及び第M条、第K条) fall out of this
    # naturally, one reference per member.
    last_article = None
    for m in _ARTICLE_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        num = _num_attr(m.group("a"), m.group("b"))
        if not num:
            continue
        consumed.append((m.start(), m.end()))
        last_article = num
        found.append((m.start(), {
            "type": "internal",
            "target": _target(num, m.group("p"), m.group("i")),
            "resolved_from_relative": False,
            "source_text": m.group(0),
            "snippet": snippet(m.start(), m.end()),
        }))

    # 前条 / 次条 / 前二項 / 同条 …, including any 第N項第K号 tail hanging off a
    # relative 条 (前条第一項 is one reference, not 前条 plus a stray 第一項).
    for m in _RELATIVE_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        unit = m.group("dir"), m.group("count"), m.group("unit")
        direction, count, unit = unit
        end = m.end()
        tail_p = tail_i = None
        if unit == "条":
            tail = _TAIL_RE.match(text, end)
            if tail and tail.end() > end:
                tail_p, tail_i = tail.group("p"), tail.group("i")
                end = tail.end()
        consumed.append((m.start(), end))

        ref = {
            "type": "relative",
            "relative_expression": text[m.start():end],
            "resolved_from_relative": False,
            "source_text": text[m.start():end],
            "snippet": snippet(m.start(), end),
        }
        offsets = _relative_offsets(direction, count)
        targets: list[dict] = []

        if not resolve_relative:
            pass
        elif direction == "同" and unit == "条" and last_article:
            targets = [_target(last_article, tail_p, tail_i)]
        elif offsets and unit == "条":
            for off in offsets:
                pos = index + off
                if 0 <= pos < len(articles):
                    num = articles[pos].get("Num") or ""
                    if num and ":" not in num:
                        targets.append(_target(num, tail_p, tail_i))
        elif offsets and unit == "項" and para_num:
            targets = [_target(ident["article"], str(para_num + off))
                       for off in offsets if para_num + off >= 1]

        if targets:
            ref["type"] = "relative_resolved"
            ref["resolved_from_relative"] = True
            if len(targets) == 1:
                ref["target"] = targets[0]
            else:
                ref["targets"] = targets
        else:
            ref["note"] = ("Relative reference left unresolved (同/各 forms and 号 "
                           "references need context this extractor does not track).")
        found.append((m.start(), ref))

    # 第N項 with no 条 in front of it: a paragraph of the article being read.
    for m in _BARE_PARA_RE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        consumed.append((m.start(), m.end()))
        found.append((m.start(), {
            "type": "internal",
            "target": _target(ident["article"], m.group("p"), m.group("i")),
            "resolved_from_relative": False,
            "same_article": True,
            "source_text": m.group(0),
            "snippet": snippet(m.start(), m.end()),
        }))

    refs = [ref for _, ref in sorted(found, key=lambda pair: pair[0])]
    for ref in refs:
        ref["source_paragraph"] = para_num
    return refs


def _junyo(texts: list[str]) -> list[dict]:
    """準用 (mutatis mutandis) clauses across an article's paragraph texts.

    ``読み替え`` is looked for across the whole paragraph, not just the clause
    sentence: the 読替え instruction is drafted as the following sentence
    (「この場合において、…と読み替えるものとする。」).
    """
    clauses = []
    for text in texts:
        matched = False
        for m in _JUNYO_CLAUSE_RE.finditer(text):
            matched = True
            clauses.append({
                "source_provisions": m.group("src").strip(),
                "applied_to": m.group("tgt").strip(),
                "has_yomikae": "読み替え" in text,
                "source_text": _sentence_snippet(text, m.start("src"), m.end(),
                                                 MAX_SNIPPET_CHARS * 2),
            })
        if not matched and "準用" in text:
            pos = text.find("準用")
            clauses.append({
                "source_provisions": None,
                "applied_to": None,
                "has_yomikae": "読み替え" in text,
                "source_text": _sentence_snippet(text, pos, pos + 2, MAX_SNIPPET_CHARS * 2),
                "note": ("準用 detected but the clause shape did not match; "
                         "read the snippet."),
            })
    return clauses


# --- reverse lookup -----------------------------------------------------------

def _citation_patterns(num_attr: str) -> tuple[re.Pattern, int | None]:
    """A regex matching citations of exactly this article, plus its main number.

    枝番 discipline matters: 第二十四条 must not match inside 第二十四条の二, and
    第二十四条の二 must not match inside 第二十四条の二の三. A negative lookahead
    for a further ``の`` + numeral enforces both.
    """
    parts = num_attr.split("_")
    head = int(parts[0]) if parts[0].isdigit() else None
    if head is None:
        return re.compile(re.escape(num_attr)), None
    forms = [f"第(?:{_int_to_kanji(head)}|{head})条"]
    for part in parts[1:]:
        if part.isdigit():
            forms.append(f"の(?:{_int_to_kanji(int(part))}|{part})")
    pattern = "".join(forms) + rf"(?!の{_NUM})"
    return re.compile(pattern), head


def _range_covers(text: str, target: int) -> re.Match | None:
    """A 第N条から第M条まで span in ``text`` that contains ``target``."""
    for m in _RANGE_RE.finditer(text):
        if m.group("b1") or m.group("b2"):
            continue
        lo, hi = _norm_num(m.group("a1")), _norm_num(m.group("a2"))
        if lo is not None and hi is not None and lo <= target <= hi:
            return m
    return None


# --- tools --------------------------------------------------------------------

def get_law_definitions(law_name_or_id: str, term: str = "") -> dict:
    """Find the terms a Japanese law defines (定義語) and where it defines them.

    Japanese statutes bind their own vocabulary: 「この法律において「労働者」とは
    …をいう」 in a （定義） article, a 各号 list where each 号 defines one term, or
    a parenthetical 「（以下「特定技能雇用契約」という。）」 buried in the article
    that first uses the term. Read an article without these and you will read it
    wrong — 「労働者」 does not mean the same thing in every law.

    Args:
        law_name_or_id: Law name, abbreviation, ``law_id`` or 法令番号
            (e.g. "労働契約法", "労基法", "322AC0000000049").
        term: Optional filter — return only definitions whose term contains this
            string. The whole law is scanned either way, so a filtered call and
            an unfiltered one see the same candidates.

    Returns each definition with the term, a snippet of the defining text, the
    article (number + caption) it sits in, and its ``scope``: "この法律" for a
    law-wide definition, or "この条"/"この章"/"この節" when the drafting limits it
    to that unit. ``definition_kind`` says which pattern matched
    (definition_article / enumerated_item / inline).
    """
    if not law_name_or_id.strip():
        raise ValueError("law_name_or_id is required")

    law_key, resolved_title, alias_source = _law_key(law_name_or_id, "")
    if not law_key:
        return _resolution_error(law_name_or_id)

    root, info, rev = _fetch_law_xml(law_key, "")
    definitions = _collect_definitions(root)
    total_found = len(definitions)

    if term.strip():
        definitions = [d for d in definitions if term.strip() in d["term"]]

    out = {
        "found": bool(definitions),
        "search_law_name": law_name_or_id,
        "resolved_law_title": rev.get("law_title") or resolved_title,
        "alias_expanded_from": alias_source,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num", law_key),
        "revision": _rev_meta(rev),
        "term_filter": term.strip() or None,
        "total_definitions_found": total_found,
        "returned": min(len(definitions), MAX_TERMS),
        "definitions": definitions[:MAX_TERMS],
        "extraction_note": _BEST_EFFORT,
    }
    if len(definitions) > MAX_TERMS:
        out["note"] = (f"{len(definitions)} definitions matched; showing the first "
                       f"{MAX_TERMS} in document order. Pass `term` to narrow.")
    elif not definitions and term.strip():
        out["hint"] = (f"No defined term contains '{term.strip()}'. The law defines "
                       f"{total_found} term(s) — call again without `term` to list "
                       "them, or the term may simply be used without definition.")
    return out


def get_article_references(law_name_or_id: str, article: str,
                           resolve_relative: bool = True) -> dict:
    """List every provision one article of a Japanese law points at.

    Extracts absolute internal references (第三十六条第一項第二号, ranges
    第三十二条から第三十二条の五まで, lists 第三十三条及び第三十六条), relative
    ones (前条・次条・前二項・同条), references to other laws by name plus 法令番号,
    and 準用 clauses ("第○条の規定は、…について準用する", with a flag when a
    読み替え table is attached).

    Args:
        law_name_or_id: Law name, abbreviation, ``law_id`` or 法令番号.
        article: Article number in any form ``find_law_article`` accepts —
            "37", "第三十七条", "第32条の2".
        resolve_relative: When True (default), 前条/次条/前項/前二項 are converted
            to absolute numbers using the article's position in the statute, and
            the reference is marked ``resolved_from_relative``. 同条/同項 resolve
            only when an absolute reference precedes them in the same paragraph.

    Returns one entry per reference with its ``type``
    (internal / internal_range / external / relative / relative_resolved), a
    structured ``target`` (article, paragraph, item; law + law_num when
    external) and the exact ``source_text`` it was matched from. External
    references carry the 法令番号 so they can be chained straight into
    ``find_law_article``.
    """
    if not law_name_or_id.strip():
        raise ValueError("law_name_or_id is required")
    if not article.strip():
        raise ValueError("article is required")

    law_key, resolved_title, alias_source = _law_key(law_name_or_id, "")
    if not law_key:
        return _resolution_error(law_name_or_id)

    num_attr, _, _ = _article_to_num(article)
    if not num_attr:
        return {"found": False, "error": f"Could not parse article '{article}'",
                "hint": "Use a form like '37', '第37条', '第32条の2' or '第三十七条'."}

    root, info, rev = _fetch_law_xml(law_key, "")
    articles = _main_articles(root)
    index = next((i for i, a in enumerate(articles) if a.get("Num") == num_attr), None)
    title = rev.get("law_title") or resolved_title

    if index is None:
        return {
            "found": False,
            "resolved_law_title": title,
            "law_num": info.get("law_num", law_key),
            "search_article": article,
            "article_num": num_attr,
            "error": f"Article {num_attr} not found in the 本則 of {title}",
            "hint": ("This tool reads 本則 articles only. Check the number with "
                     "get_law_content(law_id), or fetch 附則 text with "
                     "find_law_article(provision='suppl')."),
        }

    target_article = articles[index]
    ident = _article_ident(target_article)
    references: list[dict] = []
    paragraphs = _paragraphs(target_article)
    texts: list[str] = []
    if paragraphs:
        for para_num, para in paragraphs:
            text = _clean(para)
            texts.append(text)
            references.extend(_extract_references(
                text, ident, para_num, articles, index, resolve_relative))
    else:
        texts.append(_clean(target_article))
        references.extend(_extract_references(
            texts[0], ident, None, articles, index, resolve_relative))

    junyo = _junyo(texts)

    return {
        "found": True,
        "search_law_name": law_name_or_id,
        "resolved_law_title": title,
        "alias_expanded_from": alias_source,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num", law_key),
        "revision": _rev_meta(rev),
        "article": ident,
        "resolve_relative": resolve_relative,
        "reference_count": len(references),
        "references": references,
        "is_junyo_provision": bool(junyo),
        "junyo_clauses": junyo,
        "has_yomikae": any(c["has_yomikae"] for c in junyo),
        "extraction_note": _BEST_EFFORT,
    }


def find_where_cited(law_name_or_id: str, article: str, max_results: int = 25) -> dict:
    """Find which other articles of the SAME law cite a given article.

    The reverse of ``get_article_references``: scan every 本則 article for
    citations of the target and report the citing articles with the sentence
    each citation sits in. Useful for "what turns on 第三十六条?" — the answer is
    usually spread across articles that never appear in the target's own text.

    枝番 are matched exactly: a search for 第二十四条 will not report 第二十四条の二.
    Kanji and ASCII numbering are both matched, and a
    ``第二十条から第三十条まで`` span that covers the target is reported as a range
    citation.

    Cross-law reverse lookup is out of scope — nothing here reads other statutes.
    For that, use ``search_laws_by_keyword`` with the citation as written in
    statutory text, e.g. ``search_laws_by_keyword("労働基準法第三十六条")``. Note
    the kanji numerals: "第36条" will not match body text.

    Args:
        law_name_or_id: Law name, abbreviation, ``law_id`` or 法令番号.
        article: The cited article — "36", "第三十六条", "第32条の2".
        max_results: Maximum citing articles to return (default 25).

    Returns the citing articles in document order, each with its number,
    caption, the matched citation text and the surrounding sentence.
    """
    if not law_name_or_id.strip():
        raise ValueError("law_name_or_id is required")
    if not article.strip():
        raise ValueError("article is required")

    law_key, resolved_title, alias_source = _law_key(law_name_or_id, "")
    if not law_key:
        return _resolution_error(law_name_or_id)

    num_attr, _, _ = _article_to_num(article)
    if not num_attr:
        return {"found": False, "error": f"Could not parse article '{article}'",
                "hint": "Use a form like '36', '第36条', '第32条の2' or '第三十六条'."}

    root, info, rev = _fetch_law_xml(law_key, "")
    pattern, head = _citation_patterns(num_attr)
    has_branch = "_" in num_attr

    citations: list[dict] = []
    for citing in _main_articles(root):
        if citing.get("Num") == num_attr:
            continue  # self-references are noise
        text = _clean(citing)
        matches = list(pattern.finditer(text))
        kind = "direct"
        if not matches and head is not None and not has_branch:
            span = _range_covers(text, head)
            if span:
                matches, kind = [span], "range"
        if not matches:
            continue
        entry = dict(_article_ident(citing))
        entry["match_kind"] = kind
        entry["citation_count"] = len(matches)
        entry["citations"] = [
            {"source_text": m.group(0),
             "snippet": _sentence_snippet(text, m.start(), m.end(), MAX_SNIPPET_CHARS)}
            for m in matches[:5]
        ]
        citations.append(entry)

    return {
        "found": bool(citations),
        "search_law_name": law_name_or_id,
        "resolved_law_title": rev.get("law_title") or resolved_title,
        "alias_expanded_from": alias_source,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num", law_key),
        "revision": _rev_meta(rev),
        "cited_article": {"article": num_attr,
                          "article_display": _num_to_kanji_article(num_attr)},
        "citing_article_count": len(citations),
        "returned": min(len(citations), max_results),
        "citing_articles": citations[:max_results],
        "scope_note": ("Same-law reverse lookup only. For citations from other "
                       "laws use search_laws_by_keyword with kanji numbering, "
                       "e.g. '第三十六条'."),
        "extraction_note": _BEST_EFFORT,
    }


def register(mcp) -> None:
    """Attach the 定義語 / cross-reference tools to a FastMCP server.

    Registration is explicit so importing this module has no side effects and
    the tools can be exercised as plain functions in tests.
    """
    mcp.tool(annotations={"title": "Defined terms in a law (定義語)",
                          "readOnlyHint": True,
                          "openWorldHint": True})(get_law_definitions)
    mcp.tool(annotations={"title": "Outbound references of an article (参照・準用)",
                          "readOnlyHint": True,
                          "openWorldHint": True})(get_article_references)
    mcp.tool(annotations={"title": "Where an article is cited (same law)",
                          "readOnlyHint": True,
                          "openWorldHint": True})(find_where_cited)
