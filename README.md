<!-- mcp-name: io.github.gaijindev/e-gov-law-mcp -->

# e-Gov Law MCP

An [MCP](https://modelcontextprotocol.io) server that wraps Japan's **e-Gov 法令API v2**
(<https://laws.e-gov.go.jp/api/2>) so an LLM can search Japanese laws, run full-text
keyword searches, and fetch the exact text of a single article — including
supplementary provisions (附則), schedules/tables (別表) and items (号) — as of
today or as of any past or future date.

The API is free and needs no key. Common laws (六法 + key modern legislation) and their
everyday abbreviations are mapped directly, so "民法" or "労基法" resolve without a search
round-trip. Every element (an article, a 別表, a whole 附則 block) is fetched with the
API's `elm` parameter — an element path such as `MainProvision-Article_24_2` or
`AppdxTable[1]` — so the server asks for the one piece of text it needs instead of
downloading and scanning a whole statute. That also makes 附則 and 別表 addressable,
which a plain `Article[@Num=]` search can't do: 附則 articles reuse main-body numbers.

## Contents

- [Why an MCP instead of a chatbot?](#why-an-mcp-instead-of-a-chatbot)
- [Tools](#tools)
- [Example use cases](#example-use-cases)
- [Installation](#installation)
- [Use it with Claude or OpenAI](#use-it-with-claude-or-openai)
- [ChatGPT / Deep Research](#chatgpt--deep-research)
- [Hosted / HTTP deployment](#hosted--http-deployment)
- [Bundled skill](#bundled-skill)
- [Testing](#testing)
- [Notes and limitations](#notes-and-limitations)
- [Privacy policy](#privacy-policy)
- [Support](#support)
- [License](#license)

## Why an MCP instead of a chatbot?

A chatbot is the reasoning and language layer; an MCP is the tools and data layer
that grounds it. Asked from memory, a model can invent article numbers, cite
repealed law, or just be stale at its training cutoff, and you have no way to check.
With this MCP the assistant fetches the actual current text from the official e-Gov
database and cites the `law_id` and article, so the answer is verifiable instead of
"trust me."

| | Plain chatbot (from memory) | + this MCP |
| --- | --- | --- |
| Source | Statistical recall of text it saw | Official e-Gov database (≈9,500 laws) |
| Accuracy | Can hallucinate or paraphrase | Returns the article verbatim |
| Freshness | Frozen at a training cutoff | Live, with amendment dates |
| Verifiable | No | `law_id` + article you can look up |

A plain chatbot is fine for explanations, drafting, or translation. This MCP earns
its keep where being wrong is expensive: law, medicine, finance.

## Tools

| Tool | Description |
| --- | --- |
| `search_laws` | Search laws by title, type, or 法令番号. Returns `law_id` / `law_num` for the other tools. |
| `search_laws_by_keyword` | Full-text search across law bodies; returns the matching sentences and their `elm` positions. Supports wildcards and AND/OR/NOT. |
| `find_law_article` | Resolve a law name/abbreviation and return one article's text — 本則 or 附則 (`provision="suppl"`), as of any date (`asof`) or an exact `revision_id`. Handles `192`, `325条の3`, `第七百九条`, `第9条第2項`. |
| `get_law_content` | A law's metadata, nested 編/章/節 table of contents, 附則/別表 summary, and optionally full text (large laws are saved to `data/`). |
| `get_law_element` | Fetch any element of a law by its raw `elm` path — the escape hatch for 別表 (`AppdxTable[1]`), a whole 附則 block, 前文, 目次, or a specific paragraph. |
| `get_law_revisions` | List a law's amendment history, including 未施行 (promulgated, not yet in force) amendments and their enforcement dates. |
| `search` / `fetch` | ChatGPT Deep Research's two-tool convention — see [ChatGPT / Deep Research](#chatgpt--deep-research). |
| `get_english_law` | Official (unofficial-reference) English translation of a law or a single article, from the Ministry of Justice's [Japanese Law Translation](https://www.japaneselawtranslation.go.jp/) — bilingual text, with an explicit staleness check against the current Japanese revision. |
| `search_english_laws` | Search the JLT catalogue of ~1,000 translated laws by English or Japanese keyword. |
| `lookup_legal_term` | Bilingual legal-term lookup in the official 標準対訳辞書 (5,000+ entries, with usage guidance and example sentences). |
| `compare_law_revisions` | 新旧対照: diff two versions of a law (revision ids, dates ≥ 2017-04-01, `current`, `previous`) article-by-article, or one article in full. |
| `get_law_definitions` | Extract 定義語 (defined terms) — dedicated 定義 articles, 各号 definitions, and inline （以下「X」という。） — with their scope and defining article. |
| `get_article_references` | All outbound references from one article: internal (incl. resolved 前条/前項), external laws (with 法令番号), and 準用/読み替え clauses. |
| `find_where_cited` | Reverse lookup within a law: every article that cites a given article (枝番-exact, kanji-numeral aware). |
| `list_saved_laws`, `read_saved_law`, `get_cache_stats`, `clear_cache` | Operator tools (local file listing, cache introspection). Hidden by default; set `LAW_MCP_ADMIN_TOOLS=1` to expose them. |

## Example use cases

**A foreign resident or worker** asks in plain language and gets the real statute back:
- *"On what grounds can I be deported?"* → `find_law_article("出入国管理及び難民認定法","24")` — the 退去強制 grounds.
- *"Can I be paid less for not being Japanese?"* → `find_law_article("労働基準法","3")` (均等待遇) — discrimination in working conditions by nationality is prohibited.
- *"Max weekly hours? Overtime pay? Paid leave?"* → 労基法 第32条 / 第37条 / 第39条.
- *"Can I take a side job on my visa?"* → `find_law_article("出入国管理及び難民認定法","19")` (資格外活動).

  Pair these with the bundled `jp-foreigner-law-qa` skill, which adds a not-legal-advice caveat automatically.

**Legal research** (lawyers, 行政書士, HR, journalists, students):
- Find every statute on a topic — `search_laws(law_title="個人情報")`.
- Full-text scan all ~9,500 laws for a phrase — `search_laws_by_keyword("在留資格")` returns the laws and the matching sentences.
- Pull an exact provision to quote — `find_law_article("民法","192")`.
- Cache a whole law for offline use — `get_law_content("129AC0000000089", save=True)` writes 民法 under `data/`.

**Building grounded apps & agents** (what this repo is for):
- Give a Claude or OpenAI agent live access to primary law so it cites real articles instead of hallucinating them.
- Back an HR/compliance helper that quotes 労基法 when drafting policy, or a visa-info bot that cites 入管法.

**Learning the law**:
- *"Explain 民法第192条 (即時取得) in plain English"* — fetch the article, then let the model gloss it.
- Compare related provisions — e.g. dismissal across 労基法第20条 (notice) and 労働契約法第16条 (validity).

**Point-in-time & pending amendments (未施行)**:
- *"What did 労基法第32条 say as of 2015-01-01?"* → `find_law_article("労働基準法","32", asof="2015-01-01")` returns the article as it stood on that date.
- *"List pending amendments to 入管法"* → `get_law_revisions("出入国管理及び難民認定法", unenforced_only=True)` returns 未施行 amendments — promulgated but not yet in force — with their enforcement dates, so you can flag "this is changing on X" instead of quoting a rule that's about to be superseded.

**別表 (schedules/tables), e.g. 在留資格 status lists**:
- *"What are the requirements for a 経営・管理 visa?"* → 入管法's list of 在留資格 (status of residence) lives in a 別表 table, not a regular article: `get_law_element("326CO0000000319", "AppdxTable[1]")` fetches 別表第一 directly. This is the same mechanism the bundled `jp-foreigner-law-qa` skill uses.

## Installation

There's also a live hosted instance you can point a client at right away, no
install: `https://e-gov-law-mcp.onrender.com/mcp` (Streamable HTTP, authless —
see [Hosted / HTTP deployment](#hosted--http-deployment)). It's a free-tier
deployment, so it sleeps after ~15 minutes idle and takes a few seconds to wake up
on the next request.

To run it yourself, clone and use a venv:

```sh
git clone https://github.com/gaijindev/e-gov-law-mcp.git
cd e-gov-law-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run it directly to check it starts (it waits on stdio for a client — Ctrl-C to exit):

```sh
.venv/bin/python server.py
```

Once this package is published to PyPI, `uvx e-gov-law-mcp` will be the simplest
way to run it with no clone or venv — the console script and packaging are already
set up for that ([`pyproject.toml`](pyproject.toml)); it isn't published yet.

## Use it with Claude or OpenAI

This is an MCP **server** — it has no chat window of its own. You point an AI
assistant at it, then ask questions in plain language; the assistant calls these
tools to fetch the real law.

### Claude (no coding)

**Fastest option — point at the hosted instance**, no install: in Claude Desktop or
Claude Code, add a remote MCP server at
`https://e-gov-law-mcp.onrender.com/mcp` (see [Installation](#installation) for the
free-tier cold-start caveat).

**Or run it locally.** After [installing from source](#installation), add it to the
config file, then restart Claude:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "e-gov-law": {
      "command": "/absolute/path/to/e-gov-law-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/e-gov-law-mcp/server.py"]
    }
  }
}
```

**Claude Code (CLI)** — one command:

```sh
claude mcp add e-gov-law -- /absolute/path/to/e-gov-law-mcp/.venv/bin/python /absolute/path/to/e-gov-law-mcp/server.py
claude mcp list   # confirm it shows ✓ Connected
```

Then just ask, in plain English or Japanese:
- *"On what grounds can a foreigner be deported in Japan?"* → fetches 入管法第24条
- *"What's the maximum weekly working hours under Japanese law?"* → 労働基準法第32条
- *"Search Japanese law for 個人情報"* → lists matching statutes

### OpenAI (a little Python)

The [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/mcp/) speaks MCP.

```sh
.venv/bin/pip install openai-agents
export OPENAI_API_KEY=sk-...
```

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async def main():
    async with MCPServerStdio(
        params={
            "command": "/absolute/path/to/e-gov-law-mcp/.venv/bin/python",
            "args": ["/absolute/path/to/e-gov-law-mcp/server.py"],
        }
    ) as egov:
        agent = Agent(
            name="JP Law Assistant",
            instructions=(
                "Answer using the e-gov-law tools. Quote the retrieved article "
                "and cite the law name + article number. This is legal information, "
                "not legal advice."
            ),
            mcp_servers=[egov],
        )
        result = await Runner.run(
            agent, "On what grounds can a foreigner be deported in Japan?"
        )
        print(result.final_output)

asyncio.run(main())
```

> Any MCP-compatible client works the same way (Cursor, Cline, etc.) — point it at
> the same `command` + `args`.

## ChatGPT / Deep Research

ChatGPT's [Deep Research / connectors](https://platform.openai.com/docs/mcp) convention
expects exactly two tools, `search` and `fetch`, with a specific id/url/text shape. This
server implements both: `search(query)` runs a full-text keyword search (falling back to
a title search) and returns citable ids; `fetch(id)` resolves an id back to text — a bare
`law_id` for a whole law, or `"{law_id}::{elm_path}"` for one provision. Point a ChatGPT
connector at `https://e-gov-law-mcp.onrender.com/mcp`, or your own
[hosted instance](#hosted--http-deployment), to use it there.

## Hosted / HTTP deployment

`serve_http.py` exposes the same tools over Streamable HTTP (e.g. behind
Dokploy/Traefik) instead of stdio, for a shared/remote deployment:

```sh
LAW_MCP_TOKEN=$(openssl rand -hex 32) .venv/bin/python serve_http.py   # PORT defaults to 8000
```

It refuses to start with neither of the following set, since an unmarked open port
looks like a mistake:

- `LAW_MCP_TOKEN` — a shared-secret bearer token; requests need a matching
  `Authorization: Bearer <token>` header. Use this for a private/internal deployment.
- `LAW_MCP_PUBLIC=1` — run authless, no auth middleware at all. That's a reasonable
  choice here: the underlying data (Japan's e-Gov 法令 statute text) is public and
  free with no API key of its own, and authless is the expected shape for a ChatGPT
  connector or a Claude/MCP directory listing. The server logs a clear "running
  authless" line on startup so this is never silent.

`LAW_MCP_ADMIN_TOOLS=1` additionally exposes the operator tools (`list_saved_laws`,
`read_saved_law`, `get_cache_stats`, `clear_cache`) — leave it unset on a public
deployment.

The live instance at `https://e-gov-law-mcp.onrender.com/mcp` runs exactly this way:
Docker on Render, `LAW_MCP_PUBLIC=1`. To deploy your own copy, this repo includes a
[`render.yaml`](render.yaml) Blueprint that builds the existing `Dockerfile` as-is —
in the Render dashboard, **New → Blueprint**, point it at this repo (or your fork),
and deploy, no extra config needed. Render's free tier spins the instance down after
~15 minutes idle and cold-starts on the next request; fine for testing, worth
upgrading for a directory-listed connector you want reliably reachable.

## Bundled skill

`skill/jp-foreigner-law-qa/SKILL.md` is a Claude skill that maps common immigration,
nationality and employment questions (from a foreigner's perspective) to the right
statute article, retrieves it via this connector, explains it, and adds a
not-legal-advice caveat. Copy the folder into `~/.claude/skills/` (Claude Code/Desktop
auto-discover it there), then just ask in plain language.

## Testing

```sh
.venv/bin/pip install -e ".[dev]"     # or: .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests that hit the real e-Gov API are marked `live` and excluded by default
(`pytest.ini` sets `-m "not live"`); everything else runs against fixtures/mocks.
Run the live suite explicitly with `.venv/bin/python -m pytest -m live`.

## Notes and limitations

- **Coverage:** national in-force law only (Constitution, Acts, Cabinet/Imperial
  Orders, Ministerial Ordinances, Rules — ≈9,500 instruments). It does **not** cover
  local ordinances (条例), case law (判例), agency notices/guidelines (告示・通達), or
  treaties (条約) — those aren't in the e-Gov 法令 database.
- This returns statutory text, not legal advice; verify against the cited source.
- In `search_laws_by_keyword`, `limit` caps the **total number of matched sentences**
  returned across all laws (default 50, e-Gov max 1000), not the number of laws. Very
  small values can return nothing — keep it ≥ 20.
- The e-Gov API signals "no results" with HTTP 404 (code `404001`); the server treats
  that as an empty result set rather than an error. It signals "no such element" for
  an `elm` path with HTTP 400 (code `400021`) — also treated as "nothing there", not
  an error, so a wrong or outdated `elm` path returns `found: false` with a hint
  rather than raising.
- The e-Gov API also has a JSON response body for law data, but it's explicitly
  labeled a **trial/beta** format; this server always requests the standard XML and
  parses that, for a stable schema.

## Privacy policy

This server has no accounts, no analytics, and no telemetry. It does not collect,
store, or transmit any personal data about you or your queries.

- **What it does**: on each tool call it makes a read-only HTTP request to Japan's
  public e-Gov 法令API (`laws.e-gov.go.jp`) and, for the English-translation tools, to
  the Ministry of Justice's [Japanese Law Translation](https://www.japaneselawtranslation.go.jp/)
  site — both public government sources, keyless, no login. Your query text (e.g. the
  law name or keyword you searched) is sent to those sites as part of the request, the
  same as visiting their search pages directly; this server does not log, retain, or
  forward it anywhere else.
- **Local state**: nothing beyond an in-process, in-memory cache (cleared on restart)
  and, only if you explicitly call `get_law_content(..., save=True)` or a >50,000-char
  law triggers auto-save, a plain-text copy of the fetched statute written to local
  disk (`data/`, or `LAW_MCP_DATA_DIR`/a user data directory once installed as a
  package). No other data is written or persisted.
- **Third-party terms**: e-Gov data is provided under Japan's [政府標準利用規約
  (Government Standard Terms of Use) 2.0](https://www.digital.go.jp/copyright), a
  CC-BY-4.0-compatible license; the JLT translations are under the
  [Public Data License 1.0](https://www.japaneselawtranslation.go.jp/en/terms).
- **Self-hosting**: if you run the [hosted HTTP entrypoint](#hosted--http-deployment)
  yourself, you control that deployment and are responsible for any request logging
  your own infrastructure (proxy, load balancer, hosting provider) performs — this
  server's own code does not add any.
- **Contact**: questions about this policy or the project — see
  [Support](#support) below.

## Support

Bug reports and feature requests: [GitHub Issues](https://github.com/gaijindev/e-gov-law-mcp/issues).
This is an independent, unofficial project — not affiliated with the Japanese
government, the Digital Agency, or the Ministry of Justice.

## License

MIT
