from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from riftx.cli.i18n import get_language, normalize_language, set_language, tr
from riftx.cli.render import render_runs


@pytest.fixture(autouse=True)
def reset_language() -> None:
    set_language("en")
    yield
    set_language("en")


def test_language_aliases_and_invalid_values() -> None:
    assert normalize_language("en-US") == "en"
    assert normalize_language("zh-CN") == "zh"
    assert normalize_language("中文") == "zh"
    with pytest.raises(ValueError, match="expected 'en' or 'zh'"):
        normalize_language("fr")


def test_translation_interpolates_values_and_falls_back() -> None:
    set_language("zh")
    assert get_language() == "zh"
    assert tr("Tools on {node} (generation {generation})", node="local", generation=2) == (
        "节点 local 上的工具（代次 2）"
    )
    assert tr("Resource type") == "资源类型"
    assert tr("Browser session") == "浏览器会话"
    assert tr("Target HTTP request") == "目标 HTTP 请求"
    assert tr("Untranslated source") == "Untranslated source"


def test_renderers_switch_between_chinese_and_english() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    set_language("zh")
    render_runs(console, [])
    assert "未找到任务" in output.getvalue()

    output.seek(0)
    output.truncate(0)
    set_language("en")
    render_runs(console, [])
    assert "No runs found" in output.getvalue()
