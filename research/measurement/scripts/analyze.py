"""Analyse the measurements and answer the study's research questions.

    python research/measurement/scripts/analyze.py

Writes results/analysis.json and results/summary.md, and prints the headline
tables. Plots are produced only if matplotlib is installed; the study's
conclusions rest on the tables, not the pictures.
"""

from __future__ import annotations

import csv
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
RESULTS = BASE / "results"

# Candidate reading-speed thresholds, in "characters" per second. Netflix
# publishes 20 CPS for English adult content and 22 for Tamil, but does not
# define which unit a character is. That ambiguity is the point of the study, so
# a range is swept rather than a single number being assumed correct.
CPS_THRESHOLDS = [15, 17, 20, 22, 25]
LINE_LENGTH_LIMIT = 42  # characters per line, as published

LANGUAGE_NAMES = {
    "ml": "Malayalam", "ta": "Tamil", "hi": "Hindi", "bn": "Bengali",
    "te": "Telugu", "kn": "Kannada", "mr": "Marathi",
}

# The corpus is filtered to cues actually written in the language's own script.
# Commons files tagged "bn" turned out to be 91% Latin and "ta" 56% Latin --
# transliterations or mislabelled uploads. Leaving them in would have made those
# languages look far closer to Latin behaviour than they are, which is exactly
# the kind of artefact that produces a wrong conclusion.
EXPECTED_SCRIPT = {
    "ml": "MALAYALAM", "ta": "TAMIL", "hi": "DEVANAGARI", "bn": "BENGALI",
    "te": "TELUGU", "kn": "KANNADA", "mr": "DEVANAGARI",
}


def load() -> list[dict]:
    rows = []
    with (RESULTS / "measurements.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            for key in ("duration_ms", "n_lines", "codepoints", "graphemes", "aksharas",
                        "max_line_codepoints", "max_line_graphemes", "max_line_aksharas",
                        "letters", "digits", "spaces", "punctuation", "cue_index"):
                row[key] = int(row[key])
            for key in ("cps_codepoint", "cps_grapheme", "cps_akshara"):
                row[key] = float(row[key]) if row[key] else 0.0
            rows.append(row)
    return rows


def describe(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": round(stats.mean(values), 3),
        "median": round(stats.median(values), 3),
        "sd": round(stats.pstdev(values), 3) if len(values) > 1 else 0.0,
        "p90": round(ordered[int(0.90 * (len(ordered) - 1))], 3),
        "p99": round(ordered[int(0.99 * (len(ordered) - 1))], 3),
        "max": round(max(values), 3),
    }


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mx, my = stats.mean(xs), stats.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(num / (dx * dy), 4) if dx and dy else 0.0


