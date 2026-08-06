"""Hand-written e-Gov API fixtures.

Shapes mirror live 法令API v2 responses (base64 XML in ``law_full_text``, the
``elm`` fragment being the element's own root, 400021 for a missing element).
The statute is fictional but structurally faithful to 労働基準法: a merged
deletion range, a 附則 whose article numbers collide with the main body, and a
別表.
"""

import base64

LAW_ID = "322AC0000000049"
LAW_NUM = "昭和二十二年法律第四十九号"
LAW_TITLE = "労働基準法"

LAW_INFO = {
    "law_type": "Act",
    "law_id": LAW_ID,
    "law_num": LAW_NUM,
    "promulgation_date": "1947-04-07",
}

REVISION_INFO = {
    "law_revision_id": f"{LAW_ID}_20260717_508AC0000000060",
    "law_type": "Act",
    "law_title": LAW_TITLE,
    "law_title_kana": "ろうどうきじゅんほう",
    "abbrev": "労基法",
    "category": "労働",
    "amendment_promulgate_date": "2026-07-17",
    "amendment_enforcement_date": "2026-07-17",
    "amendment_enforcement_comment": None,
    "amendment_scheduled_enforcement_date": None,
    "amendment_law_id": "508AC0000000060",
    "amendment_law_title": "労働者災害補償保険法等の一部を改正する法律",
    "amendment_law_num": "令和八年法律第六十号",
    "repeal_status": "None",
    "current_revision_status": "CurrentEnforced",
}

ARTICLE_32_XML = """<Article Num="32">
  <ArticleCaption>（労働時間）</ArticleCaption>
  <ArticleTitle>第三十二条</ArticleTitle>
  <Paragraph Num="1">
    <ParagraphSentence>
      <Sentence Num="1">使用者は、労働者に、休憩時間を除き一週間について四十時間を超えて、労働させてはならない。</Sentence>
    </ParagraphSentence>
    <Item Num="1"><ItemTitle>一</ItemTitle><ItemSentence><Sentence>坑内労働</Sentence></ItemSentence></Item>
    <Item Num="2"><ItemTitle>二</ItemTitle><ItemSentence><Sentence>危険有害業務</Sentence></ItemSentence></Item>
  </Paragraph>
  <Paragraph Num="2">
    <ParagraphSentence>
      <Sentence Num="1">使用者は、一週間の各日については、労働者に、休憩時間を除き一日について八時間を超えて、労働させてはならない。</Sentence>
    </ParagraphSentence>
  </Paragraph>
</Article>"""

ARTICLE_24_2_XML = """<Article Num="24_2">
  <ArticleCaption>（賃金の特例）</ArticleCaption>
  <ArticleTitle>第二十四条の二</ArticleTitle>
  <Paragraph Num="1">
    <ParagraphSentence><Sentence Num="1">賃金の支払の特例は、命令で定める。</Sentence></ParagraphSentence>
  </Paragraph>
</Article>"""

SUPPL_PROVISION_XML = """<SupplProvision Extract="true">
  <SupplProvisionLabel>附　則</SupplProvisionLabel>
  <Article Num="122">
    <ArticleTitle>第百二十二条</ArticleTitle>
    <Paragraph Num="1">
      <ParagraphSentence><Sentence Num="1">この法律施行の期日は、勅令で、これを定める。</Sentence></ParagraphSentence>
    </Paragraph>
  </Article>
  <Article Num="32">
    <ArticleTitle>第三十二条</ArticleTitle>
    <Paragraph Num="1">
      <ParagraphSentence><Sentence Num="1">この附則の規定は、公布の日から施行する。</Sentence></ParagraphSentence>
    </Paragraph>
  </Article>
</SupplProvision>"""

APPDX_TABLE_XML = """<AppdxTable>
  <AppdxTableTitle>別表第一</AppdxTableTitle>
  <RelatedArticleNum>（第三十三条関係）</RelatedArticleNum>
  <TableStruct><Table><TableRow><TableColumn><Sentence>製造業</Sentence></TableColumn></TableRow></Table></TableStruct>
</AppdxTable>"""

