# Improvement research — e-Gov Law MCP (2026-08-06)

Deep-research synthesis from four streams: a code audit verified against the live
e-Gov API, a full inventory of 法令API v2 capabilities we don't use, current MCP
server best practices (spec 2025-11-25, Anthropic tool-design guidance, FastMCP 2.x),
and a competitive scan of every Japanese-law MCP server found in the wild.

## TL;DR — top 5 moves

1. **Use `elm` partial fetch on `/law_data`** — the API can return exactly one
   article/附則/別表 server-side. Replaces our download-whole-XML-and-parse approach,
   fixes the 附則/別表 gaps, and `/keyword` hit `position`s are `elm` paths you can
   feed straight back in (search → fetch pipeline).
2. **Point-in-time & amendment awareness** (`asof`, `/law_revisions`,
   `current_revision_status=UnEnforced`) — the #1 documented user pain point in this
   space; almost no competitor has it; the API supports it natively.
3. **English translation integration** (Japanese Law Translation per-law XML +
   official bilingual legal dictionary) — no competitor offers English at all, and it
   directly serves the bundled jp-foreigner-law-qa skill's audience.
4. **MCP hygiene**: tool annotations + titles, structured output via typed returns,
   `search`/`fetch` tools for ChatGPT Deep Research, actionable errors,
   concise/detailed response modes.
5. **Ship it properly**: pyproject → PyPI → `uvx e-gov-law-mcp`, official MCP
   registry, MCPB one-click bundle, authless hosted endpoint (public data), Anthropic
   connector directory + LobeHub/PulseMCP/Glama listings.

## 1. Confirmed bugs / gaps in the current code (verified live)

| # | Finding | Evidence / impact |
|---|---|---|
| 1 | **附則 (SupplProvision) articles unreachable.** `root.find(".//Article[@Num=…]")` matches main-body articles only by document-order luck; 労働基準法 has 255 `<Article>` elements of which **131 are in 附則 with duplicate Num values** (1, 2, 3…). No way to request a 附則 article; correctness of main-body lookup is accidental. | Live XML inspection of 322AC0000000049. Fix: scope query to `MainProvision`, add explicit 附則 addressing (or switch to `elm`). |
| 2 | **別表 (appended tables) not fetchable.** The bundled skill itself points users to 入管法 別表第一・第二 for the visa-status list — the server can't retrieve it except inside a >50k-char full-text dump. | `AppdxTable` elements exist (労基法 has 3). `elm=AppdxTable[1]` solves it. |
| 3 | **TOC is lossy**: only `Chapter/ChapterTitle`. Misses 編 (Part), 節 (Section), 款, article ranges, 附則, 別表. For 民法 (which is organized by 編) the TOC loses its top level. | Competitors treat TOC-first navigation as table stakes (`format=toc`). |
| 4 | **Rich revision metadata dropped.** `/laws` and `/law_data` already return `abbrev` (official 略称), `current_revision_status`, `amendment_enforcement_date`, `amendment_scheduled_enforcement_date`, `law_revision_id` — we discard all of it, so callers can't tell "this law has a pending 未施行 amendment". | Verified in live responses. |
| 5 | **Hand-maintained alias map partly redundant.** The API returns official `abbrev` values, and `/laws?law_title=` already matches common abbreviations (入管法, 個人情報保護法 both resolve). Keep a small alias layer for colloquialisms; some current aliases are dubious (税法→所得税法, 民事→民法, 知財法→著作権法) and can silently fetch the wrong statute. | Verified live. |
| 6 | **Deleted/merged articles unhandled.** Merged deletions appear as `<Article Num="3:5">`; `Delete` attributes exist. Our exact-match `Num` lookup returns "not found" with no hint. | From the official XML schema docs. |
| 7 | **号 (Item) addressing missing.** `_article_to_num` parses 条/項 but not 号 ("第24条第1項第4号"), which immigration questions hit constantly. | Parser gap. |
| 8 | **Token blowup defaults.** `search_laws_by_keyword` defaults to `limit=100` sentences; no `sentence_text_size`/`sentences_limit` control; `get_law_content` full-text path returns up to 50k chars inline. | Anthropic guidance: cap + paginate + steer via truncation notes. |
| 9 | **Cache tools pollute the agent toolset.** `get_cache_stats`/`clear_cache` are operator tools, not agent tools; every extra tool degrades selection. | Hide via FastMCP tags/disable in hosted mode. |
| 10 | **`data/` persistence assumes single-user local disk.** On the hosted HTTP deployment the dir is per-container, ephemeral, and shared across all callers. | Fine locally; document or disable in HTTP mode. |
| 11 | **No retry/backoff, no User-Agent** on `requests`; single 60s timeout. | Politeness + resilience. |
| 12 | **Zero tests, no pyproject, flat modules.** | Blocks packaging, registry, and safe refactoring. |

