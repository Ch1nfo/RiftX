import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify-release-payload.py")
SPEC = importlib.util.spec_from_file_location("verify_release_payload", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyReleasePayloadTests(unittest.TestCase):
    def test_accepts_minimal_release_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "riftx").write_bytes(b"release-binary")
            (root / "README.md").write_text("RiftX release\n", encoding="utf-8")
            self.assertEqual(MODULE.scan_paths([root]), [])

    def test_rejects_secrets_debug_files_and_temporary_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "riftx").write_bytes(
                b"sk-abcdefghijklmnopqrstuvwxyz example.test native-acceptance-secret"
            )
            (root / "riftx.pdb").write_bytes(b"symbols")
            errors = MODULE.scan_paths([root])
            self.assertTrue(any("OpenAI-style secret" in error for error in errors))
            self.assertTrue(any("temporary test endpoint" in error for error in errors))
            self.assertTrue(any("debug artifact" in error for error in errors))

    def test_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target-file"
            target.write_text("safe", encoding="utf-8")
            link = root / "linked-file"
            link.symlink_to(target)
            self.assertTrue(
                any("symbolic link" in error for error in MODULE.scan_paths([root]))
            )


if __name__ == "__main__":
    unittest.main()
