"""GReader API client (compatible with FreshRSS, Miniflux, Tiny Tiny RSS, etc.)."""

import logging
from dataclasses import dataclass, field

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Config(BaseSettings):
    url: str = Field(alias="GREADER_URL")
    username: str = Field(alias="GREADER_USERNAME")
    password: SecretStr = Field(alias="GREADER_PASSWORD")
    api_path: str = Field(default="/api/greader.php", alias="GREADER_API_PATH")

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


@dataclass
class Article:
    id: int
    title: str
    summary: str
    url: str
    published: int
    feed_name: str
    is_read: bool
    is_starred: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "published": self.published,
            "feed_name": self.feed_name,
            "is_read": self.is_read,
            "is_starred": self.is_starred,
        }


@dataclass
class Feed:
    id: int
    name: str
    url: str
    unread_count: int = 0
    categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "unread_count": self.unread_count,
            "categories": self.categories,
        }


class AuthenticationError(Exception):
    pass


class GReaderClient:
    def __init__(self, config: Config):
        self._config = config
        base_url = config.url.rstrip("/")
        api_path = config.api_path.rstrip("/")
        self.api_url = f"{base_url}{api_path}"
        self._auth_token: str | None = None
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0, follow_redirects=True)

    async def authenticate(self) -> str:
        auth_url = f"{self.api_url}/accounts/ClientLogin"
        try:
            response = await self._client.post(
                auth_url,
                data={
                    "Email": self._config.username,
                    "Passwd": self._config.password.get_secret_value(),
                },
            )
            response.raise_for_status()
            for line in response.text.split("\n"):
                if line.startswith("Auth="):
                    self._auth_token = line[5:]
                    return self._auth_token
            raise AuthenticationError("No Auth token found in authentication response")
        except httpx.HTTPStatusError as e:
            raise AuthenticationError(f"Authentication failed: {e.response.status_code}") from e

    async def _ensure_authenticated(self) -> None:
        if not self._auth_token:
            await self.authenticate()

    def _auth_headers(self) -> dict[str, str]:
        if not self._auth_token:
            raise AuthenticationError("Not authenticated")
        return {"Authorization": f"GoogleLogin auth={self._auth_token}"}

    async def get_articles(
        self,
        feed_id: int | None = None,
        limit: int = 20,
        include_read: bool = False,
        since_timestamp: int | None = None,
    ) -> list[Article]:
        await self._ensure_authenticated()
        stream_id = f"feed/{feed_id}" if feed_id else "user/-/state/com.google/reading-list"
        url = f"{self.api_url}/reader/api/0/stream/contents/{stream_id}"

        articles: list[Article] = []
        continuation: str | None = None

        while len(articles) < limit:
            params: dict = {"output": "json", "n": limit - len(articles)}
            if not include_read:
                params["xt"] = "user/-/state/com.google/read"
            if since_timestamp:
                params["ot"] = since_timestamp
            if continuation:
                params["c"] = continuation

            response = await self._client.get(url, headers=self._auth_headers(), params=params)
            response.raise_for_status()

            data = response.json()
            new_items = data.get("items", [])
            if not new_items:
                break
            for item in new_items:
                article = self._parse_article(item)
                if article:
                    articles.append(article)
                if len(articles) >= limit:
                    break

            continuation = data.get("continuation")
            if not continuation:
                break

        return articles

    def _parse_article(self, item: dict) -> Article | None:
        try:
            article_id = self._extract_article_id(item.get("id", ""))
            feed_name = item.get("origin", {}).get("title", "Unknown Feed")
            summary = (item.get("summary") or {}).get("content", "")
            categories = item.get("categories", [])
            alternates = item.get("alternate", [])
            return Article(
                id=article_id,
                title=item.get("title", "Untitled"),
                summary=summary,
                url=alternates[0].get("href", "") if alternates else "",
                published=item.get("published", 0),
                feed_name=feed_name,
                is_read="user/-/state/com.google/read" in categories,
                is_starred="user/-/state/com.google/starred" in categories,
            )
        except Exception as e:
            logger.warning("Failed to parse article: %s", e)
            return None

    @staticmethod
    def _extract_article_id(article_id_str: str) -> int:
        if "reader/item/" in article_id_str:
            raw = article_id_str.split("/")[-1]
            try:
                return int(raw)
            except ValueError:
                try:
                    return int(raw, 16)
                except ValueError:
                    pass
        return hash(article_id_str) % 1_000_000_000

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
