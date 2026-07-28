import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify-license-reports.py")
SPEC = importlib.util.spec_from_file_location("verify_license_reports", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyLicenseReportsTests(unittest.TestCase):
    def test_accepts_optional_copyleft_branch_when_permissive_choice_exists(
        self,
    ) -> None:
        self.assertTrue(MODULE.expression_is_allowed("Apache-2.0 OR GPL-2.0-only"))
        self.assertTrue(
            MODULE.expression_is_allowed(
                "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT"
            )
        )

    def test_rejects_copyleft_only_unknown_and_missing_licenses(self) -> None:
        self.assertFalse(MODULE.expression_is_allowed("GPL-3.0-only"))
        self.assertFalse(MODULE.expression_is_allowed("LicenseRef-Proprietary"))
        self.assertFalse(MODULE.expression_is_allowed(""))

    def test_validates_cargo_and_pnpm_report_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cargo = root / "cargo.json"
            cargo.write_text(
                json.dumps(
                    {
                        "packages": [
                            {"name": "ok", "version": "1.0.0", "license": "MIT"},
                            {
                                "name": "bad",
                                "version": "2.0.0",
                                "license": "AGPL-3.0-only",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            pnpm = root / "pnpm.json"
            pnpm.write_text(
                json.dumps(
                    {
                        "ISC": [
                            {
                                "name": "node-ok",
                                "versions": ["1.2.3"],
                                "license": "ISC",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(MODULE.verify_pnpm_licenses(pnpm), [])
            self.assertEqual(
                MODULE.verify_cargo_metadata(cargo),
                ["Rust bad@2.0.0: 'AGPL-3.0-only'"],
            )


if __name__ == "__main__":
    unittest.main()
