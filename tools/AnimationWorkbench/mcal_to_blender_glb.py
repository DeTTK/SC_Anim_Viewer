#!/usr/bin/env python3
import argparse
import json
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


def parse_mcal(data: bytes):
    o = 0
    sig = data[o:o + 4]
    if sig != b"MCAL":
        raise ValueError("Not an MCAL file")
    o += 4

    version = struct.unpack_from("<f", data, o)[0]
    o += 4
    bone_count = struct.unpack_from("<I", data, o)[0]
    o += 4
    unknown = data[o]
    o += 1
    clip_count = struct.unpack_from("<i", data, o)[0]
    o += 4

    clips = []
    for i in range(clip_count):
        clip_start = o

        name_len = struct.unpack_from("<H", data, o)[0]
        o += 2
        name = data[o:o + name_len].decode("ascii", errors="ignore")
        o += name_len

        frames = struct.unpack_from("<i", data, o)[0]
        o += 4
        rate = struct.unpack_from("<f", data, o)[0]
        o += 4

        payload_len = frames * bone_count * 14
        payload = data[o:o + payload_len]
        o += payload_len

        clips.append(
            {
                "index": i,
                "name": name,
                "frames": frames,
                "rate": rate,
                "payload": payload,
                "start": clip_start,
            }
        )

    if o != len(data):
        raise ValueError(f"Trailing bytes in MCAL: {len(data) - o}")

    return {
        "version": version,
        "bone_count": bone_count,
        "unknown": unknown,
        "clip_count": clip_count,
        "clips": clips,
    }


def decode_clip_payload_to_transforms(
    payload: bytes, frames: int, bones: int, normalize_quat: bool = False
) -> np.ndarray:
    raw = np.frombuffer(payload, dtype=np.int16).astype(np.float32)
    transforms = raw.reshape(frames, bones, 7) * (1.0 / 32767.0)

    if normalize_quat:
        # Optional normalization for cleaner interpolation in DCC tools.
        q = transforms[:, :, :4]
        norms = np.linalg.norm(q, axis=2, keepdims=True)
        norms = np.where(norms > 1e-8, norms, 1.0)
        transforms[:, :, :4] = q / norms

    return transforms


def adapt_transforms_to_model_bones(transforms: np.ndarray, model_bones: int) -> tuple[np.ndarray, str]:
    """
    Align MCAL transforms with model skeleton size.
    - If model has extra technical bones: keep MCAL tracks and fill extra bones with identity.
    - If model has fewer bones than MCAL: truncate tail tracks.
    """
    src_bones = int(transforms.shape[1])
    if src_bones == int(model_bones):
        return transforms, "exact"

    frames = int(transforms.shape[0])
    out = np.zeros((frames, int(model_bones), 7), dtype=np.float32)
    # Neutral transform for bones that are missing in animation source.
    out[:, :, 3] = 1.0

    n = min(src_bones, int(model_bones))
    out[:, :n, :] = transforms[:, :n, :]
    mode = "expanded_with_identity" if model_bones > src_bones else "truncated_to_model"
    return out, mode


def build_bone_index_map_by_name(source_scene, target_scene, mcal_bones: int) -> tuple[list[tuple[int, int]], int]:
    """
    Build mapping (mcal/source bone index -> target bone index) by bone names.
    """
    source_names = [str(b.name) for b in source_scene.skeleton.bones]
    target_name_to_idx = {str(b.name): int(i) for i, b in enumerate(target_scene.skeleton.bones)}

    pairs: list[tuple[int, int]] = []
    miss = 0
    for src_idx in range(min(int(mcal_bones), len(source_names))):
        name = source_names[src_idx]
        tgt_idx = target_name_to_idx.get(name)
        if tgt_idx is None:
            miss += 1
            continue
        pairs.append((src_idx, int(tgt_idx)))
    return pairs, miss


def remap_transforms_with_index_map(
    transforms: np.ndarray, model_bones: int, index_map: list[tuple[int, int]]
) -> np.ndarray:
    frames = int(transforms.shape[0])
    out = np.zeros((frames, int(model_bones), 7), dtype=np.float32)
    # Identity quaternion for bones without source animation.
    out[:, :, 3] = 1.0
    for src_idx, tgt_idx in index_map:
        if 0 <= int(src_idx) < transforms.shape[1] and 0 <= int(tgt_idx) < int(model_bones):
            out[:, int(tgt_idx), :] = transforms[:, int(src_idx), :]
    return out


def load_model_content(model_path: Path):
    from scfile import formats
    from scfile.core import UserOptions

    opts = UserOptions(parse_skeleton=True, parse_animation=False)
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


