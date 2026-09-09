# Indic subtitle measurement study

Research tooling for the Phase 1 GO/NO-GO gate: does script-aware measurement
beat code-point counting for Indic subtitles?

**Findings and the decision live in [`docs/measurement-study.md`](../../docs/measurement-study.md).**
This file covers how to run it.

## Layout

```
scripts/collect.py   fetch CC BY-SA subtitles from Wikimedia Commons
scripts/measure.py   parse cues, count under three units, write measurements.csv
scripts/analyze.py   statistics, threshold sweep, disagreement analysis
corpus/              97 SRT files + per-file .meta.json + MANIFEST.json
results/             measurements.csv, analysis.json
```

## Run

```bash
python research/measurement/scripts/collect.py --languages ml ta hi bn te kn mr
python research/measurement/scripts/measure.py
python research/measurement/scripts/analyze.py
```

`collect.py` needs network access. The corpus is committed, so `measure.py` and
`analyze.py` reproduce every published number offline.

## Corpus provenance

| | |
|---|---|
| Source | Wikimedia Commons, `TimedText` namespace |
| License | CC BY-SA 4.0 |
| Redistribution | Permitted with attribution — which is why it was chosen |
| Files | 97 |
| Cues measured | 17,577 |
| Cues analysed | 10,510 after script filtering |

Every file has a sibling `.meta.json` with its Commons page title, URL, licence
and retrieval date. `MANIFEST.json` lists them all.

No subtitle data from sources whose licence does not permit redistribution is
included, and none was downloaded.

## Why the numbers are what they are

Two decisions in `analyze.py` materially affect results and are deliberate:

**Cues are filtered to the language's own script.** Commons files tagged `bn`
were 91% Latin and `ta` 56% Latin — romanised transliterations or mislabelled
uploads. Including them made Bengali look almost Latin-like (1.055 code points
per grapheme, versus 1.595 once filtered).

**Thresholds are swept, not assumed.** Published guidelines give numbers like
20 or 22 CPS without defining what a character is. Rather than adopt one and
declare a unit correct, the analysis reports how often the *verdict changes*
when only the unit changes, across 15–25 CPS.

## Measurement units

Defined and implemented in `scripts/measure.py`, deliberately **not** in
`bhashasub.core` — the study exists to decide whether they belong there.

- **Code points** — `len(text)`.
- **Graphemes** — UAX #29 extended grapheme clusters via `regex`'s `\X`.
- **Aksharas** — graphemes, merging any cluster ending in a virama or ZWJ into
  the next. Known to over-merge in Tamil, where consonant + pulli is already a
  complete written unit; documented in the study's limitations.

Cues are parsed with BhashaSub's own SRT parser, so the study exercises shipped
code rather than a throwaway reimplementation.