WHOLE_LAW_XML = f"""<Law Era="Showa" Year="22" LawType="Act" Num="49">
  <LawNum>{LAW_NUM}</LawNum>
  <LawBody>
    <LawTitle Kana="ろうどうきじゅんほう">{LAW_TITLE}</LawTitle>
    <TOC>
      <TOCLabel>労働基準法目次</TOCLabel>
      <TOCChapter Num="1"><ChapterTitle>第一章　総則</ChapterTitle></TOCChapter>
    </TOC>
    <MainProvision>
      <Chapter Num="1">
        <ChapterTitle>第一章　総則</ChapterTitle>
        <Article Num="1">
          <ArticleCaption>（労働条件の原則）</ArticleCaption>
          <ArticleTitle>第一条</ArticleTitle>
          <Paragraph Num="1"><ParagraphSentence><Sentence>労働条件は、労働者が人たるに値する生活を営むための必要を充たすべきものでなければならない。</Sentence></ParagraphSentence></Paragraph>
        </Article>
        <Article Num="29:31">
          <ArticleTitle>第二十九条から第三十一条まで</ArticleTitle>
          <Paragraph Num="1"><ParagraphSentence><Sentence>削除</Sentence></ParagraphSentence></Paragraph>
        </Article>
      </Chapter>
      <Chapter Num="4">
        <ChapterTitle>第四章　労働時間、休憩、休日及び年次有給休暇</ChapterTitle>
        {ARTICLE_32_XML}
      </Chapter>
    </MainProvision>
    {SUPPL_PROVISION_XML}
    <SupplProvision AmendLawNum="令和八年法律第六十号">
      <SupplProvisionLabel>附　則</SupplProvisionLabel>
      <Article Num="7">
        <ArticleTitle>第七条</ArticleTitle>
        <Paragraph Num="1"><ParagraphSentence><Sentence>経過措置は、政令で定める。</Sentence></ParagraphSentence></Paragraph>
      </Article>
    </SupplProvision>
    {APPDX_TABLE_XML}
  </LawBody>
</Law>"""

# elm path -> fragment XML. "" is the whole-law fetch (no elm).
ELEMENTS = {
    "": WHOLE_LAW_XML,
    "MainProvision-Article_32": ARTICLE_32_XML,
    "MainProvision-Article_24_2": ARTICLE_24_2_XML,
    "MainProvision-Article_29:31": '<Article Num="29:31"><ArticleTitle>第二十九条から第三十一条まで</ArticleTitle><Paragraph Num="1"><ParagraphSentence><Sentence>削除</Sentence></ParagraphSentence></Paragraph></Article>',
    "SupplProvision[1]": SUPPL_PROVISION_XML,
    "AppdxTable[1]": APPDX_TABLE_XML,
}


def law_data_response(xml: str) -> dict:
    return {
        "law_info": LAW_INFO,
        "revision_info": REVISION_INFO,
        "law_full_text": base64.b64encode(xml.encode("utf-8")).decode("ascii"),
    }


NOT_FOUND_BODY = {"code": "404001", "message": "検索結果が存在しません。"}
NO_ELEMENT_BODY = {"code": "400021",
                   "message": "要素（elm）に合致する要素が法令本文に存在しません。"}

LAWS_RESPONSE = {
    "total_count": 1,
    "next_offset": None,
    "laws": [{"law_info": LAW_INFO, "revision_info": REVISION_INFO,
              "current_revision_info": REVISION_INFO}],
}

KEYWORD_RESPONSE = {
    "total_count": 2,
    "sentence_count": 2,
    "next_offset": 50,
    "items": [{
        "law_info": LAW_INFO,
        "revision_info": REVISION_INFO,
        "sentences": [
            {"position": "MainProvision-Article_32-Paragraph_1",
             "text": "使用者は、労働者に、<span>休憩時間</span>を除き一週間について四十時間を超えて"},
            {"position": "MainProvision-Article_34-Paragraph_1",
             "text": "使用者は、労働時間が六時間を超える場合においては<span>休憩時間</span>を"},
        ],
    }],
}

REVISIONS_RESPONSE = {
    "law_info": LAW_INFO,
    "revisions": [
        {**REVISION_INFO,
         "law_revision_id": f"{LAW_ID}_20281223_508AC0000000046",
         "amendment_law_title": "民法等の一部を改正する法律の施行に伴う関係法律の整備等に関する法律",
         "amendment_law_num": "令和八年法律第四十六号",
         "amendment_promulgate_date": "2026-06-24",
         "amendment_enforcement_date": "2028-12-23",
         "amendment_scheduled_enforcement_date": "2028-12-23",
         "amendment_enforcement_comment": "民法等の一部を改正する法律の施行の日",
         "current_revision_status": "UnEnforced"},
        REVISION_INFO,
        {**REVISION_INFO,
         "law_revision_id": f"{LAW_ID}_20200401_501AC0000000071",
         "amendment_enforcement_date": "2020-04-01",
         "current_revision_status": "PreviousEnforced"},
    ],
}