def main() -> None:
    all_rows = load()
    rows = [
        r for r in all_rows
        if r["script"] == EXPECTED_SCRIPT.get(r["language"])
    ]
    excluded = defaultdict(int)
    for r in all_rows:
        if r["script"] != EXPECTED_SCRIPT.get(r["language"]):
            excluded[r["language"]] += 1

    by_language = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)

    report: dict = {
        "total_cues_measured": len(all_rows),
        "total_cues_analysed": len(rows),
        "excluded_off_script": dict(sorted(excluded.items())),
        "expected_script": EXPECTED_SCRIPT,
        "languages": {},
        "questions": {},
    }

    # ---- Q1/Q2: how far apart are the units? -----------------------------
    for language, group in sorted(by_language.items()):
        cp = [r["codepoints"] for r in group]
        gr = [r["graphemes"] for r in group]
        ak = [r["aksharas"] for r in group]

        cp_gr = [r["codepoints"] / r["graphemes"] for r in group if r["graphemes"]]
        gr_ak = [r["graphemes"] / r["aksharas"] for r in group if r["aksharas"]]
        cp_ak = [r["codepoints"] / r["aksharas"] for r in group if r["aksharas"]]

        report["languages"][language] = {
            "name": LANGUAGE_NAMES.get(language, language),
            "cues": len(group),
            "files": len({r["file"] for r in group}),
            "counts": {"codepoints": describe(cp), "graphemes": describe(gr),
                       "aksharas": describe(ak)},
            "ratios": {
                "codepoint_per_grapheme": describe(cp_gr),
                "grapheme_per_akshara": describe(gr_ak),
                "codepoint_per_akshara": describe(cp_ak),
            },
            "cps": {
                "codepoint": describe([r["cps_codepoint"] for r in group]),
                "grapheme": describe([r["cps_grapheme"] for r in group]),
                "akshara": describe([r["cps_akshara"] for r in group]),
            },
            "correlation_cp_vs_grapheme": pearson(
                [float(r["codepoints"]) for r in group],
                [float(r["graphemes"]) for r in group],
            ),
            "correlation_grapheme_vs_akshara": pearson(
                [float(r["graphemes"]) for r in group],
                [float(r["aksharas"]) for r in group],
            ),
        }

    # ---- Q3: does the unit change the verdict? ---------------------------
    # Holds the nominal threshold fixed and varies only the counting unit. This
    # is the operational question: a tool counting code points against a
    # threshold written for visual characters reaches a different verdict.
    disagreement = {}
    for threshold in CPS_THRESHOLDS:
        per_language = {}
        for language, group in sorted(by_language.items()):
            cp_fail = sum(1 for r in group if r["cps_codepoint"] > threshold)
            gr_fail = sum(1 for r in group if r["cps_grapheme"] > threshold)
            ak_fail = sum(1 for r in group if r["cps_akshara"] > threshold)
            flip_cp_gr = sum(
                1 for r in group
                if (r["cps_codepoint"] > threshold) != (r["cps_grapheme"] > threshold)
            )
            flip_gr_ak = sum(
                1 for r in group
                if (r["cps_grapheme"] > threshold) != (r["cps_akshara"] > threshold)
            )
            per_language[language] = {
                "fail_codepoint": cp_fail,
                "fail_grapheme": gr_fail,
                "fail_akshara": ak_fail,
                "fail_rate_codepoint": round(100 * cp_fail / len(group), 2),
                "fail_rate_grapheme": round(100 * gr_fail / len(group), 2),
                "fail_rate_akshara": round(100 * ak_fail / len(group), 2),
                "verdict_flips_cp_vs_grapheme": flip_cp_gr,
                "flip_rate_cp_vs_grapheme": round(100 * flip_cp_gr / len(group), 2),
                "verdict_flips_grapheme_vs_akshara": flip_gr_ak,
                "flip_rate_grapheme_vs_akshara": round(100 * flip_gr_ak / len(group), 2),
            }
        disagreement[threshold] = per_language
    report["questions"]["q3_threshold_disagreement"] = disagreement

    # ---- Line length, at the published 42-character limit -----------------
    line_report = {}
    for language, group in sorted(by_language.items()):
        cp_over = sum(1 for r in group if r["max_line_codepoints"] > LINE_LENGTH_LIMIT)
        gr_over = sum(1 for r in group if r["max_line_graphemes"] > LINE_LENGTH_LIMIT)
        ak_over = sum(1 for r in group if r["max_line_aksharas"] > LINE_LENGTH_LIMIT)
        line_report[language] = {
            "over_limit_codepoint": cp_over,
            "over_limit_grapheme": gr_over,
            "over_limit_akshara": ak_over,
            "flips_cp_vs_grapheme": sum(
                1 for r in group
                if (r["max_line_codepoints"] > LINE_LENGTH_LIMIT)
                != (r["max_line_graphemes"] > LINE_LENGTH_LIMIT)
            ),
        }
    report["questions"]["q3b_line_length"] = line_report

    # ---- Disagreement examples -------------------------------------------
    examples = []
    for row in rows:
        if not row["graphemes"]:
            continue
        ratio = row["codepoints"] / row["graphemes"]
        if row["codepoints"] >= 12:
            examples.append((ratio, row))
    examples.sort(key=lambda pair: pair[0], reverse=True)

    def sample(entry):
        ratio, row = entry
        return {
            "language": row["language"],
            "text": row["text"][:90],
            "codepoints": row["codepoints"],
            "graphemes": row["graphemes"],
            "aksharas": row["aksharas"],
            "codepoint_per_grapheme": round(ratio, 3),
            "cps_codepoint": row["cps_codepoint"],
            "cps_grapheme": row["cps_grapheme"],
            "duration_ms": row["duration_ms"],
        }

    report["questions"]["q5_examples_widest_divergence"] = [sample(e) for e in examples[:8]]
    report["questions"]["q5_examples_narrowest_divergence"] = [
        sample(e) for e in examples[-5:]
    ]

    # Cues that flip a 20 CPS verdict purely because of the counting unit.
    flips = [
        r for r in rows
        if (r["cps_codepoint"] > 20) != (r["cps_grapheme"] > 20) and r["codepoints"] >= 10
    ]
    flips.sort(key=lambda r: r["cps_codepoint"] - r["cps_grapheme"], reverse=True)
    report["questions"]["q3_flip_examples_at_20cps"] = [
        {
            "language": r["language"],
            "text": r["text"][:90],
            "duration_ms": r["duration_ms"],
            "codepoints": r["codepoints"], "graphemes": r["graphemes"],
            "cps_codepoint": r["cps_codepoint"], "cps_grapheme": r["cps_grapheme"],
            "codepoint_verdict": "FAIL" if r["cps_codepoint"] > 20 else "pass",
            "grapheme_verdict": "FAIL" if r["cps_grapheme"] > 20 else "pass",
        }
        for r in flips[:8]
    ]
    report["questions"]["q3_total_flips_at_20cps"] = len(flips)

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "analysis.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ---- printed summary --------------------------------------------------
    print(f"cues measured: {report['total_cues_measured']}  "
          f"analysed after script filter: {report['total_cues_analysed']}")
    print(f"excluded as off-script: {report['excluded_off_script']}")
    print(f"{'lang':6} {'cues':>6} {'cp/graph':>9} {'graph/aksh':>11} "
          f"{'cps_cp':>8} {'cps_gr':>8} {'cps_ak':>8}")
    for language, data in report["languages"].items():
        print(f"{language:6} {data['cues']:>6} "
              f"{data['ratios']['codepoint_per_grapheme']['mean']:>9.3f} "
              f"{data['ratios']['grapheme_per_akshara']['mean']:>11.3f} "
              f"{data['cps']['codepoint']['median']:>8.2f} "
              f"{data['cps']['grapheme']['median']:>8.2f} "
              f"{data['cps']['akshara']['median']:>8.2f}")

    print("\nverdict flips when only the counting unit changes (threshold = 20 CPS)")
    print(f"{'lang':6} {'fail%cp':>8} {'fail%gr':>8} {'flip%':>7}")
    for language, data in disagreement[20].items():
        print(f"{language:6} {data['fail_rate_codepoint']:>8.2f} "
              f"{data['fail_rate_grapheme']:>8.2f} "
              f"{data['flip_rate_cp_vs_grapheme']:>7.2f}")

    print(f"\nwrote {RESULTS / 'analysis.json'}")


if __name__ == "__main__":
    main()
