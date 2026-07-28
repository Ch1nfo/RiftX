#!/usr/bin/env python3
"""Create a deterministic RiftX Linux CLI/daemon tarball and SHA-256 file."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=root)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target", default="x86_64-unknown-linux-gnu")
    return parser.parse_args()


def package_release(
    repository_root: Path,
    bin_dir: Path,
    output_dir: Path,
    version: str,
    source_commit: str,
    target: str,
) -> tuple[Path, Path, Path]:
    expected_version = (repository_root / "VERSION").read_text(encoding="utf-8").strip()
    if version != expected_version:
        raise ValueError(
            f"requested version {version!r} does not match VERSION {expected_version!r}"
        )
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise ValueError("source commit must be a lowercase 40-character Git SHA")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_stem = f"riftx-{version}-{target}"
    staging = output_dir / archive_stem
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "bin").mkdir(parents=True)

    for binary in ("riftx", "riftxd"):
        source = bin_dir / binary
        if not source.is_file():
            raise FileNotFoundError(f"release binary is missing: {source}")
        destination = staging / "bin" / binary
        shutil.copy2(source, destination)
        destination.chmod(0o755)

    copies = {
        repository_root / "LICENSE": staging / "LICENSE",
        repository_root / "NOTICE": staging / "NOTICE",
        repository_root / "README.md": staging / "README.md",
        repository_root / "riftx.toml": staging / "riftx.toml.example",
        repository_root / "docs/release/linux.md": staging / "INSTALL.md",
    }
    for source, destination in copies.items():
        if not source.is_file():
            raise FileNotFoundError(f"release document is missing: {source}")
        shutil.copy2(source, destination)
        destination.chmod(0o644)

    metadata = {
        "schema": "riftx.release/v1",
        "version": version,
        "target": target,
        "sourceCommit": source_commit,
        "minimumGlibc": "2.35",
        "containsTelemetry": False,
    }
    (staging / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    archive = output_dir / f"{archive_stem}.tar.gz"
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with archive.open("wb") as raw_stream:
        with gzip.GzipFile(
            fileobj=raw_stream, mode="wb", filename="", mtime=epoch
        ) as gzip_stream:
            with tarfile.open(
                fileobj=gzip_stream, mode="w", format=tarfile.PAX_FORMAT
            ) as tar:
                for path in sorted(staging.rglob("*")):
                    arcname = Path(archive_stem) / path.relative_to(staging)
                    info = tar.gettarinfo(str(path), arcname=str(arcname))
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = epoch
                    if info.isdir():
                        info.mode = 0o755
                        tar.addfile(info)
                    else:
                        executable = bool(path.stat().st_mode & stat.S_IXUSR)
                        info.mode = 0o755 if executable else 0o644
                        with path.open("rb") as source_stream:
                            tar.addfile(info, source_stream)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return staging, archive, checksum


def main() -> int:
    args = parse_args()
    try:
        staging, archive, checksum = package_release(
            args.repository_root.resolve(),
            args.bin_dir.resolve(),
            args.output_dir.resolve(),
            args.version,
            args.source_commit,
            args.target,
        )
    except (OSError, ValueError) as error:
        print(f"Linux release packaging failed: {error}")
        return 1
    print(f"staging={staging}")
    print(f"archive={archive}")
    print(f"checksum={checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
