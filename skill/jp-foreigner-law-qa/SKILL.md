---
name: jp-foreigner-law-qa
description: >-
  Answer Japan immigration (入管法), nationality (国籍法) and employment (労働法) law
  questions for foreign residents and workers by retrieving the exact statute
  article through the e-gov-law MCP connector. Use whenever someone asks —
  especially from a foreigner's perspective — about visas / status of residence
  (在留資格), re-entry, residence cards, deportation, refugee status,
  naturalization, or working in Japan: hours, wages, overtime, leave, dismissal,
  harassment, nationality discrimination, technical-intern / 特定技能 rules. Maps
  the question to the right law and article, fetches the text, explains it
  plainly, and adds a not-legal-advice caveat.
---

# Japan immigration & employment law Q&A (for foreigners)

Answer questions about Japanese immigration, nationality and employment law by
retrieving the **actual statute text** with the `e-gov-law` MCP connector, then
explaining it in plain language. Ground every answer in the retrieved article —
never state the content of the law from memory.

## Prerequisite

Requires the `e-gov-law` MCP server (tools `find_law_article`, `search_laws`,
`search_laws_by_keyword`, `get_law_content`, `get_law_element`,
`get_law_revisions`). If those tools are not available, tell the user to
connect/restart the connector and stop.

## Workflow

1. Identify the topic and map it to a law + article using the tables below.
2. Retrieve the text:
   - **Act + article** → `find_law_article(law_name, article)`
   - **Ordinance/order** (施行規則・施行令・基準省令) → `get_law_content(law_id)`
   - **別表 (schedule/table), e.g. 在留資格 lists** → `get_law_element(law_id, elm)`,
     e.g. `get_law_element("326CO0000000319", "AppdxTable[1]")` for 入管法 別表第一.
   - **附則 (transitional/supplementary provisions)** →
     `find_law_article(law_name, article, provision="suppl")`.
   - **Not in the tables / unsure** → `search_laws_by_keyword(term)`, then fetch.
   - **"Is this rule changing soon?" / pending amendments** →
     `get_law_revisions(law_name_or_id, unenforced_only=True)` — lists 未施行
     amendments (promulgated but not yet in force) with their enforcement dates.
3. Quote the relevant portion (Japanese, with an English gloss) and cite the law
   name + article number exactly as returned.
4. Explain in plain language what it means, in general terms, for the person.
5. Always end with the caveat below.

## Question → law / article map

### Status of residence & entry (入管法)
| If they ask about… | Retrieve |
| --- | --- |
| Being refused entry (上陸の拒否) | `find_law_article("出入国管理及び難民認定法","5")` |
| Landing examination / conditions | `find_law_article("出入国管理及び難民認定法","7")` |
| Landing permission seal / status & period granted | `find_law_article("出入国管理及び難民認定法","9")` |
| Activities outside their status (資格外活動) | `find_law_article("出入国管理及び難民認定法","19")` |
| Residence card / mid-to-long-term residents (在留カード) | `find_law_article("出入国管理及び難民認定法","19条の3")` |
| Changing status of residence | `find_law_article("出入国管理及び難民認定法","20")` |
| Extending period of stay | `find_law_article("出入国管理及び難民認定法","21")` |
| Permanent residence (永住) | `find_law_article("出入国管理及び難民認定法","22")` |
| Status revocation (在留資格の取消し) | `find_law_article("出入国管理及び難民認定法","22条の4")` |
| Re-entry permit (再入国許可) | `find_law_article("出入国管理及び難民認定法","26")` |
| Requirements for each status of residence | `get_law_content("402M50000010016")` (基準省令) |

### Deportation, detention & refugees (入管法)
| If they ask about… | Retrieve |
| --- | --- |
| Grounds for deportation (退去強制) | `find_law_article("出入国管理及び難民認定法","24")` |
| Violation investigation / deportation procedure | `find_law_article("出入国管理及び難民認定法","27")` |
| Provisional release from detention (仮放免) | `find_law_article("出入国管理及び難民認定法","54")` |
| Refugee recognition (難民認定) | `find_law_article("出入国管理及び難民認定法","61条の2")` |
| Penalties for overstay / illegal stay | `find_law_article("出入国管理及び難民認定法","70")` |
| Special permanent residents (特別永住者) | `get_law_content("403AC0000000071")` (入管特例法) |
| Technical intern training (技能実習) | `find_law_article("外国人の技能実習の適正な実施及び技能実習生の保護に関する法律", <article>)` |

### Nationality (国籍法)
| If they ask about… | Retrieve |
| --- | --- |
| Nationality by birth | `find_law_article("国籍法","2")` |
| Conditions for naturalization (帰化) | `find_law_article("国籍法","5")` |

