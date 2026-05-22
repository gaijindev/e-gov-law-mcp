# e-Gov Law MCP

An [MCP](https://modelcontextprotocol.io) server that wraps Japan's **e-Gov 法令API v2**
(<https://laws.e-gov.go.jp/api/2>) so an LLM can search Japanese laws, run full-text
keyword searches, fetch law text, and pull out individual articles by number.

The API is free and needs no key. Common laws (六法 + key modern legislation) and their
everyday abbreviations are mapped directly, so "民法" or "労基法" resolve without a search
round-trip. Articles are located by parsing the law's standard XML and matching the
`<Article Num="...">` attribute, which is more reliable than scraping flattened text.

## Contents

- [Why an MCP instead of a chatbot?](#why-an-mcp-instead-of-a-chatbot)
- [Tools](#tools)
- [Example use cases](#example-use-cases)
- [Installation](#installation)
- [Use it with Claude or OpenAI](#use-it-with-claude-or-openai)
- [Bundled skill](#bundled-skill)
- [Notes and limitations](#notes-and-limitations)
- [License](#license)

## Why an MCP instead of a chatbot?

A chatbot is the reasoning + language layer; an MCP is the tools + data layer that
grounds it. Asked from memory, a model can invent article numbers, cite repealed
law, or be stale at its training cutoff — and you can't check it. With this MCP the
assistant fetches the **actual current text** from the official e-Gov database and
cites the `law_id` + article, so the answer is grounded and verifiable.

| | Plain chatbot (from memory) | + this MCP |
| --- | --- | --- |
| Source | Statistical recall of text it saw | Official e-Gov database (≈9,500 laws) |
| Accuracy | Can hallucinate / paraphrase | Returns the article verbatim |
| Freshness | Frozen at a training cutoff | Live, with amendment dates |
| Verifiable | "Trust me" | `law_id` + article you can cite & check |
| Behavior | Probabilistic | Deterministic, testable tool calls |

A plain chatbot is fine for explanations, drafting, or translation. The MCP earns
its keep when being wrong is expensive — law, medicine, finance. In short: a plain
chatbot is a closed-book exam; an MCP-backed one is open-book with the statute on
the desk.

## Tools

| Tool | Description |
| --- | --- |
| `search_laws` | Search laws by title, type, or 法令番号. Returns `law_id` / `law_num` for the other tools. |
| `search_laws_by_keyword` | Full-text search across law bodies; returns the matching sentences. Supports wildcards and AND/OR/NOT. |
| `find_law_article` | Resolve a law name/abbreviation and return one article's text. Handles `192`, `325条の3`, `第七百九条`, `第9条第2項`. |
| `get_law_content` | A law's metadata, table of contents, article count, and optionally full text (large laws are saved to `data/`). |
| `list_saved_laws` | List law text files cached under `data/`. |
| `read_saved_law` | Read a slice of a saved law text file. |

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

## Installation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run it directly to check it starts:

```sh
.venv/bin/python server.py
```

## Use it with Claude or OpenAI

This is an MCP **server** — it has no chat window of its own. You point an AI
assistant at it, then ask questions in plain language; the assistant calls these
tools to fetch the real law. Do the [Installation](#installation) above first, then
note this folder's absolute path (`pwd` prints it) — you'll paste it below.

### Claude (no coding)

**Claude Desktop** — add the server to the config file, then restart Claude:
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

**Claude Code (CLI)** — one command (or drop the same `.mcp.json` in your project):

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

## Bundled skill

`skill/jp-foreigner-law-qa/SKILL.md` is a Claude skill that maps common immigration,
nationality and employment questions (from a foreigner's perspective) to the right
statute article, retrieves it via this connector, explains it, and adds a
not-legal-advice caveat. Copy the folder into `~/.claude/skills/` (Claude Code/Desktop
auto-discover it there), then just ask in plain language.

## Notes and limitations

- **Coverage:** national in-force law only (Constitution, Acts, Cabinet/Imperial
  Orders, Ministerial Ordinances, Rules — ≈9,500 instruments). It does **not** cover
  local ordinances (条例), case law (判例), agency notices/guidelines (告示・通達), or
  treaties (条約) — those aren't in the e-Gov 法令 database.
- This returns statutory text, not legal advice; verify against the cited source.
- In `search_laws_by_keyword`, `limit` caps the **total number of matched sentences**
  returned across all laws (e-Gov default 100, max 1000), not the number of laws. Very
  small values can return nothing — keep it ≥ 20.
- The e-Gov API signals "no results" with HTTP 404 (code `404001`); the server treats
  that as an empty result set rather than an error.

## License

MIT