## 2. Unused API v2 capabilities (full inventory)

Authoritative spec: `https://laws.e-gov.go.jp/api/2/swagger-ui/lawapi-v2.yaml` (v2.1.139).
Six endpoints exist; we use three, each partially.

- **`elm` param on `/law_data`** — fetch only part of a law:
  `MainProvision-Article_24_2`, `SupplProvision[1]`, `AppdxTable[1]`, `TOC[1]`,
  `Preamble[1]`, `Part_1-Chapter_2`, item-level paths, etc. Branch articles use `_`
  (第24条の2 → `24_2`). This single feature fixes findings #1/#2/#7 and kills most of
  our XML plumbing.
- **`/law_revisions/{id}`** — full amendment history with filters incl.
  `current_revision_status` (`CurrentEnforced` / `UnEnforced` / `PreviousEnforced` /
  `Repeal`), amendment date ranges, `amendment_type`. Enables "list pending
  amendments to law X".
- **`asof=YYYY-MM-DD`** on `/laws`, `/keyword`, `/law_data`, `/law_file` — law as it
  stood on a date; exact versions via `law_revision_id` path keys.
- **Native JSON body**: `law_full_text_format=json&json_format=light` — no base64, no
  XML parsing (marked 試行版/trial; keep the XML path as fallback).
- **`omit_amendment_suppl_provision=true`** — strips amending-law 附則 noise.
- **`/keyword` extras**: `sentences_limit` (per-law cap), `sentence_text_size`
  (snippet length), `highlight_tag`, category/era/date filters, `next_offset`
  pagination, and — crucially — each hit's `position` is an `elm` path.
- **`/laws` extras**: `order` sort, `category_cd` (50 subject codes), promulgation
  date ranges, `amendment_law_id` ("all laws amended by X"), partial `law_id` match.
- **`/attachment/{law_revision_id}`** — figures/images referenced by `<Fig src>`;
  **`/law_file/{xml|json|html|rtf|docx}/{id}`** — whole-law file export.
- **Bulk download** (`/bulkdownload`) for mass ingestion — the sanctioned path if we
  ever build local indexes (定義語/準用 analysis, offline search).
- Ops notes: no key, no numeric rate limit ("avoid bursts"), Government Standard
  Terms (CC-BY-4.0 compatible), watch https://laws.e-gov.go.jp/news/ for spec
  changes; JSON body output is officially unstable.

## 3. Competitive landscape (what others do; where we can win)

Closest competitors: ryoooo/e-gov-law-mcp (batch article fetch, deep alias/pattern
parsing), shuji-bonji/houki-egov-mcp (best-designed: `format=toc`, `at` date param,
MCP-family contracts), michimani/jpn-laws-mcp-server (revisions), kentaroajisaka's
labor-law/tax-law MCPs (bundle 通達 + tax case decisions — proves demand for
beyond-statute sources). Best-in-class patterns elsewhere: CourtListener (citation as
addressable key), EUR-Lex MCP (canonical citation resolution + amends/cites graph
traversal).

Ranked differentiators nobody or almost nobody has:

1. **Point-in-time + 未施行 revisions** (only 2 competitors, both shallow).
2. **English translations** (zero competitors; JLT has per-law English XML at stable
   URLs + the official bilingual legal-term dictionary; no API — scrape & cache).
3. **TOC-first granular retrieval** (match the best competitors).
4. **Canonical citation resolution** — accept 略称/法令番号/law_id/"第9条第2項第1号"
   and return a structured, citable object (EUR-Lex/CourtListener pattern).
5. **準用・読み替え/definition (定義語) resolution** — unsolved by anyone;
   yamachig/Lawtext's analyzer proves feasibility. Highest differentiation.
6. **新旧対照 (amendment diff)** — diff two `law_revision_id`s server-side.
7. **告示・通達** (e.g. ISA 入管 guidelines for our domain) — scrape-based, proven
   demand via the vertical servers.
