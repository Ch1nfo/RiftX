import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify-version-sync.py")
SPEC = importlib.util.spec_from_file_location("verify_version_sync", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyVersionSyncTests(unittest.TestCase):
    def test_repository_versions_match(self) -> None:
        root = Path(__file__).resolve().parents[2]
        versions = MODULE.load_versions(root)
        self.assertEqual(set(versions.values()), {"1.0.0"})

    def test_detects_binary_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            self._write_json(root / "apps/desktop/package.json", {"version": "1.0.0"})
            self._write_json(
                root / "apps/desktop/src-tauri/tauri.conf.json",
                {"version": "1.0.0"},
            )
            self._write_cargo(root / "apps/desktop/src-tauri/Cargo.toml", "1.0.0")
            self._write_cargo(root / "codex-rs/riftx-cli/Cargo.toml", "0.8.0")
            self._write_cargo(root / "codex-rs/riftx-gateway/Cargo.toml", "1.0.0")

            versions = MODULE.load_versions(root)
            self.assertEqual(versions["codex-rs/riftx-cli/Cargo.toml"], "0.8.0")
            self.assertNotEqual(set(versions.values()), {"1.0.0"})

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _write_cargo(path: Path, version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'[package]\nname = "fixture"\nversion = "{version}"\n',
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
