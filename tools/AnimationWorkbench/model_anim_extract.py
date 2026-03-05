#!/usr/bin/env python3
import argparse
import struct
import sys
from pathlib import Path

import numpy as np


def resolve_scfile_root(explicit_root: str | None) -> Path:
    candidates: list[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root).resolve())

    env_root = __import__("os").environ.get("SCFILE_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root).resolve())

    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            here / "sc-file-master",
            here / ".vendor" / "sc-file-master",
            here / "vendor" / "sc-file-master",
        ]
    )

    for c in candidates:
        if c.exists():
            return c

    checked = "\n".join(str(c) for c in candidates)
    raise FileNotFoundError(f"sc-file-master not found. Checked:\n{checked}")


def add_scfile_to_path(explicit_root: str | None) -> None:
    scfile_root = resolve_scfile_root(explicit_root)
    sys.path.insert(0, str(scfile_root))


def load_model_content(model_path: Path):
    from scfile import formats
    from scfile.core import UserOptions

    opts = UserOptions(parse_skeleton=True, parse_animation=True)
    suffix = model_path.suffix.lower()

    if suffix == ".mcsb":
        dec = formats.mcsb.McsbDecoder(str(model_path), options=opts)
    elif suffix in (".mcsa", ".mcvd"):
        dec = formats.mcsa.McsaDecoder(str(model_path), options=opts)
    else:
        raise ValueError("Model must be .mcsb, .mcsa or .mcvd")

    with dec:
        data = dec.decode()
    return data


def build_single_clip_mcal(clip_name: str, transforms: np.ndarray, frame_rate: float, version: float = 1.0, unknown: int = 0) -> bytes:
    if transforms.ndim != 3 or transforms.shape[2] != 7:
        raise ValueError("Unexpected transforms shape; expected [frames, bones, 7]")

    frame_count, bone_count, _ = transforms.shape
    q = np.clip(np.rint(transforms * 32767.0), -32768, 32767).astype(np.int16)

    clip_name_b = clip_name.encode("ascii", errors="ignore")
    if not clip_name_b:
        clip_name_b = b"clip"
    if len(clip_name_b) > 0xFFFF:
        raise ValueError("Clip name is too long for MCAL")

    out = bytearray()
    out.extend(b"MCAL")
    out.extend(struct.pack("<fI", float(version), int(bone_count)))
    out.extend(bytes([int(unknown)]))
    out.extend(struct.pack("<i", 1))

    out.extend(struct.pack("<H", len(clip_name_b)))
    out.extend(clip_name_b)
    out.extend(struct.pack("<if", int(frame_count), float(frame_rate)))
    out.extend(q.tobytes(order="C"))
    return bytes(out)


def find_clip(scene, name: str | None, index: int | None):
    clips = scene.animation.clips
    if not clips:
        raise ValueError("No built-in animation clips in model")
    if index is not None:
        if index < 0 or index >= len(clips):
            raise ValueError("clip-index out of range")
        return index, clips[index]
    for i, clip in enumerate(clips):
        if clip.name == name:
            return i, clip
    raise ValueError(f"clip-name not found: {name}")


def cmd_list(args: argparse.Namespace) -> None:
    add_scfile_to_path(args.scfile_root)
    model = Path(args.model).resolve()
    scene = load_model_content(model).scene
    clips = scene.animation.clips
    print(
        f"model={model}\n"
        f"bones={scene.count.bones} meshes={scene.count.meshes} clips={len(clips)}"
    )
    for i, clip in enumerate(clips):
        print(f"{i:4d}  {clip.name:<64} frames={clip.frames:<5d} dt={clip.rate:.9f}")


def cmd_extract_one(args: argparse.Namespace) -> None:
    add_scfile_to_path(args.scfile_root)
    model = Path(args.model).resolve()
    data = load_model_content(model)
    scene = data.scene
    clip_index, clip = find_clip(scene, args.name, args.index)

    out = Path(args.output).resolve() if args.output else Path(f"{clip.name}.from_model.mcal").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    mcal_data = build_single_clip_mcal(
        clip_name=clip.name or f"clip_{clip_index}",
        transforms=clip.transforms,
        frame_rate=clip.rate,
    )
    out.write_bytes(mcal_data)
    print(
        f"Wrote {out}\n"
        f"model={model}\n"
        f"clip_index={clip_index} clip_name={clip.name} frames={clip.frames} dt={clip.rate:.9f} bones={scene.count.bones}"
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="List/extract built-in model animation clips (.mcsb/.mcsa/.mcvd) to MCAL.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="List built-in clips from model")
    p.add_argument("--model", required=True, help="Path to model (.mcsb/.mcsa/.mcvd)")
    p.add_argument("--scfile-root", help="Path to sc-file-master root (optional)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("extract-one", help="Extract one built-in model clip to single-clip MCAL")
    p.add_argument("--model", required=True, help="Path to model (.mcsb/.mcsa/.mcvd)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--name", help="Exact built-in clip name")
    g.add_argument("--index", type=int, help="Built-in clip index")
    p.add_argument("--output", help="Output .mcal path")
    p.add_argument("--scfile-root", help="Path to sc-file-master root (optional)")
    p.set_defaults(func=cmd_extract_one)
    return ap


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
