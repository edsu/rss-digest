"""Tests for the GReader API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from rss_digest.greader import Article, AuthenticationError, Config, GReaderClient


@pytest.fixture
def config():
    return Config(
        GREADER_URL="https://test.example.com",
        GREADER_USERNAME="testuser",
        GREADER_PASSWORD="testpass",
    )


@pytest.fixture
def client(config):
    return GReaderClient(config)


SAMPLE_ITEM = {
    "id": "tag:google.com,2005:reader/item/1234567890",
    "title": "Test Article",
    "published": 1700000000,
    "alternate": [{"href": "https://example.com/article"}],
    "summary": {"content": "Article summary text"},
    "origin": {"title": "Source Feed"},
    "categories": ["user/-/state/com.google/read"],
}


# --- Authentication ---


async def test_authenticate_success(client):
    mock_response = MagicMock()
    mock_response.text = "SID=abc123\nLSID=def456\nAuth=ghi789"
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        token = await client.authenticate()

    assert token == "ghi789"
    assert client._auth_token == "ghi789"


async def test_authenticate_no_auth_token(client):
    mock_response = MagicMock()
    mock_response.text = "SID=abc123"
    mock_response.raise_for_status = MagicMock()

    with (
        patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response),
        pytest.raises(AuthenticationError, match="No Auth token"),
    ):
        await client.authenticate()


async def test_authenticate_http_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("Forbidden", request=MagicMock(), response=mock_response)
    )

    with (
        patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response),
        pytest.raises(AuthenticationError, match="403"),
    ):
        await client.authenticate()


def test_auth_headers_unauthenticated(client):
    with pytest.raises(AuthenticationError, match="Not authenticated"):
        client._auth_headers()


# --- get_articles ---


def _mock_response(items, continuation=None):
    body = {"items": items}
    if continuation is not None:
        body["continuation"] = continuation
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    return resp


async def test_get_articles(client):
    client._auth_token = "tok"
    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=_mock_response([SAMPLE_ITEM])):
        articles = await client.get_articles(limit=10)

    assert len(articles) == 1
    assert articles[0].title == "Test Article"
    assert articles[0].is_read is True
    assert articles[0].id == 1234567890


async def test_get_articles_empty(client):
    client._auth_token = "tok"
    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=_mock_response([])):
        articles = await client.get_articles()

    assert articles == []


async def test_get_articles_follows_continuation(client):
    client._auth_token = "tok"
    page1 = {**SAMPLE_ITEM, "id": "tag:google.com,2005:reader/item/1"}
    page2 = {**SAMPLE_ITEM, "id": "tag:google.com,2005:reader/item/2"}

    mock_get = AsyncMock(side_effect=[
        _mock_response([page1], continuation="next-page"),
        _mock_response([page2]),
    ])
    with patch.object(client._client, "get", mock_get):
        articles = await client.get_articles(limit=10)

    assert [a.id for a in articles] == [1, 2]
    assert mock_get.call_count == 2


async def test_get_articles_stops_at_limit(client):
    client._auth_token = "tok"
    items = [{**SAMPLE_ITEM, "id": f"tag:google.com,2005:reader/item/{i}"} for i in range(5)]
    mock_get = AsyncMock(return_value=_mock_response(items, continuation="more"))
    with patch.object(client._client, "get", mock_get):
        articles = await client.get_articles(limit=3)

    assert len(articles) == 3
    assert mock_get.call_count == 1


# --- _parse_article ---


class TestParseArticle:
    def test_full_item(self, client):
        article = client._parse_article(SAMPLE_ITEM)
        assert article is not None
        assert article.title == "Test Article"
        assert article.url == "https://example.com/article"
        assert article.summary == "Article summary text"
        assert article.feed_name == "Source Feed"
        assert article.is_read is True
        assert article.is_starred is False

    def test_missing_summary(self, client):
        item = {**SAMPLE_ITEM, "summary": None}
        article = client._parse_article(item)
        assert article is not None
        assert article.summary == ""

    def test_missing_alternate(self, client):
        item = {**SAMPLE_ITEM, "alternate": []}
        article = client._parse_article(item)
        assert article is not None
        assert article.url == ""

    def test_starred(self, client):
        item = {**SAMPLE_ITEM, "categories": ["user/-/state/com.google/starred"]}
        article = client._parse_article(item)
        assert article.is_starred is True
        assert article.is_read is False

    def test_minimal_item(self, client):
        article = client._parse_article({})
        assert article is not None
        assert article.title == "Untitled"


# --- _extract_article_id ---


class TestExtractArticleId:
    def test_decimal(self):
        assert GReaderClient._extract_article_id("tag:google.com,2005:reader/item/1234567890") == 1234567890

    def test_hex(self):
        result = GReaderClient._extract_article_id("tag:google.com,2005:reader/item/00000186a7b3c4d5")
        assert result == 0x00000186A7B3C4D5

    def test_unknown_format(self):
        result = GReaderClient._extract_article_id("some-random-string")
        assert isinstance(result, int)


# --- Article.to_dict ---


def test_article_to_dict():
    a = Article(id=1, title="T", summary="S", url="http://x.com", published=0,
                feed_name="F", is_read=False, is_starred=True)
    d = a.to_dict()
    assert d["title"] == "T"
    assert d["is_starred"] is True


# --- mark_as_read ---


async def test_mark_as_read(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.mark_as_read([1, 2, 3])

    assert mock_post.call_count == 1
    data = mock_post.call_args.kwargs["data"]
    ids = [v for k, v in data if k == "i"]
    assert ids == [
        "tag:google.com,2005:reader/item/0000000000000001",
        "tag:google.com,2005:reader/item/0000000000000002",
        "tag:google.com,2005:reader/item/0000000000000003",
    ]
    assert ("a", "user/-/state/com.google/read") in data


async def test_mark_as_read_batches(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.mark_as_read(list(range(150)))

    assert mock_post.call_count == 2


# --- Lifecycle ---


async def test_aclose(client):
    await client.aclose()
    assert client._client.is_closed
