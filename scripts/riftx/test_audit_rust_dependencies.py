import importlib.util
from datetime import date
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("audit-rust-dependencies.py")
SPEC = importlib.util.spec_from_file_location("audit_rust_dependencies", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report(advisory_id: str, crate: str, version: str) -> dict[str, object]:
    return {
        "vulnerabilities": {
            "list": [
                {
                    "advisory": {"id": advisory_id},
                    "package": {"name": crate, "version": version},
                }
            ]
        }
    }


class AuditRustDependenciesTests(unittest.TestCase):
    def test_repository_exceptions_are_current(self) -> None:
        root = Path(__file__).resolve().parents[2]
        exceptions = MODULE.load_exceptions(
            root / "security/rustsec-exceptions.toml", date(2026, 7, 28)
        )
        self.assertEqual(len(exceptions), 5)

    def test_rejects_new_vulnerability_and_stale_exception(self) -> None:
        exceptions = {("RUSTSEC-2026-0001", "known", "1.0.0"): {"reason": "fixture"}}
        errors = MODULE.evaluate(
            [report("RUSTSEC-2026-0002", "new", "2.0.0")], exceptions
        )
        self.assertEqual(
            errors,
            [
                "unapproved vulnerability RUSTSEC-2026-0002 in new@2.0.0",
                "stale exception RUSTSEC-2026-0001 for known@1.0.0",
            ],
        )

    def test_rejects_expired_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exceptions.toml"
            path.write_text(
                """
schema_version = 1
[[exception]]
id = "RUSTSEC-2026-0001"
crate = "fixture"
versions = ["1.0.0"]
expires = "2026-01-01"
release_reachable = false
scope = "test"
reason = "This fixture reason is deliberately long enough for validation."
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "expired"):
                MODULE.load_exceptions(path, date(2026, 7, 28))


if __name__ == "__main__":
    unittest.main()
