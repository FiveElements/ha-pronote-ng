"""PRONOTE implementation of the common school connector seam."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import time
from typing import TYPE_CHECKING, Any, Final, cast

from ..const import Priority, SessionStrategy, Tier  # noqa: TID252
from ..gateway import PronoteGateway  # noqa: TID252
from ..models import (  # noqa: TID252
    AttendanceFacts,
    EvaluationsFacts,
    GatewayResult,
    HistoryFacts,
    MarksFacts,
    SessionFacts,
)
from ..ratelimit import RateLimitConfig, RateLimiter  # noqa: TID252
from ..session import (  # noqa: TID252
    SerialExecutor,
    SessionCredentials,
    SessionManager,
)
from .errors import ConnectorChildMissingError, ConnectorUnsupportedError
from .protocol import ConnectorCapabilities, Source

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from ..hardened_client import HardenedClient  # noqa: TID252
    from ..models import Period  # noqa: TID252


_COLLECTABLE_TIERS: Final = frozenset(tier for tier in Tier if tier is not Tier.SESSION)


class PronoteConnector:
    """Own the complete rate-limited PRONOTE transport stack."""

    CAPABILITIES: Final = ConnectorCapabilities(
        source=Source.PRONOTE,
        tiers=_COLLECTABLE_TIERS,
        writes=frozenset(),
        services=frozenset(),
    )

    def __init__(
        self,
        *,
        entry_id: str,
        timezone: str,
        credentials: SessionCredentials,
        rate_limit_config: RateLimitConfig,
        limiter_state: Mapping[str, Any],
        strategy: SessionStrategy,
        connect_timeout: float,
        read_timeout: float,
        history_periods: int,
        on_credentials_rotated: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        self.gateway = PronoteGateway(timezone)
        self.limiter = RateLimiter(
            rate_limit_config,
            clock=time.monotonic,
            now=self.gateway.now,
        )
        self.limiter.import_state(limiter_state)
        self.executor = SerialExecutor(entry_id, read_timeout)
        self.session = SessionManager(
            entry_id=entry_id,
            credentials=credentials,
            limiter=self.limiter,
            executor=self.executor,
            strategy=strategy,
            now=self.gateway.now,
            clock=time.monotonic,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            on_credentials_rotated=on_credentials_rotated,
        )
        self._history_periods = history_periods
        self._student_ids: tuple[str, ...] = ()
        self._session_facts: dict[str, SessionFacts] = {}
        self._unread_by_student: dict[str, dict[str, int]] = {}

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """Describe the tiers this connector can collect."""
        return self.CAPABILITIES

    def now(self) -> datetime:
        """Return the current instant in the establishment timezone."""
        return self.gateway.now()

    def today(self) -> date:
        """Return today's date in the establishment timezone."""
        return self.gateway.today()

    async def async_open(self) -> None:
        """Open the session and cache the zero-cost bootstrap facts."""
        self._student_ids = await self.session.run(
            str(Tier.SESSION),
            Priority.CRITICAL,
            _client_student_ids,
            cost=0,
        )
        for student_id in self._student_ids:
            result = await self.session.run(
                str(Tier.SESSION),
                Priority.CRITICAL,
                self.gateway.session_facts,
                student_id=student_id,
                cost=0,
            )
            self._session_facts[student_id] = result.facts

    def student_ids(self) -> tuple[str, ...]:
        """Return every child announced by the opened session."""
        return self._student_ids

    def session_facts(self, student_id: str) -> SessionFacts:
        """Return the bootstrap facts cached while opening the session."""
        try:
            return self._session_facts[student_id]
        except KeyError as error:
            raise ConnectorChildMissingError(student_id) from error

    async def async_collect(  # noqa: C901, PLR0911, PLR0912 -- one arm per tier
        self,
        tier: Tier,
        student_id: str,
        *,
        priority: Priority,
    ) -> GatewayResult[Any]:
        """Collect one tier for one child through the guarded session."""
        if tier not in self.CAPABILITIES.tiers:
            raise ConnectorUnsupportedError(f"unsupported tier: {tier}")

        match tier:
            case Tier.TIMETABLE:
                today = self.today()
                tomorrow = today + timedelta(days=1)
                include_next_week = tomorrow.isocalendar()[1] != today.isocalendar()[1]
                return cast(
                    "GatewayResult[Any]",
                    await self.session.run(
                        str(tier),
                        priority,
                        lambda client: self.gateway.timetable(
                            client, include_next_week=include_next_week
                        ),
                        student_id=student_id,
                        cost=2 if include_next_week else 1,
                    ),
                )
            case Tier.HOMEWORK:
                return await self._run(
                    tier, student_id, priority, self.gateway.homework
                )
            case Tier.NEWS:
                return await self._run(tier, student_id, priority, self.gateway.news)
            case Tier.DISCUSSIONS:
                previous = self.previous_unread(student_id)
                result = cast(
                    "GatewayResult[Any]",
                    await self.session.run(
                        str(tier),
                        priority,
                        lambda client: self.gateway.discussions(
                            client, previous_unread=previous
                        ),
                        student_id=student_id,
                    ),
                )
                self.remember_unread(
                    student_id,
                    {
                        thread.id: (
                            thread.unread
                            if thread.id in result.facts.expanded
                            else previous.get(thread.id, 0)
                        )
                        for thread in result.facts.discussions
                    },
                )
                return result
            case Tier.MARKS:
                period = self._current_period(student_id)
                if period is None:
                    return GatewayResult(_empty_marks(), calls=0)
                return cast(
                    "GatewayResult[Any]",
                    await self.session.run(
                        str(tier),
                        priority,
                        _bind_marks(self.gateway, period),
                        student_id=student_id,
                        cost=2,
                    ),
                )
            case Tier.ATTENDANCE:
                period = self._current_period(student_id)
                if period is None:
                    return GatewayResult(_empty_attendance(), calls=0)
                return cast(
                    "GatewayResult[Any]",
                    await self.session.run(
                        str(tier),
                        priority,
                        _bind_attendance(self.gateway, period),
                        student_id=student_id,
                    ),
                )
            case Tier.EVALUATIONS:
                period = self._current_period(student_id)
                if period is None:
                    return GatewayResult(_empty_evaluations(), calls=0)
                return cast(
                    "GatewayResult[Any]",
                    await self.session.run(
                        str(tier),
                        priority,
                        _bind_evaluations(self.gateway, period),
                        student_id=student_id,
                    ),
                )
            case Tier.MENUS:
                return await self._run(tier, student_id, priority, self.gateway.menus)
            case Tier.STATIC:
                return await self._run(tier, student_id, priority, self.gateway.static)
            case Tier.HISTORY:
                return await self._history(student_id, priority)
            case _:
                raise ConnectorUnsupportedError(f"unsupported tier: {tier}")

    async def _run(
        self,
        tier: Tier,
        student_id: str,
        priority: Priority,
        function: Callable[[HardenedClient], Any],
    ) -> GatewayResult[Any]:
        """Run a one-request gateway operation."""
        return cast(
            "GatewayResult[Any]",
            await self.session.run(
                str(tier),
                priority,
                function,
                student_id=student_id,
            ),
        )

    async def _history(
        self, student_id: str, priority: Priority
    ) -> GatewayResult[HistoryFacts]:
        """Collect every configured closed period."""
        periods = self._closed_periods(student_id)
        if self._history_periods:
            periods = periods[-self._history_periods :]

        marks: list[MarksFacts] = []
        attendance: list[AttendanceFacts] = []
        evaluations: list[EvaluationsFacts] = []
        calls = 0
        for period in periods:
            marks_result = await self.session.run(
                str(Tier.HISTORY),
                priority,
                _bind_marks(self.gateway, period),
                student_id=student_id,
                cost=2,
            )
            marks.append(marks_result.facts)
            calls += marks_result.calls

            attendance_result = await self.session.run(
                str(Tier.HISTORY),
                priority,
                _bind_attendance(self.gateway, period),
                student_id=student_id,
            )
            attendance.append(attendance_result.facts)
            calls += attendance_result.calls

            evaluations_result = await self.session.run(
                str(Tier.HISTORY),
                priority,
                _bind_evaluations(self.gateway, period),
                student_id=student_id,
            )
            evaluations.append(evaluations_result.facts)
            calls += evaluations_result.calls

        return GatewayResult(
            HistoryFacts(
                marks=tuple(marks),
                attendance=tuple(attendance),
                evaluations=tuple(evaluations),
            ),
            calls=calls,
        )

    def _current_period(self, student_id: str) -> Period | None:
        """Return one child's current period."""
        return self.session_facts(student_id).current_period

    def _closed_periods(self, student_id: str) -> tuple[Period, ...]:
        """Return closed periods except the current one."""
        facts = self.session_facts(student_id)
        current = facts.current_period
        now = self.now()
        return tuple(
            period
            for period in facts.periods
            if period.is_closed(now) and (current is None or period.id != current.id)
        )

    def remember_unread(self, student_id: str, unread: Mapping[str, int]) -> None:
        """Store the per-thread unread counts for the next collection."""
        self._unread_by_student[student_id] = dict(unread)

    def previous_unread(self, student_id: str) -> dict[str, int]:
        """Return the unread counts from the previous collection."""
        return dict(self._unread_by_student.get(student_id, {}))

    async def async_close(self) -> None:
        """Close the session and its single-worker executor."""
        await self.session.close()

    def diagnostics(self) -> dict[str, Any]:
        """Return non-secret transport diagnostics."""
        return {
            "limiter": self.limiter.snapshot_counters(),
            "session": self.session.diagnostics(),
        }


