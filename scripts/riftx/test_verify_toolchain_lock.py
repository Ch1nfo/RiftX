import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify-toolchain-lock.py")
SPEC = importlib.util.spec_from_file_location("verify_toolchain_lock", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyToolchainLockTests(unittest.TestCase):
    def test_repository_pins_match_release_policy(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(MODULE.validate(root), [])

    def test_detects_tauri_version_drift(self) -> None:
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                ".node-version",
                "package.json",
                ".github/agent-environment.yml",
                ".github/workflows/ci.yml",
                ".github/workflows/release.yml",
                "codex-rs/rust-toolchain.toml",
                "apps/desktop/package.json",
                "apps/desktop/src-tauri/Cargo.toml",
            ):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, destination)

            package_path = root / "apps/desktop/package.json"
            package_path.write_text(
                package_path.read_text(encoding="utf-8").replace(
                    '"@tauri-apps/api": "2.11.1"',
                    '"@tauri-apps/api": "^2.11.1"',
                ),
                encoding="utf-8",
            )

            errors = MODULE.validate(root)
            self.assertTrue(any("@tauri-apps/api" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
