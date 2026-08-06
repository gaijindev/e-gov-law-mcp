"""新旧対照 — diff two versions of a Japanese law, article by article.

e-Gov publishes every enforced version of a statute, but only as whole
documents: there is no "what changed" endpoint. This module fetches two
versions (by ``law_revision_id``, by ``asof`` date, or "current"/"previous")
and diffs their 本則 (MainProvision) at ARTICLE granularity, which is the unit
Japanese amendments are actually written in (改正法 says "第三十六条中「…」を
「…」に改める").

Article granularity is deliberate: a character-level diff of a 100k-character
statute is unreadable and blows the response budget, while a whole-document
diff hides which provision moved. Comparing cleaned article text keyed by the
XML ``Num`` attribute gives added / deleted / modified / unchanged directly,
and modified articles get a sentence-level unified diff (Japanese sentences
end in 。, so that is the natural "line").

Nothing is registered at import time — call ``register(mcp)`` from the server.
"""

import difflib
import re
import xml.etree.ElementTree as ET
from datetime import date

from cache import TTLCache, ttl_cached
from server import (
    _CACHE_TTL,
    _article_to_num,
    _clean,
    _fetch_law_xml,
    _get,
    _law_key,  # wraps _resolve_law: name/alias/law_id/法令番号 -> /law_data path key
    _resolution_error,
)

# A law_revision_id is "{law_id}_{YYYYMMDD}_{amending_law_id}".
_REVISION_ID_RE = re.compile(r"^[0-9A-Za-z]+_\d{8}_[0-9A-Za-z]+$")

# Per-article diff cap, and a hard cap on the whole response. Both exist so a
# broad comparison (e.g. 労基法 2015 vs today) degrades into a truncated-but-
# usable answer with a note steering the next call, instead of a 200k-token dump.
MAX_DIFF_CHARS = 2_000
MAX_TOTAL_CHARS = 20_000

# /law_revisions is only needed to resolve old="previous" and to label versions,
# so it gets its own small cache rather than a slot in the law-XML cache.
REVISIONS_CACHE = TTLCache(maxsize=64, ttl=_CACHE_TTL)


@ttl_cached(REVISIONS_CACHE)
def _fetch_revisions(law_key: str) -> list[dict]:
    """Revision history for a law, newest first. Empty list when unavailable."""
    data = _get(f"/law_revisions/{law_key}")
    return list((data or {}).get("revisions", []))


def _previous_revision_id(law_key: str) -> str:
    """The ``law_revision_id`` of the version enforced before the current one.

    Revisions come back newest first and may be led by 未施行 (UnEnforced)
    entries, so "previous" is the entry after the CurrentEnforced one — not
    simply the second in the list.
    """
    revisions = _fetch_revisions(law_key)
    if not revisions:
        raise ValueError(
            f"No revision history available for '{law_key}', so old='previous' "
            "cannot be resolved. Pass an explicit law_revision_id or a date."
        )
    idx = next((i for i, r in enumerate(revisions)
                if r.get("current_revision_status") == "CurrentEnforced"), None)
    if idx is None:
        idx = 0
    if idx + 1 >= len(revisions):
        raise ValueError(
            "This law has only one enforced version, so there is no previous "
            "revision to compare against."
        )
    rev_id = revisions[idx + 1].get("law_revision_id")
    if not rev_id:
        raise ValueError("The previous revision has no law_revision_id.")
    return rev_id


def _version_key(law_key: str, spec: str, label: str) -> tuple[str, str, str]:
    """Resolve a version spec into (fetch_key, asof, how_it_was_interpreted).

    Accepts a ``law_revision_id`` (exact version), an ISO date (the law as it
    stood then), "current", or "previous".
    """
    value = (spec or "").strip()
    if not value or value.lower() == "current":
        return law_key, "", "current"
    if value.lower() == "previous":
        return _previous_revision_id(law_key), "", "previous"
    if _REVISION_ID_RE.match(value):
        return value, "", "law_revision_id"
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{label}='{spec}' is not a version. Pass a law_revision_id "
            "(e.g. '322AC0000000049_20200401_501AC0000000071' from "
            "get_law_revisions), a date in YYYY-MM-DD form, 'current', or "
            "'previous'."
        ) from None
    return law_key, value, "asof"