def _client_student_ids(client: Any) -> tuple[str, ...]:
    """Return child identifiers without leaking the client to the event loop."""
    children: tuple[str, ...] = tuple(str(child.id) for child in client.children)
    if children:
        return children
    return (str(client.info.id),)


def _empty_marks() -> MarksFacts:
    """Return the typed empty value for an unknown current period."""
    return MarksFacts(
        period_id="",
        period_index=0,
        grades=(),
        averages=(),
        overall_average=None,
        class_overall_average=None,
        report=None,
    )


def _empty_attendance() -> AttendanceFacts:
    """Return the typed empty value for an unknown current period."""
    return AttendanceFacts(period_id="", absences=(), delays=(), punishments=())


def _empty_evaluations() -> EvaluationsFacts:
    """Return the typed empty value for an unknown current period."""
    return EvaluationsFacts(period_id="", evaluations=())


def _bind_marks(
    gateway: PronoteGateway, period: Period
) -> Callable[[HardenedClient], Any]:
    """Bind a period without closing over a changing loop variable."""

    def call(client: HardenedClient) -> Any:
        return gateway.marks(client, period, with_report=True)

    return call


def _bind_attendance(
    gateway: PronoteGateway, period: Period
) -> Callable[[HardenedClient], Any]:
    """Bind a period into an attendance call."""

    def call(client: HardenedClient) -> Any:
        return gateway.attendance(client, period)

    return call


def _bind_evaluations(
    gateway: PronoteGateway, period: Period
) -> Callable[[HardenedClient], Any]:
    """Bind a period into an evaluations call."""

    def call(client: HardenedClient) -> Any:
        return gateway.evaluations(client, period)

    return call
