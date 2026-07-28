#!/usr/bin/env python3
"""Remove RiftX's large, reproducible local Rust build outputs."""

import argparse
from pathlib import Path
import shutil

TARGET_DIRECTORIES = (
    Path("codex-rs/target"),
    Path("apps/desktop/src-tauri/target"),
)
SIDECAR_GLOB = "apps/desktop/src-tauri/binaries/riftxd-*"


def path_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    return sum(
        child.lstat().st_size
        for child in path.rglob("*")
        if not child.is_symlink() and child.is_file()
    )


def clean(root: Path, dry_run: bool = False) -> tuple[list[Path], int]:
    root = root.resolve()
    candidates = [root / relative for relative in TARGET_DIRECTORIES]
    candidates.extend(sorted(root.glob(SIDECAR_GLOB)))
    removed: list[Path] = []
    reclaimed = 0
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        reclaimed += path_size(path)
        removed.append(path.relative_to(root))
        if dry_run:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    return removed, reclaimed


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    removed, reclaimed = clean(args.root, args.dry_run)
    action = "would remove" if args.dry_run else "removed"
    if removed:
        print(f"{action} {len(removed)} generated paths ({human_size(reclaimed)}):")
        for path in removed:
            print(f"  {path}")
    else:
        print("no RiftX Rust build outputs to clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