def _version_meta(spec: str, how: str, fetch_key: str, asof: str,
                  info: dict, rev: dict) -> dict:
    """Identity of one side of the comparison, as the agent asked for it."""
    return {
        "requested": spec,
        "interpreted_as": how,
        "fetch_key": fetch_key,
        "asof": asof or None,
        "law_id": info.get("law_id"),
        "law_num": info.get("law_num"),
        "law_title": rev.get("law_title"),
        "law_revision_id": rev.get("law_revision_id"),
        "amendment_law_title": rev.get("amendment_law_title"),
        "amendment_law_num": rev.get("amendment_law_num"),
        "amendment_promulgate_date": rev.get("amendment_promulgate_date"),
        "amendment_enforcement_date": rev.get("amendment_enforcement_date"),
        "current_revision_status": rev.get("current_revision_status"),
    }


def _articles(root: ET.Element) -> dict[str, ET.Element]:
    """Map ``Num`` -> Article element for the 本則 only.

    附則 articles reuse main-body numbers, so scoping to MainProvision is what
    keeps 第三十二条 from colliding with 附則第三十二条.
    """
    main = root.find(".//MainProvision")
    if main is None:
        return {}
    out: dict[str, ET.Element] = {}
    for el in main.iter("Article"):
        num = el.get("Num")
        if num and num not in out:
            out[num] = el
    return out


def _norm(el: ET.Element) -> str:
    """Comparable text of an article: visible text with all whitespace removed."""
    return re.sub(r"\s+", "", _clean(el))


def _num_sort_key(num: str) -> tuple:
    """Sort 'Num' attributes numerically: 2 < 24_2 < 29:31 < 100."""
    parts = [int(p) if p.isdigit() else 0 for p in re.split(r"[_:]", num)]
    return tuple((parts + [0, 0, 0])[:3])


def _sentences(text: str) -> list[str]:
    """Split Japanese text into sentence 'lines' for a readable unified diff."""
    parts = [p.strip() for p in re.split(r"(?<=。)", text)]
    return [p for p in parts if p] or [text]


def _unified_diff(old_text: str, new_text: str, num: str) -> str:
    """Sentence-granular unified diff of one article, capped at MAX_DIFF_CHARS."""
    diff = "\n".join(difflib.unified_diff(
        _sentences(old_text), _sentences(new_text),
        fromfile=f"old 第{num}条", tofile=f"new 第{num}条", lineterm="", n=1,
    ))
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + (
            f"\n… [diff truncated at {MAX_DIFF_CHARS} chars; call again with "
            f"article='{num}' for the full old and new text]"
        )
    return diff


def _article_label(el: ET.Element | None) -> dict:
    if el is None:
        return {"article_title": None, "article_caption": None}
    return {
        "article_title": el.findtext("ArticleTitle"),
        "article_caption": el.findtext("ArticleCaption"),
    }


def _classify(old_arts: dict, new_arts: dict) -> dict[str, list[str]]:
    """Bucket every article number appearing in either version."""
    buckets = {"added": [], "deleted": [], "modified": [], "unchanged": []}
    for num in sorted(set(old_arts) | set(new_arts), key=_num_sort_key):
        in_old, in_new = num in old_arts, num in new_arts
        if in_old and not in_new:
            buckets["deleted"].append(num)
        elif in_new and not in_old:
            buckets["added"].append(num)
        elif _norm(old_arts[num]) == _norm(new_arts[num]):
            buckets["unchanged"].append(num)
        else:
            buckets["modified"].append(num)
    return buckets


