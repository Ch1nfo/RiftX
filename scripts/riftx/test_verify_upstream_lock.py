import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify-upstream-lock.py")
SPEC = importlib.util.spec_from_file_location("verify_upstream_lock", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyUpstreamLockTests(unittest.TestCase):
    def test_rejects_unexpected_commit(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(ValueError, "does not match expected"):
            MODULE.validate(root, "0" * 40)

    def test_rejects_incomplete_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex-upstream.lock").write_text(
                "\n".join(
                    [
                        "schema_version = 1",
                        f'repository = "{MODULE.OFFICIAL_REPOSITORY}"',
                        f'commit = "{"1" * 40}"',
                        'excluded_paths = [".git", ".codex"]',
                        "patched_components = []",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "source is incomplete"):
                MODULE.validate(root, None)


if __name__ == "__main__":
    unittest.main()