### Employment — applies to foreign workers the same as nationals (労働基準法 unless noted)
| If they ask about… | Retrieve |
| --- | --- |
| **Discrimination by nationality** in working conditions | `find_law_article("労働基準法","3")` (均等待遇) |
| Forced labor ban | `find_law_article("労働基準法","5")` |
| Equal pay regardless of sex | `find_law_article("労働基準法","4")` |
| What conditions the employer must disclose at hiring | `find_law_article("労働基準法","15")` |
| Restrictions on dismissal (injury / maternity) | `find_law_article("労働基準法","19")` |
| Advance notice of dismissal | `find_law_article("労働基準法","20")` |
| Certificate on resignation/dismissal | `find_law_article("労働基準法","22")` |
| How/when wages must be paid | `find_law_article("労働基準法","24")` |
| Maximum working hours | `find_law_article("労働基準法","32")` |
| Break time | `find_law_article("労働基準法","34")` |
| Days off | `find_law_article("労働基準法","35")` |
| Overtime / holiday work agreement (36協定) | `find_law_article("労働基準法","36")` |
| Overtime / holiday / night premium pay | `find_law_article("労働基準法","37")` |
| Paid annual leave (有給) | `find_law_article("労働基準法","39")` |
| Maternity (pre/post-birth) leave | `find_law_article("労働基準法","65")` |
| Work rules (就業規則) | `find_law_article("労働基準法","89")` |
| Whether a dismissal is valid | `find_law_article("労働契約法","16")` |
| Dismissal during a fixed term | `find_law_article("労働契約法","17")` |
| Fixed-term → permanent conversion (無期転換) | `find_law_article("労働契約法","18")` |
| Non-renewal of a fixed-term contract (雇止め) | `find_law_article("労働契約法","19")` |
| Minimum wage | `find_law_article("最低賃金法","4")` |
| Equal treatment for part-time / fixed-term | `find_law_article("短時間労働者及び有期雇用労働者の雇用管理の改善等に関する法律","8")` |
| Sexual / maternity harassment | `find_law_article("雇用の分野における男女の均等な機会及び待遇の確保等に関する法律","11")` |
| Power harassment (パワハラ) | `find_law_article("労働施策の総合的な推進並びに労働者の雇用の安定及び職業生活の充実等に関する法律","30条の2")` |
| Work-injury compensation (労災) | `get_law_content("322AC0000000050")` (労災保険法) |
| Unemployment insurance (雇用保険) | `get_law_content("349AC0000000116")` |
| Childcare / family-care leave | `get_law_content("403AC0000000076")` (育児・介護休業法) |

If a question is not in these tables, use `search_laws_by_keyword` with a key term
(e.g. "在留資格", "解雇", "育児休業") or `search_laws` by title, then fetch the article.

## Common 在留資格 (status of residence) — context

The full list is in 入管法 別表第一・第二 — fetch it directly with
`get_law_element("326CO0000000319", "AppdxTable[1]")` (別表第一) or
`"AppdxTable[2]"` (別表第二); requirements are in the 基準省令
(`get_law_content("402M50000010016")`). Frequently asked: 技術・人文知識・国際業務,
技能, 特定技能（1号・2号）, 技能実習, 経営・管理, 留学, 家族滞在, 定住者, 永住者, 日本人の配偶者等.
特定技能 and 技能実習 are defined under 入管法 別表 + the 基準省令; the employment
statutes above apply to those workers in full.

## Worked example (follow this shape)

> **User:** "I'm on a 技術・人文知識・国際業務 visa — can I be deported for doing paid
> delivery work on the side?"
>
> 1. Side work outside the visa = 資格外活動 → `find_law_article("出入国管理及び難民認定法","19")`;
>    deportation link → `find_law_article("出入国管理及び難民認定法","24")`.
> 2. Quote 第19条 (engaging in income activities outside the authorized status needs
>    permission) and note 第24条 lists working without that permission among the
>    grounds for deportation.
> 3. Explain plainly: unauthorized side work breaches your status and is a
>    deportation ground; a 資格外活動許可 is normally required first.
> 4. Add the caveat.

## Mandatory caveat (include in every answer)

> This is general legal **information** taken from the statute, not legal advice.
> Outcomes depend on your specific facts and the discretion of the 出入国在留管理庁 /
> the authorities. Much of immigration practice lives in 審査要領 and 通達 that are
> **not** in this database. For an actual case, consult a 行政書士 (status-of-residence
> applications) or a 弁護士 (disputes / appeals).

## What this skill cannot do — say so explicitly when asked

- Predict whether a specific application will be approved (審査要領 / discretion — not in e-Gov).
- Give procedures, required documents, forms, or processing times (agency guidance, not statute).
- Cite case law or court decisions (判例 not covered).
- Cover municipal matters such as residence registration or 国民健康保険 specifics (条例 / local practice).
