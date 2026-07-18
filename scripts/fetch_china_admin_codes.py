#!/usr/bin/env python3
"""Create a reproducible county-and-above code snapshot from the MCA table."""

from __future__ import annotations

import argparse
import csv
import html
import re
import urllib.request
from pathlib import Path


DEFAULT_URL = "https://www.mca.gov.cn/mzsj/xzqh/2023/202301xzqh.html"


def parse_rows(document: str) -> list[tuple[str, str]]:
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", document, flags=re.IGNORECASE | re.DOTALL)
    values: list[str] = []
    for cell in cells:
        text = re.sub(r"<[^>]+>", "", cell)
        text = html.unescape(text).replace("\u00a0", " ").strip()
        if text:
            values.append(text)

    rows: list[tuple[str, str]] = []
    for index, value in enumerate(values[:-1]):
        if re.fullmatch(r"\d{6}", value):
            name = values[index + 1].strip()
            if name and not re.fullmatch(r"\d{6}", name):
                rows.append((value, name))

    unique = dict(rows)
    return sorted(unique.items())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request = urllib.request.Request(args.url, headers={"User-Agent": "RunLogGeoResources/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        document = response.read().decode("utf-8", errors="replace")

    rows = parse_rows(document)
    if len(rows) < 2_500:
        raise RuntimeError(f"MCA table parse returned only {len(rows)} rows")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["code", "name"])
        writer.writerows(rows)
    print(f"wrote {len(rows)} administrative codes to {args.output}")


if __name__ == "__main__":
    main()
