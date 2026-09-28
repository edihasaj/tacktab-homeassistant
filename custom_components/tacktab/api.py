"""Client for the Tacktab remote control API.

The API is documented at https://applifyer.com/tacktab/api. Every call needs
the password set in Tacktab's settings, sent as a bearer token. Every call
answers with the tablet's current status, so a command also refreshes state.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL


class TacktabError(Exception):
    """The tablet could not be reached or answered unexpectedly."""


class TacktabAuthError(TacktabError):
    """The password was rejected."""


class TacktabRateLimitError(TacktabError):
    """Too many wrong passwords from this address; Tacktab waits before checking again."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"too many wrong passwords, retry in {retry_after} s")
        self.retry_after = retry_after


class TacktabClient:
    """Talks to one Tacktab tablet."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, password: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}/api"
        self._headers = {"Authorization": f"Bearer {password}"}

    async def _request(self, method: str, path: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(10):
                # encoded=True: send the query exactly as built, so a page URL with
                # its own ?a=1&b=2 is not re-split by normalisation.
                response = await self._session.request(method, URL(self._base + path, encoded=True), headers=self._headers)
                if response.status == 401:
                    raise TacktabAuthError("password rejected")
                if response.status == 429:
                    raise TacktabRateLimitError(int(response.headers.get("Retry-After", "30") or 30))
                if response.status != 200:
                    raise TacktabError(f"{method} {path} returned HTTP {response.status}")
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise TacktabError(str(err) or type(err).__name__) from err

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def reload(self) -> dict[str, Any]:
        return await self._request("POST", "/reload")

    async def load(self, url: str) -> dict[str, Any]:
        return await self._request("POST", f"/load?url={quote(url, safe='')}")

    async def sleep(self) -> dict[str, Any]:
        return await self._request("POST", "/sleep")

    async def wake(self) -> dict[str, Any]:
        return await self._request("POST", "/wake")

    async def brightness(self, percent: int) -> dict[str, Any]:
        return await self._request("POST", f"/brightness?percent={int(percent)}")
