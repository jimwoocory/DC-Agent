# DC-Agent Content SOP

This SOP covers employee-facing content production for the client department
and planning department.

## When To Use

Use this workflow when an employee asks for any combination of:

- client copy, invitation copy, private-domain messages, or follow-up scripts
- image prompts, posters, visual briefs, or generated-image direction
- short-video scripts, storyboards, voiceover, or video-generation prompts

## Required Intake

Before generation, the assistant must identify:

- department: client department, planning, or unknown
- content type: copy, image, video, campaign, or mixed
- material status: ready, partial, or needs materials
- risk level: normal, fact-sensitive, client-commitment, or brand-sensitive

If key facts are missing, the assistant must pause generation and ask for the
missing inputs instead of drafting unsupported output.

## Knowledge Context

Before producing final content, the assistant should use available source
context from:

- knowledge-base retrieval
- Obsidian/NAS document graph metadata
- Feishu document or attachment summaries
- current conversation materials

Final output must separate source-backed facts from creative assumptions and
cite available source paths or document titles.

## Output Standard

Client department outputs must include:

- send-ready copy
- title options
- image prompt when requested
- video script when requested
- source citations
- risk notes
- next actions

Planning outputs must include:

- creative brief
- key insight
- video script
- storyboard
- voiceover or subtitles
- image prompt
- source citations
- review checklist

## Media Generation

Image and video tools must consume structured SOP prompts. Raw employee text
must be wrapped into a prompt with business brief, model prompt, style notes,
and forbidden elements before calling GPT Image or Dreamina.

All media-generation Feishu cards must use the shared card runtime gateway.

## Review

Content involving client commitments, prices, discounts, official brand claims,
or customer identity must be reviewed by an employee before external use.

## Employee SOP

### Start A Request

Use one message that names the scenario and the intended channel.

Good examples:

- `帮我写一条端午客户微信问候话术，对象是 VIP 老客户，目标是邀约到店`
- `帮我做一个视频号短视频分镜，项目是之光EV，目标是活动预热`
- `帮我做客户邀约文案、配图 prompt 和 15 秒视频脚本`

Do not ask the assistant to invent company facts. If the activity rule, customer
segment, price, benefit, platform, or brand guideline is not ready, provide it
or wait for the material-intake card.

### Supplement Materials

When a material-intake card appears, reply with the missing fields in plain
language. The recommended format is:

```text
客户/受众：
触达场景：
品牌/产品：
业务目标：
活动权益：
发送渠道：
禁用内容：
来源/附件：
```

For planning and video work, use:

```text
项目/品牌：
传播目标：
目标人群：
发布平台：
时长/画幅：
已有资料：
品牌口径/禁用元素：
```

### Confirm Delivery

Before sending or publishing, check these items:

- source citations match the supplied documents or knowledge-base entries
- assumptions are clearly marked as creative assumptions
- risk notes do not contain unapproved price, benefit, commitment, or brand claim
- image prompt and video storyboard match the requested channel and format
- next actions say who should confirm or publish

If any item is wrong, reply with the correction. Do not send the draft externally
until the review card is confirmed.

### Roll Back

For generated images or videos, keep the media generation record id shown on the
card. If a generated asset is wrong, reply:

```text
回滚这次生成记录：<record_id>，原因：<原因>，请基于以下资料重做：...
```

The next generation must not reuse the rejected output as a source fact.
