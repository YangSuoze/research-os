"""按论文标题异步搜索 GitHub 仓库，返回待核验的代码仓库候选。"""

from __future__ import annotations

import re
from typing import Any, Self

import httpx

from paper_import_mcp.config import settings


class GitHubSearchError(RuntimeError):
    """GitHub 搜索或 README 请求失败；保留状态码和限流信息。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        rate_limit_reset: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.rate_limit_reset = rate_limit_reset


class GitHubClient:
    """GitHub REST API 异步客户端。

    搜索公开仓库不要求 Token，可用 ``BACKEND_GITHUB_TOKEN`` 提供认证。
    返回搜索候选及元数据，官方仓库归属由后续 service 核验。
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        configured_token = token if token is not None else settings.github_token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "research-os-mcp/0.1.0",
        }
        if configured_token and configured_token.strip():
            self._headers["Authorization"] = f"Bearer {configured_token.strip()}"
        self._client = client or httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def search_repositories(
        self, paper_title: str, *, limit: int = 10
    ) -> dict[str, Any]:
        """按论文标题返回最多 limit 个仓库，使用 GitHub 默认相关性排序。

        在 name、description、readme 中检索完整标题。完整标题无结果且
        搜索结果完整时，尝试冒号前的论文简称；最多发起两次 API 请求。
        返回 repositories、total_count、实际 query 和 incomplete_results。
        空结果与限流/网络失败分开处理，不把请求失败视作论文没有代码。
        """

        title = " ".join(paper_title.split())
        if not title:
            raise ValueError("paper_title must not be blank")
        if len(title) > 256:
            raise ValueError("paper_title must be at most 256 characters")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer between 1 and 100")

        search_title = " ".join(title.replace('"', " ").split())
        if not search_title:
            raise ValueError("paper_title must contain searchable text")
        query = self._title_query(search_title)
        payload = await self._search(query, limit)
        short_title = search_title.split(":", 1)[0].strip()
        if (
            not payload["items"]
            and not payload["incomplete_results"]
            and short_title
            and short_title != search_title
        ):
            query = self._title_query(short_title)
            payload = await self._search(query, limit)

        return {
            "paper_title": title,
            "query": query,
            "total_count": payload["total_count"],
            "incomplete_results": payload["incomplete_results"],
            "repositories": [self._repository(item) for item in payload["items"]],
        }

    async def get_readme(self, full_name: str) -> str | None:
        """读取 owner/repo 默认分支的原始 README；不存在时返回 None。"""

        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name) or any(
            part in {".", ".."} for part in full_name.split("/")
        ):
            raise ValueError("full_name must be an owner/repo repository name")
        try:
            response = await self._client.get(
                f"https://api.github.com/repos/{full_name}/readme",
                headers={
                    **self._headers,
                    "Accept": "application/vnd.github.raw+json",
                },
            )
        except httpx.RequestError as exc:
            raise GitHubSearchError("GitHub README request failed") from exc
        if response.status_code == 404:
            return None
        self._raise_for_error(response, operation="README")
        return response.text

    @staticmethod
    def _title_query(title: str) -> str:
        return f'"{title}" in:name,description,readme'

    async def _search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            response = await self._client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "per_page": limit, "page": 1},
                headers=self._headers,
            )
        except httpx.RequestError as exc:
            raise GitHubSearchError("GitHub search request failed") from exc

        self._raise_for_error(response, operation="search")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubSearchError("GitHub search returned invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("total_count"), int)
            or not isinstance(payload.get("incomplete_results"), bool)
        ):
            raise GitHubSearchError("GitHub search returned an invalid response")
        return payload

    @staticmethod
    def _raise_for_error(response: httpx.Response, *, operation: str) -> None:
        if response.is_error:
            try:
                payload = response.json()
                message = (
                    payload.get("message", "Request failed")
                    if isinstance(payload, dict)
                    else "Request failed"
                )
            except ValueError:
                message = "Request failed"
            raise GitHubSearchError(
                f"GitHub {operation} failed (HTTP {response.status_code}): {message}",
                status_code=response.status_code,
                retry_after=response.headers.get("Retry-After"),
                rate_limit_reset=response.headers.get("X-RateLimit-Reset"),
            )

    @staticmethod
    def _repository(item: Any) -> dict[str, Any]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("full_name"), str)
            or not isinstance(item.get("html_url"), str)
        ):
            raise GitHubSearchError("GitHub search returned an invalid repository")
        return {
            "full_name": item["full_name"],
            "url": item["html_url"],
            "description": item.get("description"),
            "stars": item.get("stargazers_count", 0),
            "language": item.get("language"),
            "fork": item.get("fork", False),
            "archived": item.get("archived", False),
        }
