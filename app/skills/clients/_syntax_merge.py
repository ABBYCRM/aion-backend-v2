#!/usr/bin/env python3
"""Merge one generated scenario TXT file with its paired syntax TXT file by ID."""
from pathlib import Path
import argparse, json

def scenario_rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            ident, sep, rest = line.partition(" | ")
            if not sep or len(ident) != 6 or not ident.isdigit():
                raise ValueError(f"Invalid scenario row: {line[:120]!r}")
            yield ident, rest.rstrip("\n")

def syntax_rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                raise ValueError(f"Invalid syntax row: {line[:120]!r}")
            ident, construct, encoded = parts
            json.loads(encoded)  # Validate JSON-escaped snippet.
            yield ident, construct, encoded

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario_file", type=Path)
    parser.add_argument("syntax_file", type=Path)
    parser.add_argument("output_file", type=Path)
    args = parser.parse_args()
    scenarios = scenario_rows(args.scenario_file)
    syntax = syntax_rows(args.syntax_file)
    count = 0
    with args.output_file.open("w", encoding="utf-8", newline="\n") as out:
        out.write("# ID<TAB>scenario<TAB>construct<TAB>JSON-escaped syntax snippet\n")
        for scenario, syntax_row in zip(scenarios, syntax, strict=True):
            sid, scenario_text = scenario
            xid, construct, encoded = syntax_row
            if sid != xid:
                raise ValueError(f"ID mismatch: scenario {sid}, syntax {xid}")
            out.write(f"{sid}\t{scenario_text}\t{construct}\t{encoded}\n")
            count += 1
    if count != 100_000:
        raise ValueError(f"Expected 100,000 merged rows, got {count}")
    print(f"Merged {count:,} rows into {args.output_file}")

if __name__ == "__main__":
    main()
