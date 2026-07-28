import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("package-linux-release.py")
SPEC = importlib.util.spec_from_file_location("package_linux_release", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PackageLinuxReleaseTests(unittest.TestCase):
    def test_packages_required_files_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            binaries = root / "binaries"
            output = root / "output"
            (repository / "docs/release").mkdir(parents=True)
            binaries.mkdir()
            (repository / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            for name in ("LICENSE", "NOTICE", "README.md", "riftx.toml"):
                (repository / name).write_text(f"{name}\n", encoding="utf-8")
            (repository / "docs/release/linux.md").write_text(
                "install\n", encoding="utf-8"
            )
            for binary in ("riftx", "riftxd"):
                path = binaries / binary
                path.write_bytes(binary.encode())
                path.chmod(0o755)

            staging, archive, checksum = MODULE.package_release(
                repository,
                binaries,
                output,
                "1.0.0",
                "a" * 40,
                "x86_64-unknown-linux-gnu",
            )

            self.assertTrue(staging.is_dir())
            expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(
                checksum.read_text(encoding="utf-8"),
                f"{expected_digest}  {archive.name}\n",
            )
            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                prefix = "riftx-1.0.0-x86_64-unknown-linux-gnu"
                self.assertIn(f"{prefix}/bin/riftx", names)
                self.assertIn(f"{prefix}/bin/riftxd", names)
                metadata = json.load(tar.extractfile(f"{prefix}/BUILD-METADATA.json"))
                self.assertEqual(metadata["sourceCommit"], "a" * 40)
                self.assertEqual(metadata["minimumGlibc"], "2.35")

    def test_rejects_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                MODULE.package_release(
                    root,
                    root,
                    root / "output",
                    "1.0.1",
                    "b" * 40,
                    "x86_64-unknown-linux-gnu",
                )


if __name__ == "__main__":
    unittest.main()
