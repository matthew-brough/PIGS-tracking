"""
Discord webhook logging handler.

Aggregates log records and POSTs them to a Discord webhook in batches,
respecting Discord's 2 000 char/message and 5 messages/2 s limits.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import re

import httpx

logger = logging.getLogger(__name__)

# Matches httpx log lines for successful webhook POSTs to Discord
_DISCORD_POST_RE = re.compile(r'POST https://discord\.com/api/webhooks/.+ "HTTP/\S+ 204')


class DiscordWebhookHandler(logging.Handler):
    """Buffer log lines and flush to Discord at intervals or when full.

    A background ``asyncio.Task`` (started via :meth:`start`) drives the
    flush loop; :meth:`emit` is synchronous and safe to call from any thread.
    """

    MAX_LENGTH = 1_990  # chars per message (leave margin under 2 000)
    FLUSH_INTERVAL = 2.0  # seconds between flushes
    _CHECK_INTERVAL = 0.1  # poll frequency inside the background loop

    def __init__(self, webhook_url: str, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.webhook_url = webhook_url
        self._buffer: list[str] = []
        self._buffer_len: int = 0
        self._lock = threading.Lock()
        self._last_flush: float = 0.0
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Call once *inside* a running event loop (e.g. in lifespan)."""
        self._client = httpx.AsyncClient(timeout=10.0)
        self._last_flush = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Flush remaining buffer and release resources."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- logging.Handler -----------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            # Filter out httpx logs for our own successful Discord webhook
            # POSTs – forwarding them would create an infinite loop.
            if _DISCORD_POST_RE.search(msg):
                return
            msg = self.format(record)
            if len(msg) > self.MAX_LENGTH:
                msg = msg[: self.MAX_LENGTH]
            with self._lock:
                sep = 1 if self._buffer else 0
                needed = sep + len(msg)
                if self._buffer and self._buffer_len + needed > self.MAX_LENGTH:
                    # Buffer is full — the background task will flush it soon.
                    # Start a fresh batch with just this message.
                    self._buffer_len = len(msg)
                    self._buffer.append(msg)
                    return
                self._buffer.append(msg)
                self._buffer_len += needed
        except Exception:
            self.handleError(record)

    # -- background loop -----------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._CHECK_INTERVAL)
                with self._lock:
                    if not self._buffer:
                        continue
                    elapsed = time.monotonic() - self._last_flush
                    should_flush = elapsed >= self.FLUSH_INTERVAL or self._buffer_len >= self.MAX_LENGTH
                if should_flush:
                    await self._flush()
        except asyncio.CancelledError:
            pass

    # -- flushing ------------------------------------------------------------

    async def _flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            lines = self._buffer.copy()
            self._buffer.clear()
            self._buffer_len = 0
            self._last_flush = time.monotonic()

        for batch in self._split_batches(lines):
            await self._send(batch)

    def _split_batches(self, lines: list[str]) -> list[str]:
        """Group *lines* into ≤MAX_LENGTH joined strings."""
        batches: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            sep = 1 if current else 0
            if current and current_len + sep + len(line) > self.MAX_LENGTH:
                batches.append("\n".join(current))
                current, current_len, sep = [], 0, 0
            current.append(line)
            current_len += sep + len(line)
        if current:
            batches.append("\n".join(current))
        return batches

    async def _send(self, content: str) -> None:
        if not self._client or not content:
            return
        payload = self._wrap_content(content)
        try:
            resp = await self._client.post(self.webhook_url, json=payload)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 2)
                await asyncio.sleep(float(retry_after))
                await self._client.post(self.webhook_url, json=payload)
        except Exception:
            pass  # silently drop — avoid recursive logging

    @staticmethod
    def _wrap_content(content: str) -> dict:
        """Wrap text in a Discord code block when it fits."""
        if len(content) + 8 <= 2_000:
            return {"content": f"```\n{content}\n```"}
        return {"content": content}
