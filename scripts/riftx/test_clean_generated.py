import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("clean-generated.py")
SPEC = importlib.util.spec_from_file_location("clean_generated", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CleanGeneratedTests(unittest.TestCase):
    def test_removes_targets_and_sidecars_but_preserves_gitkeep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in MODULE.TARGET_DIRECTORIES:
                target = root / relative
                target.mkdir(parents=True)
                (target / "large-file").write_bytes(b"x" * 32)
            binaries = root / "apps/desktop/src-tauri/binaries"
            binaries.mkdir(parents=True)
            gitkeep = binaries / ".gitkeep"
            gitkeep.write_text("", encoding="utf-8")
            sidecar = binaries / "riftxd-x86_64-unknown-linux-gnu"
            sidecar.write_bytes(b"sidecar")

            removed, reclaimed = MODULE.clean(root)

            self.assertEqual(
                set(removed),
                {
                    Path("codex-rs/target"),
                    Path("apps/desktop/src-tauri/target"),
                    Path(
                        "apps/desktop/src-tauri/binaries/"
                        "riftxd-x86_64-unknown-linux-gnu"
                    ),
                },
            )
            self.assertGreater(reclaimed, 0)
            self.assertTrue(gitkeep.is_file())
            self.assertFalse(sidecar.exists())

    def test_dry_run_keeps_generated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "codex-rs/target"
            target.mkdir(parents=True)
            (target / "file").write_text("data", encoding="utf-8")
            removed, _ = MODULE.clean(root, dry_run=True)
            self.assertEqual(removed, [Path("codex-rs/target")])
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
