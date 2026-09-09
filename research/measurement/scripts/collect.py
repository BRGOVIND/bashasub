"""Fetch openly-licensed Indic subtitle files from Wikimedia Commons.

Commons stores subtitles as wiki pages in the TimedText namespace (102), named
``TimedText:<video file>.<language>.srt``. Page text is licensed CC BY-SA 4.0
and is redistributable with attribution, which is why this source was chosen
over subtitle sites whose licensing does not permit republication.

Each downloaded file is written next to a .meta.json recording where it came
from, so provenance survives into the study.

    python research/measurement/scripts/collect.py --languages ml ta hi
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "BhashaSub-research/0.1 (https://github.com/BRGOVIND/bashasub)"
LICENSE = "CC BY-SA 4.0"
NAMESPACE = 102

CORPUS = Path(__file__).resolve().parents[1] / "corpus"


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def list_subtitle_pages(languages: set[str], scan_limit: int) -> dict[str, list[str]]:
    """Walk the TimedText namespace and collect pages for the wanted languages."""
    found: dict[str, list[str]] = {language: [] for language in languages}
    continue_from = None
    scanned = 0

    while scanned < scan_limit:
        query = (
            f"{API}?action=query&list=allpages&apnamespace={NAMESPACE}"
            "&aplimit=500&format=json"
        )
        if continue_from:
            query += "&apcontinue=" + urllib.parse.quote(continue_from)

        payload = _get(query)
        for page in payload["query"]["allpages"]:
            title = page["title"]
            scanned += 1
            parts = title.rsplit(".", 2)
            if len(parts) == 3 and parts[2] == "srt" and parts[1] in languages:
                found[parts[1]].append(title)

        continue_from = payload.get("continue", {}).get("apcontinue")
        if not continue_from:
            break
        time.sleep(0.2)

    return found


def fetch_page_text(title: str) -> str | None:
    query = (
        f"{API}?action=query&prop=revisions&rvprop=content&rvslots=main"
        f"&titles={urllib.parse.quote(title)}&format=json"
    )
    pages = _get(query)["query"]["pages"]
    for page in pages.values():
        revisions = page.get("revisions")
        if revisions:
            return revisions[0]["slots"]["main"]["*"]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", nargs="+", default=["ml", "ta", "hi"])
    parser.add_argument("--per-language", type=int, default=12)
    parser.add_argument("--scan-limit", type=int, default=30000)
    args = parser.parse_args()

    CORPUS.mkdir(parents=True, exist_ok=True)
    languages = set(args.languages)

    print(f"scanning TimedText namespace for {sorted(languages)} ...")
    pages = list_subtitle_pages(languages, args.scan_limit)

    manifest = []
    for language, titles in sorted(pages.items()):
        print(f"  {language}: {len(titles)} files available")
        for title in titles[: args.per_language]:
            text = fetch_page_text(title)
            if not text or "-->" not in text:
                continue

            # Windows forbids <>:"/\|?* in filenames; Commons titles contain them.
            safe = re.sub(r'[<>:"/\|?*]', "_", title.replace("TimedText:", ""))
            safe = re.sub(r"\s+", "_", safe).strip("._")[:120]
            path = CORPUS / f"{language}__{safe}"
            path.write_text(text, encoding="utf-8", newline="\n")

            entry = {
                "file": path.name,
                "language": language,
                "source": "Wikimedia Commons",
                "page": title,
                "url": "https://commons.wikimedia.org/wiki/"
                + urllib.parse.quote(title.replace(" ", "_")),
                "license": LICENSE,
                "redistributable": True,
                "retrieved": time.strftime("%Y-%m-%d"),
            }
            manifest.append(entry)
            (CORPUS / f"{path.name}.meta.json").write_text(
                json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            time.sleep(0.2)

    (CORPUS / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"saved {len(manifest)} subtitle files to {CORPUS}")


if __name__ == "__main__":
    main()
