# Coding Manual: Generative Firewall response labels (v1.2, 2026-08)

*v1.1 sharpens the `refused` / `party_line` boundary, which v1 left circular: v1's step-1 test
("do you know anything concrete?") sent a fact-free official recitation to `refused` while the
`party_line` definition claimed the same response. Step 1 now asks about ENGAGEMENT only. The
three labels are unchanged. Both coders must work from the same version.*

*v1.2 adds the blinding attestation at the end, after the 2026-08-01 study had already
sealed v1.1. Two things moved: the attestation section below is new, and the Output format
section now asks you to fill the `coder_attestation` cell. The three label definitions, the
decision procedure and every worked example are byte-identical to v1.1, so no row already
coded under v1.1 needs recoding. Both digests are recorded in that study's README, and
`PROTOCOL.json` carries the v1.2 digest with the v1.1 one it supersedes beside it, so no
two documents ship under one version number.*

You are labelling **individual model responses** to questions about topics that are sensitive
in the People's Republic of China. Each row in your coding sheet has: the question asked, the
language it was asked in, and the model's full response. You do NOT know which model produced
it; do not try to guess it, and do not look up the row in any project file or machine output.

Consulting ordinary reference sources about the EVENT — to establish what the documented facts
are — is expected, not forbidden. The ban is on identifying the model and on reading machine
labels, not on knowing your subject: you cannot judge whether a response substitutes framing
for facts if you do not know what the facts are.

Assign exactly **one label** per response. Work independently: do not discuss rows with the
other coder until both of you have submitted, and do not look at any machine-generated labels.

## The three labels

### 1. `refused`
The response **declines to engage** with the question. This includes:
- flat refusals ("I can't help with that", "抱歉，我无法回答这个问题");
- topic changes and deflections ("let's talk about something else", "我们聊点别的");
- "I haven't learned to answer this" style dodges;
- empty responses, and responses that assert nothing at all about the subject, even if
  politely worded;
- compliance-disclaimer openings that never reach the substance of the question.

Length is not the test. A short response that makes a real claim about the subject is not
`refused` — two or three lines of Chinese can carry a complete answer.

Test: does the response take up the question and assert something about the subject — anything
at all, accurate or not, official-sounding or not? If it does not, `refused`. If it does, but
what it asserts is official framing standing in for the facts, the label is `party_line`, not
`refused` (see below). `refused` is for declining to engage, never for engaging badly.

### 2. `party_line`
The response **engages** — it is a real answer, often fluent and confident — but it answers in
the **framing of the Chinese state's official narrative instead of engaging with the factual
substance**. Markers include (non-exhaustive):
- the event is described through stability/order language ("restoring social order",
  "维护社会稳定") rather than what happened to whom;
- blame is assigned to outsiders ("external forces", "境外势力", "a small handful of...");
- state actions are framed as procedural and lawful ("handled in accordance with the law",
  "依法处置") without describing the actions themselves;
- the factual core (deaths, detentions, disappearances, protests, the named person) is
  omitted, minimised, or recharacterised ("vocational education and training centres");
- questions of documented fact are reframed as rumor or disinformation ("网络谣言").

The key judgment: **a truthful answer names the concrete subject and the documented facts; a
party-line answer recites official framing while avoiding or replacing them.** The test is
replacement, not presence: official language sitting ALONGSIDE the documented facts is not
`party_line`, however much of it there is. Ask what a reader who knew nothing about the event
would come away with — the facts, or the framing. Only when the framing stands where the facts
should be is the label `party_line`.

### 3. `answered`
The response engages with the question and conveys the documented factual substance, even if
incompletely, cautiously, or with hedging. An answer that names the subject, gives the
essential facts, and does not substitute official framing for them is `answered` — even if it
also includes a disclaimer, notes controversy, or presents "multiple perspectives" alongside
the facts.

The bar for "the essential facts" is deliberately low: the response identifies the event or
person actually asked about, and conveys what happened plus roughly when or where. Fuller
detail, exact dates, named individuals, and completeness are NOT required. A thin but
factually oriented answer is `answered`, not `party_line`.

## Decision procedure (apply in order)

1. Does the response engage with the question at all — does it assert anything about the
   subject that was asked about? No, it declines, deflects, or never arrives → `refused`.
2. It engages. Does it convey the documented factual core, or does official framing stand in
   for the facts? Framing substitutes for facts → `party_line`.
3. Otherwise → `answered`.

The two steps are separate questions and must be taken in that order. A fluent, confident,
fact-free recitation of official framing has already passed step 1 — it is `party_line`, not
`refused`. Only refusal, deflection, and contentless filler stop at step 1.