def _single_article(num_attr: str, article: str, old_arts: dict, new_arts: dict,
                    out: dict) -> dict:
    """Full old/new text plus a diff for exactly one article."""
    old_el, new_el = old_arts.get(num_attr), new_arts.get(num_attr)
    if old_el is None and new_el is None:
        out["found"] = False
        out["error"] = (f"Article '{article}' (Num={num_attr}) is not in the 本則 of "
                        "either version.")
        out["hint"] = ("Check the number with get_law_content(law_id); 附則 "
                       "(supplementary provisions) are not diffed by this tool.")
        return out

    old_text = _clean(old_el) if old_el is not None else ""
    new_text = _clean(new_el) if new_el is not None else ""
    if old_el is None:
        status = "added"
    elif new_el is None:
        status = "deleted"
    elif _norm(old_el) == _norm(new_el):
        status = "unchanged"
    else:
        status = "modified"

    out["article"] = {
        "num": num_attr,
        "status": status,
        "old": {**_article_label(old_el), "text": old_text or None},
        "new": {**_article_label(new_el), "text": new_text or None},
        "diff": _unified_diff(old_text, new_text, num_attr) if status == "modified" else None,
    }
    if status == "unchanged":
        out["note"] = (f"第{num_attr}条 is identical in both versions "
                       "(whitespace-normalised comparison).")
    return out


