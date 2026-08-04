from __future__ import annotations

from riftx.audit import (
    ConfigurationDetector,
    DependencyDetector,
    DetectorInput,
    JavaScriptDetector,
    PythonDetector,
    SecretDetector,
    SourceClassification,
    builtin_detector_registry,
    builtin_detectors,
)


def _input(
    content: str,
    *,
    path: str,
    language: str = "unknown",
    category: SourceClassification = SourceClassification.SOURCE,
) -> DetectorInput:
    return DetectorInput(
        relative_path=path,
        blob_digest="a" * 64,
        language=language,
        category=category,
        content=content,
    )


def test_builtin_registry_has_fixed_complete_rule_inventory() -> None:
    registry = builtin_detector_registry()

    assert [value.rule_id for value in registry.metadata()] == [
        "configuration.insecure_setting",
        "dependency.unpinned",
        "javascript.dangerous_api",
        "python.dangerous_api",
        "secret.hardcoded_credential",
    ]
    assert len(builtin_detectors()) == 5
    assert len({value.implementation_digest for value in registry.metadata()}) == 5
    assert registry.registry_digest == builtin_detector_registry().registry_digest


def test_secret_detector_finds_high_confidence_values_and_redacts_evidence() -> None:
    content = (
        'password = "correct-horse-battery-staple"\n'
        'api_key = "${API_KEY}"\n'
        'aws = "AKIA1234567890ABCDEF"\n'
        "-----BEGIN PRIVATE KEY-----\n"
    )

    matches = SecretDetector().detect(_input(content, path="settings.py"))

    assert len(matches) == 3
    assert all("correct-horse" not in value.evidence for value in matches)
    assert any("[REDACTED]" in value.evidence for value in matches)
    assert [value.line for value in matches] == [1, 3, 4]


def test_dependency_detector_checks_common_local_manifests() -> None:
    detector = DependencyDetector()
    requirements = detector.detect(
        _input(
            "requests==2.32.0\nflask>=3\ngit+https://example.invalid/repo.git\n",
            path="requirements.txt",
        )
    )
    package_json = detector.detect(
        _input(
            '{"dependencies":{"exact":"1.2.3","range":"^2.0.0","latest":"latest"}}',
            path="package.json",
            category=SourceClassification.DATA,
        )
    )
    pyproject = detector.detect(
        _input(
            '[project]\ndependencies = ["safe==1.2.3", "unsafe>=2"]\n',
            path="pyproject.toml",
            category=SourceClassification.DATA,
        )
    )

    assert [value.line for value in requirements] == [2, 3]
    assert len(package_json) == 2
    assert len(pyproject) == 1
    assert all("not pinned" in value.message for value in (*requirements, *package_json))


def test_configuration_detector_reports_insecure_defaults() -> None:
    content = (
        "debug: true\n"
        "verify_ssl = false\n"
        'allow_origins = ["*"]\n'
        "USER root\n"
        "debug: false\n"
    )

    matches = ConfigurationDetector().detect(
        _input(
            content,
            path="config/app.yaml",
            category=SourceClassification.CONFIGURATION,
        )
    )

    assert [value.line for value in matches] == [1, 2, 3, 4]


def test_python_detector_uses_ast_and_avoids_safe_yaml_loader() -> None:
    content = (
        "import os, pickle, subprocess, yaml\n"
        "eval(user_input)\n"
        "os.system(command)\n"
        "subprocess.run(command, shell=True)\n"
        "pickle.loads(payload)\n"
        "yaml.load(payload)\n"
        "yaml.load(payload, Loader=yaml.SafeLoader)\n"
    )

    matches = PythonDetector().detect(
        _input(content, path="app.py", language="python")
    )

    assert [value.line for value in matches] == [2, 3, 4, 5, 6]
    assert PythonDetector().detect(
        _input("def broken(:\n", path="broken.py", language="python")
    ) == ()


def test_javascript_detector_covers_dynamic_code_shell_dom_and_tls() -> None:
    content = (
        "eval(input);\n"
        "const fn = new Function(source);\n"
        "child_process.exec(command);\n"
        "node.innerHTML = html;\n"
        "const options = { rejectUnauthorized: false };\n"
    )

    matches = JavaScriptDetector().detect(
        _input(content, path="app.ts", language="typescript")
    )

    assert [value.line for value in matches] == [1, 2, 3, 4, 5]
