"""The doorbell: a WebSocket SingleMall rings when the list changes.

The integration keeps one connection open per signed-in person. SingleMall
sends ``{"kind": "changed"}`` on it whenever that person's list changes, from
a phone, from Claude or from Home Assistant itself, and the coordinator reads
the list once. The message carries nothing of the list.

The connection is opened from here, so nothing inside the home has to be
reachable. SingleMall closes it after two hours at most, and after ten
minutes without traffic, so it is pinged every eight minutes and opened again
whenever it closes, waiting longer after each failure. Every successful open
reads the list once, so a change made while it was closed is not missed. The
coordinator's own read every thirty minutes is the safety net under all of
this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import json
import logging
import random
from typing import Any, Protocol

from aiohttp import ClientError, ClientSession, WSMsgType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AbstractAuth, SingleMallError

_LOGGER = logging.getLogger(__name__)

# API Gateway closes a connection idle for ten minutes.
PING_INTERVAL_SECONDS = 8 * 60
# The wait before opening again: doubled after each failure, up to the ceiling.
RETRY_FIRST_SECONDS = 5.0
RETRY_CEILING_SECONDS = 5 * 60.0
# A connection that stayed open this long was not a failure.
STABLE_SECONDS = 60.0


class Socket(Protocol):
    """The part of aiohttp's ``ClientWebSocketResponse`` the doorbell uses."""

    async def send_json(self, data: Any) -> None: ...

    async def receive(self) -> Any: ...

    async def close(self) -> Any: ...


type Connect = Callable[[str, dict[str, str]], Awaitable[Socket]]


def aiohttp_connect(session: ClientSession) -> Connect:
    """Open the socket with Home Assistant's shared HTTP session."""

    async def connect(url: str, headers: dict[str, str]) -> Socket:
        return await session.ws_connect(url, headers=headers, autoping=True)

    return connect


def connect_for(hass: HomeAssistant) -> Connect:
    """How the doorbell opens its socket; the tests replace this."""
    return aiohttp_connect(async_get_clientsession(hass))


def is_doorbell(text: str) -> bool:
    """Whether a message says that the list changed."""
    try:
        message = json.loads(text)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("kind") == "changed"


class Doorbell:
    """Keeps the connection open and asks for a read when it rings."""

    def __init__(
        self,
        url: str,
        auth: AbstractAuth,
        connect: Connect,
        on_ring: Callable[[], Awaitable[None]],
        *,
        ping_interval: float = PING_INTERVAL_SECONDS,
        retry_first: float = RETRY_FIRST_SECONDS,
        retry_ceiling: float = RETRY_CEILING_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Ring ``on_ring`` on each open and on each doorbell."""
        self._url = url
        self._auth = auth
        self._connect = connect
        self._on_ring = on_ring
        self._ping_interval = ping_interval
        self._retry_first = retry_first
        self._retry_ceiling = retry_ceiling
        self._sleep = sleep
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._socket: Socket | None = None
        self._stopped = False

    async def run(self) -> None:
        """Open, listen, and open again, until stopped."""
        wait = self._retry_first
        while not self._stopped:
            opened_at = self._clock()
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except (ClientError, TimeoutError, SingleMallError, OSError) as err:
                _LOGGER.debug("SingleMall: the doorbell closed (%s)", type(err).__name__)
            if self._stopped:
                return
            if self._clock() - opened_at >= STABLE_SECONDS:
                wait = self._retry_first
            # A little jitter, so many installations cut off together do not
            # all come back in the same second.
            await self._sleep(wait * random.uniform(0.8, 1.2))
            wait = min(wait * 2, self._retry_ceiling)

    async def _listen(self) -> None:
        token = await self._auth.async_get_access_token()
        socket = await self._connect(self._url, {"Authorization": f"Bearer {token}"})
        self._socket = socket
        pinger = asyncio.create_task(self._ping(socket))
        try:
            # Whatever changed while the connection was down.
            await self._on_ring()
            while True:
                message = await socket.receive()
                if message.type == WSMsgType.TEXT:
                    if is_doorbell(message.data):
                        await self._on_ring()
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    return
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pinger
            self._socket = None
            with contextlib.suppress(ClientError, TimeoutError, OSError):
                await socket.close()

    async def _ping(self, socket: Socket) -> None:
        while True:
            await self._sleep(self._ping_interval)
            with contextlib.suppress(ClientError, TimeoutError, OSError, ConnectionError):
                await socket.send_json({"action": "ping"})

    async def stop(self) -> None:
        """Stop opening and close what is open."""
        self._stopped = True
        socket = self._socket
        if socket is not None:
            with contextlib.suppress(ClientError, TimeoutError, OSError):
                await socket.close()