def build_bones_report(scene, clip_name: str, output_path: Path) -> None:
    rows = []
    for b in scene.skeleton.bones:
        parent = b.parent_id
        parent_name = "ROOT" if parent < 0 else scene.skeleton.bones[parent].name
        rows.append(
            {
                "id": b.id,
                "name": b.name,
                "parent_id": parent,
                "parent_name": parent_name,
                "clip": clip_name,
            }
        )
    output_path.write_text(json.dumps(rows, ensure_ascii=True, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Export MCAL clip to Blender-ready GLB using a matching MCSB/MCVD skeleton.")
    ap.add_argument("--model", required=True, help="Path to .mcsb/.mcsa/.mcvd model (with skeleton and mesh)")
    ap.add_argument("--mcal", required=True, help="Path to .mcal animation file")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--clip-name")
    g.add_argument("--clip-index", type=int)
    ap.add_argument("--out", required=True, help="Output .glb path")
    ap.add_argument("--bones-report", help="Optional output .json with bone hierarchy report")
    ap.add_argument("--scfile-root", help="Path to sc-file-master root (optional)")
    ap.add_argument(
        "--source-model",
        help="Optional source skeleton model (.mcsb/.mcsa/.mcvd) matching MCAL bone order; "
        "used to remap tracks to target model by bone names.",
    )
    ap.add_argument(
        "--normalize-quat",
        action="store_true",
        help="Normalize quaternion keys before GLB export (not byte-exact round-trip safe).",
    )
    args = ap.parse_args()

    add_scfile_to_path(args.scfile_root)
    from scfile.core import UserOptions
    from scfile.formats.glb.encoder import GlbEncoder
    from scfile.structures.animation import AnimationClip

    model_path = Path(args.model)
    mcal_path = Path(args.mcal)
    out_path = Path(args.out)

    scene_data = load_model_content(model_path)
    scene = scene_data.scene

    mcal = parse_mcal(mcal_path.read_bytes())

    if args.clip_index is not None:
        if args.clip_index < 0 or args.clip_index >= mcal["clip_count"]:
            raise ValueError("clip-index is out of range")
        clip_src = mcal["clips"][args.clip_index]
    else:
        matches = [c for c in mcal["clips"] if c["name"] == args.clip_name]
        if not matches:
            raise ValueError("clip-name not found in MCAL")
        clip_src = matches[0]

    transforms = decode_clip_payload_to_transforms(
        payload=clip_src["payload"],
        frames=clip_src["frames"],
        bones=mcal["bone_count"],
        normalize_quat=args.normalize_quat,
    )

    remap_mode = "exact"
    remap_info = ""
    if args.source_model:
        source_model_path = Path(args.source_model)
        if not source_model_path.exists():
            raise FileNotFoundError(f"source-model not found: {source_model_path}")
        source_scene_data = load_model_content(source_model_path)
        source_scene = source_scene_data.scene
        index_map, misses = build_bone_index_map_by_name(
            source_scene=source_scene,
            target_scene=scene,
            mcal_bones=int(mcal["bone_count"]),
        )
        if index_map:
            transforms = remap_transforms_with_index_map(
                transforms=transforms,
                model_bones=int(scene.count.bones),
                index_map=index_map,
            )
            remap_mode = "mapped_by_name"
            remap_info = f" mapped={len(index_map)} missing_source_names={misses}"
        else:
            transforms, remap_mode = adapt_transforms_to_model_bones(transforms, int(scene.count.bones))
            remap_mode = f"{remap_mode}_fallback_no_name_matches"
    else:
        transforms, remap_mode = adapt_transforms_to_model_bones(transforms, int(scene.count.bones))

    clip = AnimationClip()
    clip.name = clip_src["name"]
    clip.frames = clip_src["frames"]
    clip.rate = clip_src["rate"]
    clip.transforms = transforms

    scene.count.clips = 1
    scene.animation.clips = [clip]

    # Required so GLB encoder writes bones and animation channels.
    scene_data.flags[0] = True  # Flag.SKELETON

    out_path.parent.mkdir(parents=True, exist_ok=True)
    enc_opts = UserOptions(parse_skeleton=True, parse_animation=True)
    enc = GlbEncoder(scene_data, options=enc_opts)
    enc.encode().save(str(out_path))

    if args.bones_report:
        report_path = Path(args.bones_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        build_bones_report(scene, clip.name, report_path)

    print(
        f"Exported GLB: {out_path}\n"
        f"clip={clip.name} frames={clip.frames} rate={clip.rate} "
        f"mcal_bones={mcal['bone_count']} model_bones={scene.count.bones} "
        f"remap={remap_mode}{remap_info} meshes={scene.count.meshes}"
    )


if __name__ == "__main__":
    main()