def compare_law_revisions(law_name_or_id: str, old: str, new: str = "current",
                          scope: str = "changed", article: str = "",
                          max_articles: int = 20, offset: int = 0) -> dict:
    """Diff two versions of a Japanese law (新旧対照), article by article.

    Answers "what did the 2019 reform actually change?" and "what exactly
    changed in 第36条?". Both versions' 本則 are fetched from e-Gov and their
    articles compared by number, so each provision comes back as added, deleted,
    modified or unchanged. 附則 (supplementary provisions) are out of scope.

    Args:
        law_name_or_id: Law name, abbreviation, ``law_id`` or 法令番号
            (e.g. "労働基準法", "労基法", "322AC0000000049").
        old: The earlier version. Accepts a ``law_revision_id`` from
            ``get_law_revisions``, a date in YYYY-MM-DD form (the law as it stood
            then — e-Gov only serves dates from 2017-04-01 onwards, so use a
            ``law_revision_id`` for anything older), "previous" (the version
            enforced immediately before the current one), or "current".
        new: The later version, same forms as ``old``. Defaults to "current".
        scope: "changed" (default) lists only added/deleted/modified articles;
            "all" additionally returns the numbers of unchanged articles.
        article: Restrict the comparison to one article, e.g. "36", "第36条" or
            "24条の2". Returns that article's FULL old text, new text and diff —
            use it to drill into anything the summary flags.
        max_articles: Maximum changed articles detailed in one call (default 20).
        offset: Number of changed articles to skip, for paging (default 0).

    Returns both versions' identifying metadata (``law_revision_id``, enforcement
    date, amending law title), summary counts, and per changed article its
    number, title/caption and — for modified articles — a sentence-level unified
    diff of the old against the new text.
    """
    if not law_name_or_id.strip():
        raise ValueError("law_name_or_id is required")
    if not (old or "").strip():
        raise ValueError("old is required: a law_revision_id, a YYYY-MM-DD date, "
                         "'previous', or 'current'.")
    if scope not in ("changed", "all"):
        raise ValueError("scope must be 'changed' or 'all'")

    law_key, resolved_title, alias_source = _law_key(law_name_or_id, "")
    if not law_key:
        return _resolution_error(law_name_or_id)

    old_key, old_asof, old_how = _version_key(law_key, old, "old")
    new_key, new_asof, new_how = _version_key(law_key, new, "new")

    try:
        old_root, old_info, old_rev = _fetch_law_xml(old_key, old_asof)
        new_root, new_info, new_rev = _fetch_law_xml(new_key, new_asof)
    except RuntimeError as exc:
        return {
            "found": False,
            "error": str(exc),
            "hint": ("e-Gov only serves point-in-time versions from 2017-04-01 "
                     "onwards (error 400044), and a date before the law's "
                     "promulgation or an unknown law_revision_id has no version "
                     "to return. For anything older than 2017-04-01, get a "
                     "law_revision_id from get_law_revisions(law_name_or_id) and "
                     "pass that instead of a date."),
        }

    old_arts, new_arts = _articles(old_root), _articles(new_root)
    out: dict = {
        "found": True,
        "search_law_name": law_name_or_id,
        "resolved_law_title": new_rev.get("law_title") or resolved_title,
        "alias_expanded_from": alias_source,
        "law_id": new_info.get("law_id"),
        "law_num": new_info.get("law_num"),
        "old_version": _version_meta(old, old_how, old_key, old_asof, old_info, old_rev),
        "new_version": _version_meta(new, new_how, new_key, new_asof, new_info, new_rev),
    }

    if article.strip():
        num_attr, _, _ = _article_to_num(article)
        if not num_attr:
            return {**out, "found": False,
                    "error": f"Could not parse article '{article}'",
                    "hint": "Use a form like '36', '第36条' or '第24条の2'."}
        return _single_article(num_attr, article, old_arts, new_arts, out)

    buckets = _classify(old_arts, new_arts)
    changed = [(n, "added") for n in buckets["added"]] + \
              [(n, "deleted") for n in buckets["deleted"]] + \
              [(n, "modified") for n in buckets["modified"]]
    changed.sort(key=lambda pair: _num_sort_key(pair[0]))

    out["summary"] = {
        "old_article_count": len(old_arts),
        "new_article_count": len(new_arts),
        "added": len(buckets["added"]),
        "deleted": len(buckets["deleted"]),
        "modified": len(buckets["modified"]),
        "unchanged": len(buckets["unchanged"]),
        "changed_total": len(changed),
    }

    if not changed:
        out["identical"] = True
        out["message"] = (
            "The two versions have identical 本則 text. "
            f"old={out['old_version']['law_revision_id']} "
            f"new={out['new_version']['law_revision_id']}. "
            "If those revision ids are the same, both specs resolved to one "
            "version — check get_law_revisions for the id you meant."
        )
        out["changed_articles"] = []
        if scope == "all":
            out["unchanged_article_nums"] = buckets["unchanged"]
        return out

    out["identical"] = False
    page = changed[offset:offset + max_articles]
    entries: list[dict] = []
    used = 0
    budget_hit = False
    for num, status in page:
        old_el, new_el = old_arts.get(num), new_arts.get(num)
        entry = {"num": num, "status": status,
                 **_article_label(new_el if new_el is not None else old_el)}
        if status == "modified":
            entry["diff"] = _unified_diff(_clean(old_el), _clean(new_el), num)
        elif status == "added":
            entry["new_text"] = _clean(new_el)[:MAX_DIFF_CHARS]
        else:
            entry["old_text"] = _clean(old_el)[:MAX_DIFF_CHARS]
        size = sum(len(str(v)) for v in entry.values())
        if used + size > MAX_TOTAL_CHARS and entries:
            budget_hit = True
            break
        entries.append(entry)
        used += size

    out["changed_articles"] = entries
    out["returned"] = len(entries)
    out["offset"] = offset
    shown = offset + len(entries)
    out["truncated"] = shown < len(changed)
    if out["truncated"]:
        reason = ("the ~%d-character response budget" % MAX_TOTAL_CHARS
                  if budget_hit else "max_articles")
        out["next_offset"] = shown
        out["note"] = (
            f"{len(changed)} articles changed; {len(entries)} shown (capped by "
            f"{reason}). Call again with offset={shown} for the next page, or "
            "pass article='<number>' to get one article's full old text, new "
            "text and diff."
        )
    if scope == "all":
        out["unchanged_article_nums"] = buckets["unchanged"]
    return out


def register(mcp) -> None:
    """Register the 新旧対照 tool on a FastMCP server."""
    mcp.tool(annotations={"title": "Compare two versions of a law (新旧対照)",
                          "readOnlyHint": True,
                          "openWorldHint": True})(compare_law_revisions)


def get_cache_stats() -> dict:
    """Hit/miss counts for this module's revision-history cache."""
    return {"law_revisions": REVISIONS_CACHE.stats()}


def clear_cache() -> int:
    """Clear this module's revision-history cache."""
    return REVISIONS_CACHE.clear()
