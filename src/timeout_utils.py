"""Timeout utilities for async gather operations.

Provides gather_with_timeout() — a drop-in replacement for asyncio.gather()
that enforces a timeout and returns partial results instead of hanging indefinitely.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, NamedTuple

from src.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OVERALL_TIMEOUT = 3600  # 1 hour hard cap for analyze_market_single
PER_REQUEST_TIMEOUT = 45  # max seconds for a single HTTP request (across all retries)
GATHER_TIMEOUT_FLOOR = 60  # minimum gather timeout in seconds
GATHER_TIMEOUT_HEADROOM = 3  # multiplier over theoretical minimum

# Rate limits (mirrors rate_limiter.py — used for timeout computation only)
STEAMSPY_RATE = 1  # req/sec
GAMALYTIC_RATE = 5  # req/sec


# ---------------------------------------------------------------------------
# GatherResult
# ---------------------------------------------------------------------------

class GatherResult(NamedTuple):
    """Result from gather_with_timeout()."""

    results: list[Any]  # same as asyncio.gather return (completed + TimeoutError for cancelled)
    completed: int  # count of tasks that finished
    timed_out: int  # count of tasks cancelled by timeout
    timeout_seconds: float  # the timeout that was applied
    hit_timeout: bool  # whether the timeout was reached


# ---------------------------------------------------------------------------
# Timeout computation
# ---------------------------------------------------------------------------

def compute_gather_timeout(task_count: int, rate_limit: float) -> float:
    """Compute a gather timeout based on task count and rate limit.

    Formula: max(GATHER_TIMEOUT_FLOOR, (task_count / rate_limit) * GATHER_TIMEOUT_HEADROOM)

    Args:
        task_count: Number of tasks in the gather.
        rate_limit: Requests per second for the target API.

    Returns:
        Timeout in seconds.
    """
    theoretical_min = task_count / rate_limit
    return max(GATHER_TIMEOUT_FLOOR, theoretical_min * GATHER_TIMEOUT_HEADROOM)


# ---------------------------------------------------------------------------
# gather_with_timeout
# ---------------------------------------------------------------------------

async def gather_with_timeout(
    tasks: list[Coroutine],
    timeout: float,
    label: str = "",
    return_exceptions: bool = True,
) -> GatherResult:
    """Like asyncio.gather() but with a timeout. Returns partial results on timeout.

    On timeout, completed tasks' results are preserved and pending tasks are cancelled.
    Cancelled tasks appear as asyncio.TimeoutError instances in the results list.

    Args:
        tasks: List of coroutines to run concurrently.
        timeout: Maximum seconds to wait for all tasks.
        label: Human-readable label for logging.
        return_exceptions: If True, exceptions from tasks are returned as results
            (same as asyncio.gather's return_exceptions parameter).

    Returns:
        GatherResult with results list, completion counts, and timeout metadata.
    """
    if not tasks:
        return GatherResult(results=[], completed=0, timed_out=0,
                            timeout_seconds=timeout, hit_timeout=False)

    futures = [asyncio.ensure_future(t) for t in tasks]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*futures, return_exceptions=return_exceptions),
            timeout=timeout,
        )
        return GatherResult(results=list(results), completed=len(futures), timed_out=0,
                            timeout_seconds=timeout, hit_timeout=False)
    except asyncio.TimeoutError:
        # Collect completed results, cancel pending
        collected: list[Any] = []
        done_count = 0
        for f in futures:
            if f.done():
                done_count += 1
                if f.cancelled():
                    collected.append(asyncio.TimeoutError(f"Cancelled after {timeout}s"))
                else:
                    exc = f.exception()
                    if exc is not None:
                        collected.append(exc)
                    else:
                        collected.append(f.result())
            else:
                f.cancel()
                collected.append(asyncio.TimeoutError(f"Timed out after {timeout}s"))

        timed_out_count = len(futures) - done_count
        log_label = f"[{label}] " if label else ""
        logger.warning(
            f"gather_with_timeout {log_label}{timeout}s timeout hit: "
            f"{done_count}/{len(futures)} completed, {timed_out_count} cancelled"
        )
        return GatherResult(results=collected, completed=done_count,
                            timed_out=timed_out_count, timeout_seconds=timeout,
                            hit_timeout=True)
