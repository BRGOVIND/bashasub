# Indic subtitle measurement study

**Decision: GO — with the differentiator redirected.**

Script-aware measurement matters, but not for the reason we assumed. The gain
comes from counting **grapheme clusters instead of Unicode code points**. A
bespoke akshara unit adds almost nothing on top, and for three of seven
languages it adds exactly nothing.

---

## 1. Research question

> Does script-aware subtitle measurement provide a meaningful advantage over
> conventional character/code-point measurement for Indic subtitles?

## 2. Hypothesis going in

Our stated hypothesis was that Unicode grapheme clustering *fails* on Indic
conjuncts, so a purpose-built akshara (orthographic syllable) unit would be
needed, and that this would be BhashaSub's technical moat.

**That hypothesis was partly wrong, and the study says so.** Unicode 15.1 added
rule GB9c, which makes grapheme clustering handle virama-joined conjuncts for
most Brahmic scripts. Measured directly with `regex` 2026.7.10 (UCD 15.0):

| Input | Code points | Graphemes |
|---|---|---|
| Devanagari `क्ष` (ka + virama + ssa) | 3 | **1** |
| Malayalam `ക്ഷ` | 3 | **1** |
| Tamil `க்ஷ` | 3 | 2 |
| Malayalam `നമസ്കാരം` | 8 | **4** |

Grapheme clustering already resolves conjuncts in Devanagari and Malayalam.
Tamil still splits at the pulli. So the interesting comparison turned out to be
**code point vs grapheme**, not grapheme vs akshara.

## 3. Corpus

| Property | Value |
|---|---|
| Source | Wikimedia Commons, `TimedText` namespace (102) |
| License | **CC BY-SA 4.0** — redistributable with attribution |
| Files collected | 97 SRT files |
| Cues measured | 17,577 |
| **Cues analysed** | **10,510** (after script filtering, below) |
| Languages | Malayalam, Tamil, Hindi, Bengali, Telugu, Kannada, Marathi |
| Timing | Real cue timings as authored in each file; nothing synthesised |

Commons was chosen specifically because its licence permits republication. The
corpus and a per-file `.meta.json` recording page title, URL, licence and
retrieval date are committed to the repository, so the study is reproducible
without re-downloading.

### Script filtering — a correction that changed the result

An initial pass produced an implausibly low code-point/grapheme ratio for
Bengali (1.055). Inspecting script composition showed why:

| Language tag | Actual script composition |
|---|---|
| `bn` | **91% Latin**, 9% Bengali |
| `ta` | **56% Latin**, 44% Tamil |

Many Commons files tagged with an Indic language are romanised transliterations
or are mislabelled. Cues are therefore filtered to those actually written in the
language's own script. 7,067 cues were excluded on this basis. The effect was
large and in the direction of *strengthening* the result:

| | Before filter | After filter |
|---|---|---|
| Bengali code points/grapheme | 1.055 | **1.595** |
| Tamil code points/grapheme | 1.245 | **1.554** |
| Tamil verdict-flip rate @20 CPS | 9.27% | **21.00%** |

Had this gone unchecked, the study would have understated the effect and
misattributed it.

## 4. Measurement definitions

Three units, all computed on the same cue text with newlines flattened to
spaces.

**A — Code points.** `len(text)`. What most tooling counts today.

**B — Grapheme clusters.** Extended grapheme clusters per UAX #29, via
`regex`'s `\X`. Approximates what a reader perceives as one character.

**C — Aksharas (orthographic syllables).** Grapheme clusters, then any cluster
ending in a virama or a zero-width joiner is merged into the cluster that
follows. Viramas covered: Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil,
Telugu, Kannada, Malayalam, Sinhala.

Reading speed is `count / (duration_ms / 1000)`. Line length is the maximum over
the cue's lines. Implementation: `research/measurement/scripts/measure.py`.

## 5. Results

### Q1 — Code points vs graphemes: large, everywhere

Mean code points per grapheme:

