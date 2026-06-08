import asyncio
import time
from collections import deque, defaultdict
from typing import Tuple


class TokenBucketRateLimiter:
    """
    Per-client token-bucket limiter with flood protection.

    Usage:
      limiter = TokenBucketRateLimiter(...)
      allowed, retry_after = await limiter.allow(client_id)
      if not allowed:
          # retry_after in seconds
    """

    def __init__(
        self,
        capacity: int = 4,
        refill_amount: int = 1,
        refill_interval: float = 0.5,
        deny_window: float = 10.0,
        deny_threshold: int = 5,
        base_block_seconds: float = 30.0,
        backoff_multiplier: float = 2.0,
        max_block_seconds: float = 3600.0,
        notify_cooldown: float = 10.0,
    ):
        # token-bucket params
        self.capacity = float(capacity)
        self.refill_amount = float(refill_amount)
        self.refill_interval = float(refill_interval)

        # flood protection params
        self.deny_window = deny_window
        self.deny_threshold = deny_threshold
        self.base_block_seconds = base_block_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_block_seconds = max_block_seconds

        # minimal notify cooldown to avoid repeated "you're rate limited"
        # responses
        self.notify_cooldown = notify_cooldown

        # per-client state
        self._state = {}  # client_id -> {tokens, last_refill, lock}
        # client_id -> deque[timestamp_of_denial]
        self._denials = defaultdict(deque)
        self._block_info = {}  # client_id -> (blocked_until, block_count)
        self._last_notify = {}  # client_id -> last_notification_time

        # protect _state & book-keeping
        self._global_lock = asyncio.Lock()

    def _now(self) -> float:
        return time.monotonic()

    async def _ensure_client(self, client_id: str):
        async with self._global_lock:
            if client_id not in self._state:
                now = self._now()
                self._state[client_id] = {
                    "tokens": self.capacity,
                    "last_refill": now,
                    "lock": asyncio.Lock(),
                }

    async def allow(self, client_id: str) -> Tuple[bool, float]:
        """
        Attempt to allow an action for client_id.

        Returns:
            (True, 0.0) if allowed,
            (False, retry_after_seconds) if not allowed.
        """
        await self._ensure_client(client_id)
        state = self._state[client_id]
        lock = state["lock"]

        now = self._now()

        # Check temporary block first
        blocked_until, _ = self._block_info.get(client_id, (0.0, 0))
        if now < blocked_until:
            return False, blocked_until - now

        async with lock:
            # Refill tokens based on elapsed time
            elapsed = now - state["last_refill"]
            if elapsed >= self.refill_interval:
                steps = int(elapsed / self.refill_interval)
                add = steps * self.refill_amount
                state["tokens"] = min(self.capacity, state["tokens"] + add)
                state["last_refill"] += steps * self.refill_interval

            if state["tokens"] >= 1.0:
                state["tokens"] -= 1.0
                # On success, clear old denials (they're no longer relevant)
                dq = self._denials.get(client_id)
                if dq:
                    while dq and dq[0] + self.deny_window < now:
                        dq.popleft()
                return True, 0.0

            # Not enough tokens -> record denial and possibly trigger block
            self._record_denial(client_id, now)
            blocked = self._check_and_apply_block(client_id, now)
            if blocked:
                blocked_until, _ = self._block_info.get(client_id, (0.0, 0))
                return False, max(0.0, blocked_until - now)
            # Not blocked yet, but still rate-limited: return a short retry
            # estimate.
            # Estimate based on time until next token (simple heuristic)
            time_since_refill = now - state["last_refill"]
            time_until_next = max(
                0.0, self.refill_interval - time_since_refill)
            return False, time_until_next

    def _record_denial(self, client_id: str, now: float):
        dq = self._denials[client_id]
        dq.append(now)
        # prune old denials
        while dq and dq[0] + self.deny_window < now:
            dq.popleft()

    def _check_and_apply_block(self, client_id: str, now: float) -> bool:
        dq = self._denials.get(client_id)
        if not dq:
            return False
        if len(dq) < self.deny_threshold:
            return False

        # Apply or increase block
        blocked_until, block_count = self._block_info.get(client_id, (0.0, 0))
        # new block length with exponential backoff
        next_block = min(
            self.max_block_seconds,
            self.base_block_seconds * (self.backoff_multiplier ** block_count),
        )
        self._block_info[client_id] = (now + next_block, block_count + 1)
        # clear denials so threshold counting restarts after block
        self._denials[client_id].clear()
        return True

    def get_block_time(self, client_id: str) -> float:
        """Return seconds remaining in block, 0 if not blocked."""
        now = self._now()
        blocked_until, _ = self._block_info.get(client_id, (0.0, 0))
        return max(0.0, blocked_until - now)

    def notify_allowed(self, client_id: str) -> bool:
        """
        Return True if we should send a human-facing notification now for this
        client. Throttles notifications to at most one per notify_cooldown
        seconds.
        Non-async (fast) — safe to call from sync code.
        """
        now = self._now()
        last = self._last_notify.get(client_id, 0.0)
        if now - last >= self.notify_cooldown:
            self._last_notify[client_id] = now
            return True
        return False

    # Optional helpers for tests or admin tools:
    def force_reset(self, client_id: str):
        """Reset limiter state for a client (useful in tests)."""
        self._state.pop(client_id, None)
        self._denials.pop(client_id, None)
        self._block_info.pop(client_id, None)
        self._last_notify.pop(client_id, None)
