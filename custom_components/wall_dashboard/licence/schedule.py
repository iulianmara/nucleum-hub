"""When the licence asks the server again (server/README.md §10.9 "Când cere"; D-068): the refresh's rhythm, the
backoff after a failure, and a budget that keeps every home well under the server's rate limits.

Pure: the standard library and the state rule only, no clock and no randomness of its own (the caller passes now and
a ``rng`` with ``uniform``), so the tests can walk a whole month in a few lines.

The rhythm, from what the stored token says:

- the staging instance and a subscription: once a day. A staging token lives 7 days at most and stops the instance
  when it ends (D-068); a subscription's token is renewed with every month paid;
- a one-time licence (and the plans from before D-064): once a week, so its permanent token is never older than the
  30 days after which the dashboard asks for a check (verify_security, LIFE_VERIFY_S);
- when the token's full time ends (a subscription's paid period, a staging token, the one-time licence's 14 days):
  5 minutes after that moment, when the renewal Paddle charged has reached the server, then every hour for a day,
  then daily for as long as the grace lasts. A renewal paid on time thus replaces the token within the hour, and one
  paid during the 7 days of grace within the day (or at once, when the admin opens the settings). The randomness of
  the regular rhythm never carries a request past that moment;
- a token whose grace ended more than a day ago (a subscription that stopped, a staging instance that could not be
  checked): once a week. Only support can bring such a licence back, and the admin can still ask at once;
- a home the server says no longer holds the licence (not_bound): once a week, in case that answer was a mistake;
- after a failure that may pass (no connection, the server busy): 5 minutes, then twice as long each time, up to 6
  hours; after a 429, never before its Retry-After.

Every time gets a little randomness on top (up to an hour, 5 minutes near a period's end), so ten thousand homes
started together by a Home Assistant release do not ask in the same second. The budget counts what was really sent:
at most 10 refreshes an hour and 50 a day, 5 trial requests an hour and 15 a day, half the server's own limits
(server/worker/limits.mjs: refresh 20 and 100, trial 10 and 30), so a Home Assistant that restarts in a loop or an
admin who keeps opening the settings never meets a 429.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final, NamedTuple, Protocol

from .state import window_of

HOUR: Final = 3600
DAY: Final = 24 * HOUR
WEEK: Final = 7 * DAY

END_DELAY: Final = 300  # after a period's end: Paddle has charged the renewal and its webhook has landed
NEAR_SPREAD: Final = 300  # the randomness near a period's end
SPREAD: Final = HOUR  # the randomness of the daily and weekly asks
HOURLY_FOR: Final = DAY  # how long after a period's end the licence asks every hour
BACKOFF_FIRST: Final = 300
BACKOFF_MAX: Final = 6 * HOUR
MIN_GAP: Final = 60  # never two requests of one kind closer than this
START_DELAY: Final = (30.0, 600.0)  # the first refresh after Home Assistant starts: spread over ten minutes
TRIAL_DELAY: Final = 2.0  # the trial at the first start: right after setup, which it never delays
SETTINGS_GAP: Final = 600  # opening the settings asks again only when the last request is older than this

# (per hour, per day) of what this integration sends; the server's are twice these (limits.mjs LIMITS)
BUDGETS: Final = {"refresh": (10, 50), "trial": (5, 15)}
SERVER_LIMITS: Final = {"refresh": (20, 100), "trial": (10, 30)}


class Random(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


def interval_of(claims: Mapping[str, Any] | None, role: str | None) -> int:
    """The regular rhythm: daily for the staging instance and a subscription (or a licence whose token this build
    could not verify: it may be either), weekly otherwise."""
    if role == "staging" or claims is None or claims.get("role") == "staging" or claims.get("plan") == "sub":
        return DAY
    return WEEK


def planned(claims: Mapping[str, Any] | None, role: str | None, *, checked: float | None, now: float,
            bound: bool = True) -> tuple[float, float]:
    """``(when, spread)``: when the next refresh is due by the rhythm (the module's docstring) and how much randomness
    to add to it. ``claims`` are the stored token's known claims (None: no verified token), ``checked`` the time of
    the last answer that was a 200 (None: never). A clock that went back counts from now, never from the future."""
    base = now if checked is None else min(checked, now)
    if not bound:
        return base + WEEK, SPREAD
    when, spread = base + interval_of(claims, role), SPREAD
    if claims is None:
        return when, spread
    window = window_of(claims)
    end = window.full_until
    if end is None:
        return when, spread
    if now < end:
        if end + END_DELAY < when + spread:
            return end + END_DELAY, NEAR_SPREAD
        return when, spread
    if now < end + HOURLY_FOR:
        return min(when, base + HOUR), NEAR_SPREAD
    if window.grace_until is not None and now >= window.grace_until + HOURLY_FOR:
        return base + WEEK, spread
    return min(when, base + DAY), spread


def backoff(failures: int, rng: Random) -> float:
    """The wait after ``failures`` failures in a row (1 or more): 5 minutes doubled each time, at most 6 hours, give
    or take a fifth."""
    if failures < 1:
        return 0.0
    wait = min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** min(failures - 1, 16))
    return wait * rng.uniform(0.8, 1.2)


class Budget:
    """How many requests of one kind went out in the last hour and the last day (a sliding window, so every fixed
    hour and day the server counts holds no more than these)."""

    def __init__(self, per_hour: int, per_day: int, times: Iterable[Any] = ()) -> None:
        self.per_hour = per_hour
        self.per_day = per_day
        self.times = sorted(float(t) for t in times if isinstance(t, (int, float)) and not isinstance(t, bool))

    def _prune(self, now: float) -> None:
        # a time after now is a clock that went back: it is dropped rather than counted for a day
        self.times = [t for t in self.times if now - DAY < t <= now]

    def next_free(self, now: float) -> float:
        """The first moment, now or later, at which one more request stays within both limits (nothing is changed:
        ``now`` may be a moment to come)."""
        free = now
        for window, limit in ((HOUR, self.per_hour), (DAY, self.per_day)):
            inside = [t for t in self.times if now - window < t <= now]
            if len(inside) >= limit:
                free = max(free, inside[len(inside) - limit] + window)
        return free

    def allows(self, now: float) -> bool:
        return self.next_free(now) <= now

    def note(self, now: float) -> None:
        self._prune(now)
        self.times.append(float(now))

    def stored(self) -> list[int]:
        return [int(t) for t in self.times]


class Attempt(NamedTuple):
    """What the manager knows about the requests of one kind: the last one sent, how many failed in a row, the
    server's Retry-After."""

    last: float | None
    failures: int
    not_before: float | None


def next_attempt(due: float, spread: float, attempt: Attempt, budget: Budget, *, now: float, rng: Random,
                 starting: bool = False) -> float:
    """The moment to send the next request of a kind: the rhythm's ``due`` with its randomness, after the backoff of
    the failures so far, the server's Retry-After, the minimum gap and the budget; right after a start, never sooner
    than START_DELAY."""
    when = due + rng.uniform(0.0, spread)
    if attempt.last is not None:
        when = max(when, min(attempt.last, now) + MIN_GAP)
        if attempt.failures:
            when = max(when, min(attempt.last, now) + backoff(attempt.failures, rng))
    if attempt.not_before is not None:
        when = max(when, attempt.not_before)
    if starting:
        when = max(when, now + rng.uniform(*START_DELAY))
    return max(when, budget.next_free(max(when, now)))
