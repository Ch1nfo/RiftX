from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from riftx.web import (
    OpenAIHostedSearchProvider,
    SearchFreshness,
    SearchProviderError,
    SearchRequest,
    SearXNGSearchProvider,
)


async def test_searxng_normalizes_unicode_publication_and_duplicates() -> None:
    captured: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "安全公告 – 修复",
                        "url": "https://vendor.example/advisory?utm_source=search",
                        "content": "<b>受影响版本</b> 1.0",
                        "publishedDate": "2026-07-29T12:00:00Z",
                        "engine": "example",
                    },
                    {
                        "title": "duplicate",
                        "url": "https://vendor.example/advisory",
                        "content": "duplicate",
                    },
                    {
                        "title": "blocked",
                        "url": "https://forum.example/thread",
                    },
                ]
            },
        )

    provider = SearXNGSearchProvider(
        "https://search.example/base",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    response = await provider.search(
        SearchRequest(
            query="CVE 测试",
            freshness=SearchFreshness.MONTH,
            language="zh-CN",
            allowed_domains=["vendor.example"],
        )
    )

    assert captured is not None
    assert captured.url.path == "/base/search"
    assert captured.url.params["q"] == "CVE 测试"
    assert captured.url.params["format"] == "json"
    assert captured.url.params["language"] == "zh-CN"
    assert captured.url.params["time_range"] == "month"
    assert len(response.results) == 1
    result = response.results[0]
    assert result.title == "安全公告 – 修复"
    assert result.normalized_url == "https://vendor.example/advisory"
    assert result.snippet == "受影响版本  1.0"
    assert result.published_at is not None
    assert result.provider_metadata["engine"] == "example"


async def test_searxng_empty_results() -> None:
    provider = SearXNGSearchProvider(
        "https://search.example",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))
        ),
    )
    response = await provider.search(SearchRequest(query="nothing"))
    assert response.results == []


@pytest.mark.parametrize("status", [429, 503])
async def test_searxng_classifies_retryable_http_failures(status: int) -> None:
    provider = SearXNGSearchProvider(
        "https://search.example",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))),
    )
    with pytest.raises(SearchProviderError) as caught:
        await provider.search(SearchRequest(query="retry"))
    assert caught.value.retryable is True
    assert caught.value.status_code == status


async def test_searxng_classifies_timeout() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    provider = SearXNGSearchProvider(
        "https://search.example",
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(SearchProviderError, match="timed out") as caught:
        await provider.search(SearchRequest(query="slow"))
    assert caught.value.retryable is True


class FakeResponses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self.response


async def test_openai_hosted_search_normalizes_citations_and_filters_domains() -> None:
    annotation = SimpleNamespace(
        type="url_citation",
        url="https://docs.example/security/fix?utm_campaign=hosted",
        title="Official Fix",
    )
    duplicate_source = SimpleNamespace(url="https://docs.example/security/fix?utm_campaign=hosted")
    blocked_source = SimpleNamespace(url="https://blocked.example/post")
    output = [
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    type="output_text",
                    text="The official fix is available.",
                    annotations=[annotation],
                )
            ],
        ),
        SimpleNamespace(
            type="web_search_call",
            action=SimpleNamespace(sources=[duplicate_source, blocked_source]),
        ),
    ]
    api = FakeResponses(SimpleNamespace(id="resp-1", output=output))
    client = SimpleNamespace(responses=api)
    provider = OpenAIHostedSearchProvider(client, model="gpt-search")

    response = await provider.search(
        SearchRequest(
            query="official fix",
            allowed_domains=["docs.example"],
            max_results=3,
        )
    )

    assert len(response.results) == 1
    assert response.results[0].title == "Official Fix"
    assert response.results[0].normalized_url == "https://docs.example/security/fix"
    assert response.results[0].snippet == "The official fix is available."
    assert api.kwargs["model"] == "gpt-search"
    assert api.kwargs["include"] == ["web_search_call.action.sources"]
    assert api.kwargs["tools"] == [
        {
            "type": "web_search",
            "search_context_size": "medium",
            "filters": {"allowed_domains": ["docs.example"]},
        }
    ]


def test_search_request_rejects_conflicting_domain_filters() -> None:
    with pytest.raises(ValueError, match="both allowed and blocked"):
        SearchRequest(
            query="conflict",
            allowed_domains=["EXAMPLE.com"],
            blocked_domains=["example.com"],
        )