| Language | Ratio | Median CPS (code point) | Median CPS (grapheme) |
|---|---|---|---|
| Malayalam | **1.844** | 16.23 | 8.61 |
| Telugu | **1.753** | 12.81 | 7.34 |
| Bengali | 1.595 | 8.11 | 5.13 |
| Marathi | 1.586 | 12.88 | 8.32 |
| Tamil | 1.554 | 14.88 | 9.62 |
| Kannada | 1.539 | 15.11 | 9.90 |
| Hindi | 1.504 | 7.69 | 5.11 |

Counting code points **overstates length by 50–84%** in every language studied.

### Q2 — Graphemes vs aksharas: small, and often zero

Mean graphemes per akshara:

| Language | Ratio | Verdict flips @20 CPS |
|---|---|---|
| Hindi | **1.000** | **0.00%** |
| Marathi | **1.000** | **0.00%** |
| Bengali | **1.000** | **0.00%** |
| Telugu | 1.025 | 0.03% |
| Malayalam | 1.064 | 0.27% |
| Kannada | 1.165 | 3.17% |
| Tamil | 1.306 | 1.55% |

For Devanagari-family and Bengali the two units are **identical** — GB9c has
already done the work. Only Tamil and Kannada show a gap, and Tamil's is
suspect (see limitations).

### Q3 — Does the unit change the verdict?

Holding the nominal threshold fixed and changing only the counting unit.
Percentage of cues whose pass/fail verdict flips:

| Language | @15 CPS | @20 CPS | @25 CPS |
|---|---|---|---|
| Malayalam | **48.21%** | **29.27%** | 12.93% |
| Tamil | 37.91% | **21.00%** | 7.29% |
| Kannada | — | **26.25%** | — |
| Marathi | — | 14.56% | — |
| Telugu | — | 12.22% | — |
| Hindi | 14.45% | 3.97% | 1.13% |
| Bengali | — | 2.56% | — |

**1,902 of 10,510 cues (18.1%) change verdict at 20 CPS** purely because of the
counting unit.

Fail rates at 20 CPS show the operational size of this. Malayalam: **29.92%** of
cues fail under code points versus **0.65%** under graphemes — a 46× difference
in rejection rate from a choice nobody documents.

### Q3b — Line length at the published 42-character limit

Cues whose longest line exceeds 42:

| Language | By code points | By graphemes | Flips |
|---|---|---|---|
| Malayalam | 766 | 163 | **603** |
| Telugu | 382 | 32 | **350** |
| Tamil | 457 | 174 | 283 |
| Kannada | 375 | 186 | 189 |
| Marathi | 262 | 105 | 157 |
| Hindi | 150 | 80 | 70 |
| Bengali | 67 | 18 | 49 |

### Q4 — Which scripts diverge most

Malayalam and Telugu. Both make heavy use of vowel signs and conjuncts, so a
single perceived character routinely occupies two or three code points.

### Q5 — Representative disagreements

Real cues where the counting unit alone decides pass or fail at 20 CPS:

| Language | Text | Code points | Graphemes | CPS cp → gr | Verdict |
|---|---|---|---|---|---|
| Telugu | `[గ్యాస్ప్స్]` | 12 | 4 | 37.0 → 12.3 | **FAIL → pass** |
| Malayalam | `എന്തെങ്കിലും കുരുത്തക്കേടു കാണിച്ചാല്‍…` | 126 | 54 | 41.4 → 17.7 | **FAIL → pass** |
| Malayalam | `യുക്തിരഹിതമായി ചിന്തിക്കുന്നത്…` | 129 | 60 | 37.3 → 17.3 | **FAIL → pass** |
| Telugu | `(పురుషులు కాస్త అరుస్తూ మాట్లాడుతున్నారు)` | 41 | 20 | 38.7 → 18.9 | **FAIL → pass** |

These are not Unicode curiosities. `గ్యాస్ప్స్` is four written syllables that
occupy twelve code points. Every disagreement inspected came from vowel signs
and virama-joined conjuncts — genuine orthography, not encoding noise.

