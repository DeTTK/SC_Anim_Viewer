from __future__ import annotations

import argparse
import json
import re
import string
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class McalStringEntry:
    offset: int
    value: str
    frame_count: int | None = None
    sample_step: float | None = None


@dataclass
class McalReport:
    path: str
    size: int
    kind: str
    magic: str
    version: float | None
    strings: list[McalStringEntry]
    query_hits: list[str]
    matches_query: bool


def _is_printable_ratio_ok(text: str, threshold: float = 0.9) -> bool:
    if not text:
        return False
    printable = sum(ch in string.printable and ch not in "\r\n\t\x0b\x0c" for ch in text)
    return (printable / len(text)) >= threshold


def _is_candidate_text(text: str) -> bool:
    if len(text) < 3:
        return False
    if not _is_printable_ratio_ok(text):
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    return True


def extract_length_prefixed_strings(
    blob: bytes, max_strings: int = 64, min_len: int = 3, max_len: int = 200, unaligned: bool = False
) -> list[McalStringEntry]:
    entries: list[McalStringEntry] = []
    seen_values: set[tuple[int, str]] = set()
    step = 1 if unaligned else 4
    limit = len(blob) - 12

    for offset in range(0, max(0, limit), step):
        length = struct.unpack_from("<I", blob, offset)[0]
        if length < min_len or length > max_len:
            continue

        end = offset + 4 + length
        if end > len(blob):
            continue

        chunk = blob[offset + 4 : end]
        if b"\x00" in chunk:
            continue

        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue

        if not _is_candidate_text(text):
            continue

        dedup_key = (offset, text)
        if dedup_key in seen_values:
            continue
        seen_values.add(dedup_key)

        frame_count = None
        sample_step = None
        if end + 8 <= len(blob):
            frames = struct.unpack_from("<I", blob, end)[0]
            step_value = struct.unpack_from("<f", blob, end + 4)[0]
            if 0 < frames < 2_000_000:
                frame_count = frames
            if 0.0 < step_value < 10.0:
                sample_step = step_value

        entries.append(
            McalStringEntry(
                offset=offset,
                value=text,
                frame_count=frame_count,
                sample_step=sample_step,
            )
        )
        if len(entries) >= max_strings:
            break

    return entries


def extract_ascii_strings(blob: bytes, max_strings: int = 64, min_len: int = 6, max_len: int = 200) -> list[McalStringEntry]:
    entries: list[McalStringEntry] = []
    pattern = rb"[\x20-\x7E]{" + str(min_len).encode("ascii") + b"," + str(max_len).encode("ascii") + b"}"

    for match in re.finditer(pattern, blob):
        text = match.group().decode("ascii", errors="ignore")
        if not _is_candidate_text(text):
            continue

        offset = match.start()
        end = match.end()
        frame_count = None
        sample_step = None
        if end + 8 <= len(blob):
            frames = struct.unpack_from("<I", blob, end)[0]
            step_value = struct.unpack_from("<f", blob, end + 4)[0]
            if 0 < frames < 2_000_000:
                frame_count = frames
            if 0.0 < step_value < 10.0:
                sample_step = step_value

        entries.append(
            McalStringEntry(
                offset=offset,
                value=text,
                frame_count=frame_count,
                sample_step=sample_step,
            )
        )
        if len(entries) >= max_strings:
            break

    return entries


def extract_candidate_strings(blob: bytes, max_strings: int = 64, unaligned: bool = False) -> list[McalStringEntry]:
    prefixed = extract_length_prefixed_strings(blob, max_strings=max_strings, unaligned=unaligned)
    ascii_runs = extract_ascii_strings(blob, max_strings=max_strings)

    merged: list[McalStringEntry] = []
    seen: set[tuple[int, str]] = set()
    for row in sorted(prefixed + ascii_runs, key=lambda item: item.offset):
        key = (row.offset, row.value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= max_strings:
            break
    return merged


def inspect_mcal_file(path: Path, query: str | None = None, max_strings: int = 64, unaligned: bool = False) -> McalReport:
    blob = path.read_bytes()
    magic = blob[:4].decode("ascii", errors="replace")
    version = struct.unpack_from("<f", blob, 4)[0] if len(blob) >= 8 else None
    strings = extract_candidate_strings(blob, max_strings=max_strings, unaligned=unaligned)
    kind = path.suffix.lower().lstrip(".") or "unknown"

    query_l = (query or "").lower().strip()
    query_hits: list[str] = []
    matches_query = True
    if query_l:
        all_ascii = extract_ascii_strings(blob, max_strings=20_000)
        for row in all_ascii:
            if query_l in row.value.lower():
                query_hits.append(row.value)
                if len(query_hits) >= 20:
                    break
        matches_query = (
            query_l in path.as_posix().lower()
            or any(query_l in row.value.lower() for row in strings)
            or bool(query_hits)
        )

    return McalReport(
        path=path.as_posix(),
        size=len(blob),
        kind=kind,
        magic=magic,
        version=version,
        strings=strings,
        query_hits=query_hits,
        matches_query=matches_query,
    )


def iter_mcal_files(source: Path, recursive: bool) -> Iterable[Path]:
    if source.is_file():
        if source.suffix.lower() in {".mcal", ".mcvd"}:
            yield source
        return

    patterns = ("**/*.mcal", "**/*.mcvd") if recursive else ("*.mcal", "*.mcvd")
    for pattern in patterns:
        yield from source.glob(pattern)


def _print_report(report: McalReport, details: bool) -> None:
    print(f"{report.path}")
    print(
        f"  kind={report.kind} size={report.size} magic={report.magic} "
        f"version={report.version if report.version is not None else 'unknown'} "
        f"strings={len(report.strings)}"
    )
    if details:
        for row in report.strings:
            extras: list[str] = []
            if row.frame_count is not None:
                extras.append(f"frames={row.frame_count}")
            if row.sample_step is not None:
                extras.append(f"step={row.sample_step:.6f}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            print(f"    @{row.offset:>8}  {row.value}{suffix}")
    if report.query_hits:
        print("  query hits:")
        for hit in report.query_hits:
            print(f"    - {hit}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcal-inspect",
        description="Inspect MCAL/MCVD files and extract likely animation string blocks.",
    )
    parser.add_argument("path", nargs="?", default=".", help="MCAL/MCVD file or folder path.")
    parser.add_argument("-r", "--recursive", action="store_true", help="Scan directories recursively.")
    parser.add_argument("-q", "--query", help="Only keep files that contain this text in path or extracted strings.")
    parser.add_argument("--max-strings", type=int, default=64, help="Maximum extracted string blocks per file.")
    parser.add_argument("--unaligned", action="store_true", help="Scan every byte offset (slower, more false positives).")
    parser.add_argument("--details", action="store_true", help="Print extracted strings for each file.")
    parser.add_argument("--json", dest="json_output", help="Write output report as JSON to this path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.path).expanduser()
    files = sorted(iter_mcal_files(source, recursive=args.recursive))
    if not files:
        print("No .mcal/.mcvd files found.")
        return 1

    reports = [
        inspect_mcal_file(path, query=args.query, max_strings=max(1, args.max_strings), unaligned=args.unaligned)
        for path in files
    ]
    reports = [row for row in reports if row.matches_query]
    if not reports:
        print("No matches.")
        return 2

    for report in reports:
        _print_report(report, details=args.details)

    total_size = sum(row.size for row in reports)
    print(f"\nMatched files: {len(reports)} | Total size: {total_size} bytes")

    if args.json_output:
        payload = [asdict(row) for row in reports]
        out_path = Path(args.json_output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON saved to: {out_path.as_posix()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
