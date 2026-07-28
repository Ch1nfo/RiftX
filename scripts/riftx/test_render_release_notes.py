import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("render-release-notes.py")
SPEC = importlib.util.spec_from_file_location("render_release_notes", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RenderReleaseNotesTests(unittest.TestCase):
    def test_extracts_requested_section(self) -> None:
        notes = MODULE.extract_release_notes(
            "# Changelog\n\n## [1.0.0] - 2026-07-28\n\n- Ready.\n\n## [0.9.0]\n\n- Old.\n",
            "1.0.0",
        )
        self.assertEqual(notes, "# RiftX 1.0.0\n\n- Ready.\n")

    def test_rejects_placeholder(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            MODULE.extract_release_notes(
                "## [1.0.0] - TBD\n\nRelease notes will be filled later.\n",
                "1.0.0",
            )


if __name__ == "__main__":
    unittest.main()