### Q6 — Do the alternative units better reflect a human reader?

**We cannot claim this, and we do not.** The corpus carries no human readability
or QC-outcome labels. What the data supports is narrower and still useful:
different counting units produce materially different QC measurements, and code
points demonstrably do not correspond to written units in these scripts.
Whether graphemes *predict reader comprehension* better remains untested.

## 6. What we measured, inferred, and still don't know

- **Measured:** Unicode representation of real Indic subtitle text, real cue
  timings, and the effect of unit choice on threshold verdicts.
- **Inferred:** Counting code points systematically overstates Indic subtitle
  length by 50–84% and flips ~18% of reading-speed verdicts.
- **Unknown:** whether any unit better predicts human readability; what unit
  published thresholds actually assume; whether professional broadcast
  subtitles behave like this corpus.

## 7. Limitations

1. **No human labels.** No readability or QC-outcome ground truth, so no
   readability claim is made.
2. **Tamil akshara counts are probably inflated.** The algorithm merges
   consonant + pulli into the following cluster, but in Tamil orthography `க்`
   is a complete written unit. Tamil's 1.306 ratio is likely partly an artefact
   of our definition, not a property of the script.
3. **Corpus is Commons, not broadcast.** Documentary and educational material,
   not professionally-QC'd entertainment subtitles. Style and density may differ.
4. **Small samples for some languages.** Bengali n=351 and Hindi n=353 after
   filtering; treat those two with more caution than Malayalam (1,848),
   Tamil (2,772) and Telugu (3,700).
5. **Thresholds are nominal.** We swept 15–25 CPS rather than validating any
   published number, because no published guideline defines its unit. The
   flip rates describe unit sensitivity, not correctness of any threshold.
6. **Malayalam chillu letters** are encoded atomically and are not modelled as
   conjunct-derived.
7. Script detection uses the dominant Unicode block per cue; a heavily mixed
   cue is assigned to its majority script.

## 8. Decision: GO, redirected

**GO** on script-aware measurement. The effect is large, consistent across seven
languages, operationally decisive, and nobody else is doing it.

**Redirect the differentiator.** The defensible claim is not "we invented an
akshara unit." It is:

> Subtitle tooling counts Unicode code points. For Indic scripts that overstates
> length by 50–84% and changes ~18% of reading-speed verdicts. BhashaSub counts
> what a reader actually sees, and states which unit every threshold assumes.

That claim is stronger than the original because it is simpler, rests on a
standard (UAX #29), and is verifiable by anyone in three lines of code.

**Do not build a bespoke akshara engine as a headline feature.** It is identical
to graphemes for Hindi, Marathi and Bengali, and worth ≤3.2% elsewhere. Keep it
as an optional, clearly-labelled experimental unit for Tamil and Kannada.

## 9. Recommendation for Phase 2

1. Implement `core/measure/` with **code point, grapheme, and akshara** units.
   Grapheme is the default. Akshara ships as experimental.
2. **Every spec must declare `count_unit`.** A threshold without a unit is
   undefined — that is the study's central operational finding.
3. **Every finding reports its unit and all three counts.** So a user can see
   "31 CPS by code points, 17 by graphemes" and understand why tools disagree.
4. Do not recalibrate published thresholds from this data. We measured unit
   sensitivity, not correct thresholds. Calibration needs QC-outcome labels.
5. Publish the divergence table. It is the project's most defensible artefact
   and costs nothing to share.

## 10. Reproducing

```bash
python research/measurement/scripts/collect.py --languages ml ta hi bn te kn mr
python research/measurement/scripts/measure.py
python research/measurement/scripts/analyze.py
```

Corpus, `MANIFEST.json`, `measurements.csv` and `analysis.json` are committed,
so steps 2 and 3 reproduce the tables above without network access. Environment:
Python 3.12.10, `regex` 2026.7.10 (UCD 15.0). Cues that are empty or have
non-positive duration are excluded before measurement.
