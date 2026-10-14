"""A small client for the SingleMall Home Assistant API.

The routes it calls:

- ``GET /shopping-list`` -> ``200 {"lines": [...]}`` with a strong ``ETag``,
  or ``304`` when ``If-None-Match`` matches;
- ``POST /shopping-list/items`` -> ``201`` with the line added or raised;
- ``PATCH /shopping-list/items/{id}`` -> ``200`` with the line;
- ``DELETE /shopping-list/items/{id}`` -> ``204``.

Every error is ``{"error": "<code>"}``. This module knows nothing about
Home Assistant entities; it only turns HTTP into values and exceptions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientResponseError, ClientSession
from homeassistant.helpers import config_entry_oauth2_flow

from .const import REQUEST_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)

SHOPPING_LIST_PATH = "/shopping-list"
ITEMS_PATH = "/shopping-list/items"


class SingleMallError(Exception):
    """Any failure talking to SingleMall."""

    def __init__(self, code: str, status: int | None = None) -> None:
        """Keep the API's error code and the HTTP status, never a body."""
        super().__init__(code if status is None else f"{code} ({status})")
        self.code = code
        self.status = status


class AuthError(SingleMallError):
    """The sign-in is no longer valid: the person has to sign in again."""


class ForbiddenError(SingleMallError):
    """The person signed in is not allowed to use SingleMall."""


class NotFoundError(SingleMallError):
    """The line is not (or no longer) on the list."""


class RequestError(SingleMallError):
    """The API refused the request as malformed (a 400 with a code)."""


class ConnectionFailed(SingleMallError):
    """SingleMall could not be reached, or answered with a server error."""


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the shopping list, as the API serves it."""

    id: str
    name: str
    quantity: int
    ticked: bool
    typed: bool
    note: str | None = None
    product_id: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> Line:
        """Read one line; raise ``SingleMallError`` for anything else."""
        if not isinstance(value, dict):
            raise SingleMallError("invalid_response")
        try:
            line_id = value["id"]
            name = value["name"]
            quantity = value["quantity"]
            ticked = value["ticked"]
        except KeyError as err:
            raise SingleMallError("invalid_response") from err
        if not isinstance(line_id, str) or not isinstance(name, str):
            raise SingleMallError("invalid_response")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise SingleMallError("invalid_response")
        note = value.get("note")
        product_id = value.get("productId")
        return cls(
            id=line_id,
            name=name,
            quantity=quantity,
            ticked=bool(ticked),
            typed=bool(value.get("typed", product_id is None)),
            note=note if isinstance(note, str) and note else None,
            product_id=product_id if isinstance(product_id, str) else None,
        )


@dataclass(frozen=True, slots=True)
class ListSnapshot:
    """What a read of the list returned."""

    lines: list[Line] | None
    """The lines, or ``None`` when the server answered 304 (unchanged)."""
    etag: str | None


class AbstractAuth(ABC):
    """Where a request's bearer comes from."""

    @abstractmethod
    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing it when needed."""


class OAuth2SessionAuth(AbstractAuth):
    """A config entry's tokens, refreshed by Home Assistant's OAuth2 session."""

    def __init__(self, session: config_entry_oauth2_flow.OAuth2Session) -> None:
        """Wrap the entry's session."""
        self._session = session

    async def async_get_access_token(self) -> str:
        """Refresh when close to expiry; a refused refresh means sign in again."""
        try:
            await self._session.async_ensure_token_valid()
        except ClientResponseError as err:
            if HTTPStatus.BAD_REQUEST <= err.status < HTTPStatus.INTERNAL_SERVER_ERROR:
                # Cognito answers 400 invalid_grant for an expired or revoked
                # refresh token: only a new sign-in can fix that.
                raise AuthError("refresh_refused", err.status) from err
            raise ConnectionFailed("refresh_failed", err.status) from err
        except (ClientError, TimeoutError) as err:
            raise ConnectionFailed("refresh_failed") from err
        return str(self._session.token["access_token"])


class StaticTokenAuth(AbstractAuth):
    """A token just obtained by the config flow, before any entry exists."""

    def __init__(self, access_token: str) -> None:
        """Keep the token."""
        self._access_token = access_token

    async def async_get_access_token(self) -> str:
        """Return the token as it is."""
        return self._access_token


def _error_for(status: int, code: str) -> SingleMallError:
    if status == HTTPStatus.UNAUTHORIZED:
        return AuthError(code, status)
    if status == HTTPStatus.FORBIDDEN:
        return ForbiddenError(code, status)
    if status == HTTPStatus.NOT_FOUND:
        return NotFoundError(code, status)
    if status in (HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT):
        return RequestError(code, status)
    if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return ConnectionFailed(code, status)
    return SingleMallError(code, status)


class SingleMallApi:
    """The four routes of the Home Assistant API."""

    def __init__(self, session: ClientSession, auth: AbstractAuth, base_url: str) -> None:
        """Bind a client session, a token source and the API base URL."""
        self._session = session
        self._auth = auth
        self._base_url = base_url.rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        token = await self._auth.async_get_access_token()
        request_headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        if headers:
            request_headers.update(headers)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response: ClientResponse = await self._session.request(
                    method, f"{self._base_url}{path}", json=json, headers=request_headers
                )
                async with response:
                    status = response.status
                    response_headers = {k.lower(): v for k, v in response.headers.items()}
                    payload: Any = None
                    if status not in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED):
                        try:
                            payload = await response.json(content_type=None)
                        except ValueError:
                            payload = None
        except TimeoutError as err:
            raise ConnectionFailed("timeout") from err
        except ClientError as err:
            raise ConnectionFailed("connection_error") from err

        if status >= HTTPStatus.BAD_REQUEST:
            code = payload.get("error") if isinstance(payload, dict) else None
            # The status and the code, never the body or the words of a line.
            _LOGGER.debug("%s %s answered %s %s", method, path.split("/items/")[0], status, code)
            raise _error_for(status, code if isinstance(code, str) else "http_error")
        return status, response_headers, payload

    async def async_get_lines(self, etag: str | None = None) -> ListSnapshot:
        """Read the list; ``lines`` is ``None`` when it did not change since ``etag``."""
        headers = {"if-none-match": etag} if etag else None
        status, response_headers, payload = await self._request(
            "GET", SHOPPING_LIST_PATH, headers=headers
        )
        new_etag = response_headers.get("etag") or etag
        if status == HTTPStatus.NOT_MODIFIED:
            return ListSnapshot(lines=None, etag=new_etag)
        if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
            raise SingleMallError("invalid_response", status)
        return ListSnapshot(
            lines=[Line.from_json(line) for line in payload["lines"]],
            etag=response_headers.get("etag"),
        )

    async def async_add_item(
        self, name: str, *, note: str | None = None, quantity: int | None = None
    ) -> Line:
        """Add words to the list, or raise the line that already has them."""
        body: dict[str, Any] = {"name": name}
        if note:
            body["note"] = note
        if quantity is not None:
            body["quantity"] = quantity
        _, _, payload = await self._request("POST", ITEMS_PATH, json=body)
        return Line.from_json(payload)

    async def async_update_item(self, line_id: str, changes: dict[str, Any]) -> Line:
        """Change only the fields in ``changes`` (name, note, ticked, quantity)."""
        _, _, payload = await self._request("PATCH", f"{ITEMS_PATH}/{line_id}", json=changes)
        return Line.from_json(payload)

    async def async_delete_item(self, line_id: str) -> None:
        """Remove a line; the server writes the app's tombstone."""
        await self._request("DELETE", f"{ITEMS_PATH}/{line_id}")
