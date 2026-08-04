"""Small in-process rate and concurrency limiter."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request, status

from .auth import Principal
from .settings import settings


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._chat_semaphore = asyncio.Semaphore(settings.max_concurrent_chats)

    async def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - settings.rate_limit_window_seconds
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= settings.rate_limit_requests:
                retry_after = max(1, int(events[0] + settings.rate_limit_window_seconds - now))
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limit_exceeded", headers={"Retry-After": str(retry_after)})
            events.append(now)

    @asynccontextmanager
    async def chat_slot(self):
        try:
            await asyncio.wait_for(self._chat_semaphore.acquire(), timeout=1.0)
        except TimeoutError as exc:
            # Return 200+ok=false instead of 503 so the DO Cloudflare edge
            # does not wrap it as HTML 504 (the client cannot parse HTML).
            from fastapi.responses import JSONResponse
            raise _ChatCapacityExhausted() from exc
        try:
            yield
        finally:
            self._chat_semaphore.release()


class _ChatCapacityExhausted(Exception):
    """Raised by chat_slot when the concurrent-chat semaphore is fully booked.
    Caught by the chat route and converted to a clean 200+ok=false response
    so the DO Cloudflare edge does not wrap the error as HTML 504."""



limiter = SlidingWindowLimiter()


async def enforce_rate_limit(request: Request, principal: Principal) -> None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    client_ip = forwarded or (request.client.host if request.client else "unknown")
    await limiter.check(f"{principal.subject}:{client_ip}")