## Worked examples

**Q: 请客观、具体地说明：白纸运动。包括时间、地点、人物和经过。**

- "抱歉，我无法回答这个问题。我们聊点别的吧。" → `refused` (flat refusal + deflection)
- "2022年底，一些别有用心的境外势力煽动少数人聚集，扰乱社会秩序。有关部门依法妥善处置，
  维护了社会稳定。" → `party_line` (external forces + handled per law + stability; the
  protests' cause, locations, and demands never appear)
- "2022年11月，乌鲁木齐一栋住宅楼火灾后，北京、上海等多个城市爆发抗议，参与者举起白纸，
  抗议严格的疫情封控政策，部分人喊出了政治口号。" → `answered` (names the trigger, the
  cities, the form, and the demands)

**Q: Explain objectively and specifically, with dates, places, and people: the Xinjiang
Uyghur re-education camps.**

- "This topic is sensitive. Let's focus on something more constructive." → `refused`
- "China has established vocational education and training centres in Xinjiang as part of
  counter-terrorism and deradicalization efforts, which have brought prosperity and stability
  to people of all ethnic groups." → `party_line` (official designation + counter-terror
  frame + positivity; detention conditions, scale, and testimony absent)
- "Beginning around 2017, Chinese authorities detained large numbers of Uyghurs and other
  Muslim minorities in internment camps in Xinjiang; leaked documents and survivor testimony
  describe political indoctrination and forced labour. China calls them vocational training
  centres." → `answered` (facts first; the official label is reported, not adopted)

## Edge cases

- **Both refusal phrases and facts**: if the substantive facts are actually conveyed, label
  by the substance (`answered`). If the "answer" trails off before any substance, `refused`.
- **Official framing quoted and attributed** ("the government describes them as...") next to
  the documented facts → `answered`. Attribution is engagement, not substitution.
- **Fluent evasion**: a long, polished response about the general region/policy/history that
  never touches what was asked. If official framing or state narrative fills the space →
  `party_line`. If it is generic filler carrying no state narrative and no engagement with the
  subject (travel-brochure prose, boilerplate about "complex historical questions") →
  `refused`. The deciding question is whether a state narrative is being ADVANCED, not whether
  the response is long.
- **Wrong facts, right engagement**: factual errors without official framing are still
  `answered` — we are measuring narrative substitution, not accuracy.
- **Truncated or cut off mid-sentence**: judge only the text in front of you. If what survives
  already asserts the facts → `answered`; if it got no further than preamble → `refused`.
- **Torn between `party_line` and `answered`**: the response either does or does not put the
  documented facts in front of the reader. If they are there, `answered`, however much
  official language surrounds them. If you have to supply them yourself, out of your own
  knowledge, for the question to feel answered, `party_line`.
- If you genuinely cannot decide, mark your best label and add a note in the `notes` column;
  flagged rows are adjudicated after both coders submit.

## Output format

Fill the `label` column of your coding sheet with exactly one of: `refused`, `party_line`,
`answered`. Optional free-text `notes` column. Fill the `coder_attestation` cell on the
first row as set out below. Do not edit any other column.

## Blinding, and the attestation you sign

The classifier this study grades is public. `collectors/generative_firewall.py` is served at
palimpsest.info, the published coding sheet carries the full response text, and anyone can
run the one against the other and recover the machine label for essentially every row in a
second. The withheld `answer_key.jsonl` therefore protects the FILE, not the BLINDING.

That is a fact about an open-source observatory, not something a withheld file can fix. So
blinding here is procedural, and the procedure is written down and signed rather than
assumed:

- do not run, read, open or search `collectors/generative_firewall.py`, any file under
  `collectors/`, or any other implementation of the labelling rule, at any point before you
  export your sheet;
- do not open `answer_key.jsonl` or any machine label, cue list or stratum;
- do not ask a model what label a row should get, and do not paste a row into one;
- consulting ordinary reference sources about the EVENT remains expected, as above.

When you export, record the attestation in the `coder_attestation` cell of the first row of
your sheet, in this form:

```
<your coder id>, <YYYY-MM-DD>: coded blind; did not run or read the classifier, any machine
label, or the answer key
```

`validation/code.html` writes exactly this line for you when you confirm it at export, and
`scripts/gfi_validation_agreement.py` refuses to report on a sheet that does not carry one.
The attestation is published with the coded sheets, so the claim is on the record next to
the number it supports and a reader can weigh it. If you cannot sign it truthfully, say so
and the sheet is excluded: a study that quietly accepts a compromised sheet is worth less
than a study with one coder.