8. 判例 (CC0 scraped dataset exists; heavy), 条例 (no API anywhere; lowest priority).

## 4. MCP best practices to adopt (spec 2025-11-25 / FastMCP 2.x)

- **Tool annotations + titles** on every tool (`readOnlyHint=True` everywhere except
  `clear_cache`). Trivial; required for Anthropic connector-directory listing.
- **Structured output**: annotate return types (TypedDict/dataclass) → FastMCP emits
  output schemas + `structuredContent` automatically.
- **Tool-design guidance** (Anthropic "writing effective tools for agents"):
  sharply differentiate the two search tools in their descriptions (title/number
  lookup vs full-text); add `response_format: concise|detailed`; cap responses and
  make truncation notes steer the next call; error messages that tell the agent what
  to try next (e.g. suggest the formal title on resolution failure).
- **ChatGPT compatibility**: add thin `search(query)` / `fetch(id)` tools following
  OpenAI's Deep Research convention (~50 lines, delegating to existing tools).
- **Testing**: pytest with FastMCP in-memory `Client(mcp)` + respx-mocked e-Gov HTTP;
  a lists-tools test asserting annotations/titles; MCP Inspector smoke test; a small
  10-question eval set drawn from the skill's domain.
- **Optional**: `law://{law_id}` resource template, a citation prompt, `ctx`
  progress logging on slow fetches. Skip elicitation/completions (weak client
  support).

## 5. Distribution & deployment

- **Packaging**: move to a package dir + `pyproject.toml` with a console script →
  PyPI → `uvx e-gov-law-mcp`; include the `mcp-name:` marker in the PyPI README.
- **Official MCP registry**: publish `server.json` (`mcp-publisher`, GitHub-OIDC
  namespace `io.github.<user>/e-gov-law-mcp`); one record can carry PyPI + OCI +
  remote URL.
- **MCPB bundle** (`npx @anthropic-ai/mcpb`) for one-click Claude Desktop install
  (bundle deps into `server/lib`).
- **Hosted endpoint**: data is public — go **authless** (rate-limited) instead of the
  static bearer token; that unlocks ChatGPT connectors and the Anthropic directory
  (which also needs: titles/annotations, privacy policy page, docs page, ≥3 example
  prompts). Keep OAuth (FastMCP `RemoteAuthProvider`) as a later option; don't
  hand-roll.
- Get listed on LobeHub / PulseMCP / Glama for discovery (competitors are).

## 6. Suggested phasing

- **Phase 1 — correctness & hygiene (small):** `elm`-based article fetch (fixes
  附則/別表/号/deleted-article gaps), TOC upgrade, surface revision metadata +
  `abbrev`, annotations/titles/structured output, actionable errors, response caps,
  hide cache tools, retry/UA, pytest suite.
- **Phase 2 — differentiation (medium):** `asof` + `list_law_revisions` +
  未施行 support; `search`/`fetch` ChatGPT tools; canonical citation resolution;
  keyword-search snippet/pagination controls.
- **Phase 3 — distribution:** pyproject/PyPI/uvx, MCP registry, MCPB, authless
  hosted endpoint, directory submissions.
- **Phase 4 — moats (large):** English translation layer (JLT), 新旧対照 diff,
  定義語/準用 tools (Lawtext-style analysis over bulk-downloaded XML), then
  告示・通達 for the immigration/labor vertical.

## Key sources

- API spec: https://laws.e-gov.go.jp/api/2/swagger-ui/lawapi-v2.yaml · docs: https://laws.e-gov.go.jp/docs/ · news: https://laws.e-gov.go.jp/news/
- Anthropic tool design: https://www.anthropic.com/engineering/writing-tools-for-agents
- MCP spec changelog: https://modelcontextprotocol.io/specification/2025-11-25/changelog
- FastMCP: https://gofastmcp.com/servers/tools · /servers/testing · /integrations/chatgpt
- Claude connectors: https://support.claude.com/en/articles/11503834 · directory: https://claude.com/docs/connectors/building/submission
- Registry publishing: https://modelcontextprotocol.info/tools/registry/publishing/
- Competitor design note: https://qiita.com/shuji-bonji/items/f242ab90685f687a3609 · citation-graph pattern: https://github.com/cyanheads/eur-lex-mcp-server
- JLT (English): https://www.japaneselawtranslation.go.jp/ · Lawtext: https://github.com/yamachig/Lawtext
