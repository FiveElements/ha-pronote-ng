"""``PronoteAccount``: one per config entry, and the only thing that ticks.

The master heartbeat asks the scheduler what is due, then runs those tiers in
one session, spaced by the limiter (§5.1). Nothing else in the integration owns
a timer that reaches the network.

The order of operations inside a tick is not incidental:

1. tiers are served in priority order, because the limiter sheds by priority and
   serving out of order would drop the wrong ones (annexe B §2.4);
2. a collection that succeeds publishes its snapshot *and then* runs the delta
   detector, so an event never fires for data the entities do not yet hold;
3. a collection that is deferred moves its deadline and keeps its snapshot;
4. a collection that fails flips the coordinator's success flag and keeps its
   snapshot too -- "I know, but it is old" beats "I no longer know" on school
   data (§4.4).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .child_keys import pair
from .const import (
    CONF_ACCOUNT_PIN,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_CLIENT_IDENTIFIER,
    CONF_DEVICE_NAME,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_UUID,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MASTER_TICK,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_STALE_AFTER,
    DOMAIN,
    ISSUE_ACCOUNT_UNREADABLE,
    ISSUE_BOOTSTRAP_FAILED,
    ISSUE_DAILY_CAP_NEAR,
    ISSUE_INVALID_CREDENTIALS,
    ISSUE_MFA_REQUIRED,
    OPT_CONNECT_TIMEOUT,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_MASTER_TICK,
    OPT_READ_TIMEOUT,
    OPT_SESSION_STRATEGY,
    OPT_STALE_AFTER,
    OPT_WRITE_OPERATIONS_ENABLED,
    LoginMode,
    Priority,
    SessionStrategy,
    Tier,
)
from .coordinator import PronoteTierCoordinator
from .delta import DeltaDetector
from .gateway import PronoteGateway
from .login_guard import limiter_state_store
from .models import Period, SessionFacts, Snapshot, Student
from .options import (
    bounded_option,
    build_rate_limit_config,
    tier_enabled,
    tier_intervals,
)
from .ratelimit import RateLimiter, TierDeferred
from .scheduler import FetchScheduler, default_plans
from .session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
    SerialExecutor,
    SessionCredentials,
    SessionManager,
)
from .urls import public_url

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from homeassistant.config_entries import ConfigEntry

    from .delta import DeltaEvent

_LOGGER: Final = logging.getLogger(__name__)

#: Signal fired on the Home Assistant bus for every detected change. The
#: ``event`` entities listen for it; nothing else should.
SIGNAL_DELTA: Final = f"{DOMAIN}_delta"

#: Tiers scoped to a period. They need to know *which* period, and the closed
#: ones are the ``history`` tier's business, not theirs.
PERIOD_TIERS: Final = frozenset({Tier.MARKS, Tier.ATTENDANCE, Tier.EVALUATIONS})


@dataclass(slots=True)
class TierRecord:
    """Bookkeeping the diagnostic entities read."""

    last_success: Any | None = None
    last_duration_ms: int | None = None
    last_calls: int = 0
    consecutive_failures: int = 0


@dataclass(slots=True)
class AccountState:
    """Everything about the account that entities may read synchronously."""

    students: tuple[Student, ...] = ()
    periods: tuple[Period, ...] = ()
    current_period: Period | None = None
    records: dict[Tier, TierRecord] = field(default_factory=dict)
    last_collection_tier: Tier | None = None


class PronoteAccount:
    """Orchestrates one PRONOTE account: session, budget, cadence, snapshots."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.state = AccountState()

        options = entry.options
        timezone = options.get(OPT_ESTABLISHMENT_TIMEZONE, str(hass.config.time_zone))
        self.gateway = PronoteGateway(timezone)

        # Clamped, like everything else read out of the options mapping. A
        # stored read timeout of 0 makes every request time out before it is
        # sent, which presents as a total outage with a healthy server, and
        # the options page -- the only place that bounds these fields -- is
        # not on the path a restored or hand-edited entry takes.
        self._read_timeout = bounded_option(
            options, OPT_READ_TIMEOUT, DEFAULT_READ_TIMEOUT
        )
        self._connect_timeout = float(
            bounded_option(options, OPT_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT)
        )
        self.stale_after = int(
            bounded_option(options, OPT_STALE_AFTER, DEFAULT_STALE_AFTER)
        )

        self.limiter = RateLimiter(
            build_rate_limit_config(options),
            clock=time.monotonic,
            now=self.gateway.now,
        )
        # A hold outlives the object that opened it. `async_setup_entry` is
        # retried by Home Assistant with a backoff capped at eighty seconds,
        # and each attempt built a limiter that had never heard of the
        # hour-long bootstrap hold the previous attempt had just opened -- so a
        # school down for a weekend was asked for a fresh login every eighty
        # seconds for sixty-one hours, against a configured ceiling of
        # twenty-four a day, while `calls_today` reported five (§B2).
        self.limiter.import_state(limiter_state_store(hass).get(entry.entry_id, {}))
        self.scheduler = FetchScheduler(
            default_plans(tier_intervals(options), tier_enabled(options)),
            clock=time.monotonic,
            now=self.gateway.now,
        )
        # An options save reloads the entry, which builds a new scheduler whose
        # every deadline is unset -- making all ten tiers immediately due. The
        # previous schedule is handed across the reload through `hass.data`, so
        # nudging one interval no longer costs a login plus a full batch (§7.3).
        self.scheduler.import_state(_saved_schedule(hass).get(entry.entry_id, {}))
        self.executor = SerialExecutor(entry.entry_id, self._read_timeout)
        self.session = SessionManager(
            entry_id=entry.entry_id,
            credentials=_credentials_from_entry(entry),
            limiter=self.limiter,
            executor=self.executor,
            strategy=SessionStrategy(
                options.get(OPT_SESSION_STRATEGY, DEFAULT_SESSION_STRATEGY)
            ),
            now=self.gateway.now,
            clock=time.monotonic,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            on_credentials_rotated=self._async_persist_credentials,
        )
        self.delta = DeltaDetector()

        # A coordinator per tier, `session` included. The session tier costs
        # no request -- periods, class and establishment come free with the
        # login -- but its entities still need something to subscribe to.
        self.coordinators: dict[Tier, PronoteTierCoordinator] = {
            tier: PronoteTierCoordinator(hass, entry, tier) for tier in Tier
        }

        self._selected_children: tuple[str, ...] = tuple(
            entry.data.get(CONF_CHILDREN) or ()
        )
        #: PRONOTE resource identifier -> the key this integration minted for
        #: that child. Filled by `_async_pair_children` during set-up, before
        #: any platform is forwarded, because `entity.py` reads it to build
        #: every `unique_id`. Empty until then, and deliberately not defaulted
        #: to the resource identifier: see `stable_key`.
        self._child_keys: dict[str, str] = {}
        self._unsub_tick: Any | None = None
        self._tick_lock = asyncio.Lock()
        self._unread_by_student: dict[str, dict[str, int]] = {}
        self._shutting_down: bool = False
        #: Registry id of this entry's account device, filled in by
        #: `async_setup_entry` right after it creates that device and before the
        #: platforms are forwarded. Child devices need it to declare their
        #: parent: `DeviceInfo` takes `via_device_id` -- a registry id -- and no
        #: longer the identifier tuple, so the link cannot be expressed without
        #: having created the parent first.
        self.account_device_id: str | None = None

    # -- properties --------------------------------------------------------

    @property
    def write_enabled(self) -> bool:
        """Whether write operations are permitted (false by default, §8.3)."""
        return bool(self.entry.options.get(OPT_WRITE_OPERATIONS_ENABLED, False))

    @property
    def students(self) -> tuple[Student, ...]:
        """The children this entry follows."""
        return self.state.students

    @property
    def establishment_name(self) -> str:
        """What to call the account device.

        The establishment of the first child that names one. A parent account
        can hold children in two establishments, in which case this is
        arbitrary -- but a device has one name, and naming it after one of the
        two schools is more useful than naming it "PRONOTE". The fallback
        exists for the window before the first login has told us anything.
        """
        return (
            next(
                (
                    student.establishment
                    for student in self.state.students
                    if student.establishment
                ),
                None,
            )
            or "PRONOTE"
        )

    # -- setup and teardown ------------------------------------------------

    async def async_setup(self) -> None:
        """Log in once, learn the account's shape, and start the heartbeat."""
        await self._async_load_session_facts()

        # A master tick of 0 would schedule the next tick in the past, and
        # `async_track_point_in_time` fires an overdue callback immediately
        # -- an unthrottled loop rescheduling itself for ever.
        interval = int(
            bounded_option(self.entry.options, OPT_MASTER_TICK, DEFAULT_MASTER_TICK)
        )
        self._unsub_tick = async_track_time_interval(
            self.hass,
            self._async_tick,
            timedelta(minutes=interval),
            name=f"{DOMAIN} master tick",
        )
        # Collect immediately rather than waiting a full tick: a fresh install
        # showing nothing for five minutes reads as broken.
        self.hass.async_create_task(
            self._async_tick(), name=f"{DOMAIN} first collection"
        )

    async def async_unload(self) -> None:
        """Stop the heartbeat and release the session.

        Never joins the worker thread from the event loop: pronotepy's transport
        can be mid-request, and joining would freeze the reload. A leaked thread
        beats a frozen instance (§3.2).
        """
        self._shutting_down = True
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        _saved_schedule(self.hass)[self.entry.entry_id] = self.scheduler.export_state()
        limiter_state_store(self.hass)[self.entry.entry_id] = (
            self.limiter.export_state()
        )
        await self.session.close()

    async def _async_load_session_facts(self) -> None:
        """Read what the login itself supplies -- students, periods, class.

        Zero calls beyond the login: ``periods``, ``class_name`` and
        ``establishment`` live in ``func_options`` and
        ``parametres_utilisateur``, already in memory once authenticated
        (annexe A §2).
        """
        students: list[Student] = []
        session_snapshots: list[SessionFacts] = []
        periods: tuple[Period, ...] = ()
        current: Period | None = None

        for student_id in await self._async_student_ids():
            facts = await self.session.run(
                str(Tier.SESSION),
                Priority.CRITICAL,
                self.gateway.session_facts,
                student_id=student_id,
                cost=0,
            )
            students.append(facts.facts.student)
            session_snapshots.append(facts.facts)
            periods = facts.facts.periods
            current = facts.facts.current_period

        self.state.students = tuple(students)
        self.state.periods = periods
        self.state.current_period = current

        # Before the platforms are forwarded, because every `unique_id` is
        # built from what this establishes.
        self._async_pair_children(students)

        session_coordinator = self.coordinators[Tier.SESSION]
        for student, facts in zip(students, session_snapshots, strict=True):
            session_coordinator.publish(
                student.id,
                Snapshot(
                    data=facts,
                    fetched_at=self.gateway.now(),
                    tier=Tier.SESSION,
                    calls=0,
                    student_id=student.id,
                ),
            )

    @callback
    def _async_pair_children(self, students: Sequence[Student]) -> None:
        """Give every announced child the key this integration owns.

        The whole point of `child_keys` is that PRONOTE's resource identifier
        rotates, so this runs at every set-up rather than once: the table in
        the config entry is the durable artefact, and re-pairing against it is
        the normal mode of operation.

        Writing the entry here is safe and the timing is not accidental.
        `async_setup_entry` attaches the update listener that reloads the entry
        *after* `async_setup` returns, so this write cannot start a reload
        loop -- and it only writes when the table actually changed, which is
        the same guard `_async_persist_credentials` needs for the opposite
        reason.
        """
        stored = list(self.entry.data.get(CONF_CHILD_KEYS) or ())
        keys, table, notes = pair(
            stored, [(student.id, student.name) for student in students]
        )
        self._child_keys = keys
        for note in notes:
            # Keys only, never a name: this lands in the log users attach to
            # public issues (§8.2). It is logged at all because the silence
            # here is what hid the duplicate-device defect for as long as it
            # existed.
            _LOGGER.info("child identity: %s", note)
        if table != stored:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_CHILD_KEYS: table}
            )

        self._async_adopt_existing_registry_rows()

    @callback
    def _async_adopt_existing_registry_rows(self) -> None:
        """Re-point rows created against PRONOTE's rotating identifier.

        Entities and devices that predate the minted keys carry
        ``<entry_id>_46#<signature>...`` in their ``unique_id``. Rewriting that
        **in place** is the whole difference between a fix and a second
        breakage: `async_update_entity` keeps the registry row, so the
        ``entity_id``, the device, a name the user set by hand, the area and
        the labels all survive. Creating new entities and leaving the old ones
        would *be* the defect, performed deliberately -- and it would break
        every dashboard badge and tile, which can only be wired to a literal
        ``entity_id``.

        Run here rather than from a version-numbered migration handler, and
        that is a deliberate choice: a migration runs before any login, so it
        cannot know which children the account announces *now* -- and on an
        instance that already suffered the defect the registry holds two
        generations for one child, with no way to tell from the registry alone
        which is alive. Pairing has just answered that question, so this runs
        immediately after it and only ever touches children the account
        actually announced. The other generation is left exactly as it is: its
        entities are dead either way, and deleting registry rows on a user's
        behalf is not this integration's decision.

        Idempotent by construction -- after the first pass nothing matches the
        old prefix -- so it also repairs an entry whose identifier rotated
        between one start and the next.
        """
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        entry_id = self.entry.entry_id
        rows = er.async_entries_for_config_entry(entities, entry_id)

        for resource_id, minted in self._child_keys.items():
            if resource_id == minted:
                continue
            self._async_adopt_device(devices, entry_id, resource_id, minted)
            self._async_adopt_entities(entities, rows, entry_id, resource_id, minted)

    @callback
    def _async_adopt_device(
        self, devices: dr.DeviceRegistry, entry_id: str, resource_id: str, minted: str
    ) -> None:
        """Move one child device onto its minted identifier."""
        was = (DOMAIN, f"{entry_id}_{resource_id}")
        now = (DOMAIN, f"{entry_id}_{minted}")
        # `async_get_device_by_identifier` and not `async_get_device`: the
        # latter is deprecated because a device identifier is no longer unique
        # across config entries, and it *raises* under the test harness. That
        # is also why it takes the entry id -- which is the more correct
        # question anyway, since two accounts may follow the same child.
        device = devices.async_get_device_by_identifier(was, entry_id)
        if device is None:
            return
        taken = devices.async_get_device_by_identifier(now, entry_id)
        if taken is not None:
            if taken.id != device.id:
                # Both generations exist as devices. Claiming the identifier
                # would collide, and merging them would decide for the user
                # which of two devices keeps their custom name.
                _LOGGER.warning(
                    "a device already carries the minted identifier for %s, so "
                    "the older one is left as it is; it will not update again "
                    "and can be deleted from the device page",
                    minted,
                )
            return
        devices.async_update_device(device.id, new_identifiers={now})
        _LOGGER.info(
            "adopted the existing device for %s, keeping its name, area and labels",
            minted,
        )

    @callback
    def _async_adopt_entities(
        self,
        entities: er.EntityRegistry,
        rows: list[er.RegistryEntry],
        entry_id: str,
        resource_id: str,
        minted: str,
    ) -> None:
        """Rewrite the unique id of every entity of one child, in place."""
        was = f"{entry_id}_{resource_id}_"
        now = f"{entry_id}_{minted}_"
        adopted = 0
        for row in rows:
            if not row.unique_id.startswith(was):
                continue
            wanted = now + row.unique_id[len(was) :]
            taken = entities.async_get_entity_id(row.domain, DOMAIN, wanted)
            if taken is not None and taken != row.entity_id:
                # Refusing beats raising: `async_update_entity` would abort the
                # whole set-up over one row, and an entry that will not load is
                # worse than one entity left behind.
                _LOGGER.warning(
                    "cannot re-point %s: %s already holds the identity it would take",
                    row.entity_id,
                    taken,
                )
                continue
            entities.async_update_entity(row.entity_id, new_unique_id=wanted)
            adopted += 1
        if adopted:
            _LOGGER.info(
                "re-pointed %d existing entities of %s onto an identity that "
                "no longer follows PRONOTE's rotating identifier; every "
                "entity_id, custom name, area and label is unchanged",
                adopted,
                minted,
            )

    def student_id_for_key(self, key: str) -> str | None:
        """Turn a minted key back into the identifier PRONOTE announced.

        The reverse of `stable_key`, needed at every boundary where a *device*
        is the input: a device identifier carries the minted key, while the
        coordinators, the gateway and the bus events are all keyed by the
        session's resource identifier. Three call sites need it -- the
        diagnostics dump, the device triggers and the service target
        resolution -- and each is a place where a device that Home Assistant
        stored months ago has to be matched to the child of this session.

        A value that is *already* a known resource identifier is returned
        unchanged, which is what makes a device created before the keys
        existed keep working between the upgrade and the migration.
        """
        if key in self._child_keys:
            return key
        for student_id, minted in self._child_keys.items():
            if minted == key:
                return student_id
        return None

    def stable_key(self, student_id: str) -> str:
        """The minted key for a child, for use in a `unique_id`.

        Falls back to the resource identifier only if pairing never ran for
        this child, which cannot happen through `async_setup`: every student
        in `state.students` was paired before the platforms were forwarded.
        It is a loud fallback rather than an exception because raising here
        would fail the whole entry -- losing every child's entities -- to
        protect the identity of one. The error says what to look for, since
        the consequence is precisely the defect this replaced: an entity whose
        identity follows a value PRONOTE rotates.
        """
        key = self._child_keys.get(student_id)
        if key is None:
            _LOGGER.error(
                "no minted key for a child that entities are being built for; "
                "falling back to the PRONOTE resource identifier, which is "
                "not stable between sessions and will orphan those entities "
                "the next time it rotates. This is an integration bug: "
                "pairing runs before the platforms are forwarded"
            )
            return student_id
        return key

    async def _async_student_ids(self) -> tuple[str, ...]:
        """Which children to follow: those the user selected, or all of them."""
        # Touching the session here is what forces the login, so it is also
        # where a bad password surfaces during setup.
        #
        # The closure returns the identifiers, not the client. Returning the
        # client handed a live `HardenedClient` back to the event loop, which is
        # exactly the shape §3.1 forbids: the value was dropped immediately, but
        # it is the doorway to `Erreur.G = 22`, and `_reconcile` already reads
        # an attribute off whatever comes back.
        available: tuple[str, ...] = await self.session.run(
            str(Tier.SESSION),
            Priority.CRITICAL,
            _client_student_ids,
            cost=0,
        )
        if not self._selected_children:
            return available
        chosen = tuple(sid for sid in available if sid in self._selected_children)
        if not chosen:
            # Following every child is the right recovery -- refusing to
            # collect anything because a stored identifier went stale would
            # take the whole integration down -- but doing it *silently* is
            # what let a serious defect hide.
            #
            # A PRONOTE resource identifier is written `46#<signature>`, and
            # that signature is **not stable between sessions**. The
            # identifiers stored at configuration time therefore stop matching,
            # this fallback quietly follows the children it was handed instead,
            # and because an entity's `unique_id` embeds the identifier, a
            # whole new device and one entity per description appear while the
            # previous generation is orphaned in the registry -- every
            # dashboard, automation and helper pointing at it dead, with
            # nothing logged anywhere. `config_flow._account_identity` already
            # learned that lesson for the *account* id and drops the signature
            # before comparing; nobody carried it here.
            _LOGGER.warning(
                "none of the %d selected children match the %d the account "
                "now announces, so all of them are followed. PRONOTE resource "
                "identifiers are not stable between sessions, so this is "
                "expected to happen and is recovered from -- but it also means "
                "the entities of the previously followed children are no "
                "longer updated. Re-select the children in the options to "
                "settle the selection on what the account announces today",
                len(self._selected_children),
                len(available),
            )
        return chosen or available

    def _stopping(self) -> bool:
        """Whether teardown has begun.

        A method and not a bare attribute read, because a batch awaits between
        tiers and ``async_unload`` sets the flag from another task in the
        meantime -- so the value genuinely changes across the loop. Read as
        ``self._shutting_down``, a type checker narrows it to ``False`` after
        the guard at the top of the tick and calls the in-loop check dead code,
        which is the one thing it is not.
        """
        return self._shutting_down

    # -- the master tick ---------------------------------------------------

    async def _async_tick(self, _now: Any = None) -> None:
        """Serve every due tier, once, in priority order."""
        if self._stopping():
            return
        if self._tick_lock.locked():
            # The previous batch is still running. Skipping is correct: the
            # deadlines have not moved, so the next tick picks the same tiers up
            # -- whereas queueing would let a slow server build a backlog that
            # then arrives as the burst the single heartbeat exists to prevent.
            _LOGGER.debug("skipping a tick: the previous batch is still running")
            return

        async with self._tick_lock:
            due = self.scheduler.due()
            if not due:
                return
            _LOGGER.debug("batch: %s", ", ".join(str(tier) for tier in due))
            # Declaring the batch is load-bearing twice over. The limiter needs
            # it so that entering quiet hours halfway through lets the batch
            # finish rather than refusing its remaining tiers (annexe B §8);
            # the session needs it because `per_batch` means one login per
            # *batch*, and without a boundary it meant one login per
            # `(child, tier)` -- 36 logins in a single tick for a two-child
            # account with three closed periods, against a cap of 24.
            self.limiter.begin_batch()
            self.session.begin_batch()
            try:
                for tier in due:
                    if self._stopping():
                        return
                    await self._async_collect(tier)
            finally:
                self.limiter.end_batch()
            self._async_sync_issues()

    async def async_request_tick(self) -> None:
        """Serve what is due now, without waiting for the next heartbeat.

        The refresh button and the ``refresh`` service both land here after
        boosting the tiers they want. Nothing is bypassed: the scheduler still
        decides what is due, the limiter still spaces the calls, and
        ``_tick_lock`` still refuses a second batch -- so a button pressed ten
        times in a minute costs one batch, not ten (§5.1).
        """
        if self._stopping() or self._tick_lock.locked():
            return
        self.hass.async_create_task(
            self._async_tick(), name=f"{DOMAIN} requested collection"
        )

    async def _async_collect(self, tier: Tier) -> None:
        """Collect one tier for every followed student."""
        coordinator = self.coordinators.get(tier)
        if coordinator is None:
            return

        record = self.state.records.setdefault(tier, TierRecord())
        started = time.monotonic()
        calls = 0
        events: list[tuple[str, DeltaEvent]] = []
        succeeded = False

        for student in self.state.students:
            try:
                snapshot, used, detected = await self._async_collect_student(
                    tier, student.id
                )
            except TierDeferred as deferred:
                self.scheduler.defer(tier, deferred.retry_after)
                _LOGGER.debug(
                    "tier %s deferred for student %s: %s",
                    tier,
                    student.id,
                    deferred.reason,
                )
                return
            except (
                LoginRefused,
                InvalidCredentials,
                MfaRequired,
                BootstrapFailed,
                AccountUnreadable,
                IntegrationFault,
            ) as error:
                # Authentication problems are the account's problem, not the
                # tier's: they open a repair and stop the batch rather than
                # being retried per tier.
                record.consecutive_failures += 1
                coordinator.note_failure(error)
                # `retry_delay()`, not `backoff_delay()`. The latter is zero
                # until there have been *consecutive* failures, which is
                # exactly the case for every authentication refusal -- bad
                # credentials, a demanded PIN, an unreadable bootstrap all
                # leave that counter at zero. Deferring by zero seconds made
                # the tier due again on the very next tick, so ten tiers
                # attempted ten logins a tick: the one gesture that gets an
                # address suspended.
                self.scheduler.mark_failed(tier, self.limiter.retry_delay())
                _LOGGER.debug("authentication blocked tier %s: %s", tier, error)
                return
            except Exception as error:  # noqa: BLE001 -- top of one cycle (§5.3)
                record.consecutive_failures += 1
                coordinator.note_failure(error)
                self.scheduler.mark_failed(tier, self.limiter.retry_delay())
                _LOGGER.debug(
                    "tier %s failed for student %s", tier, student.id, exc_info=True
                )
                return

            calls += used
            succeeded = True
            coordinator.publish(student.id, snapshot)
            events.extend((student.id, event) for event in detected)

        if not succeeded:
            # No student produced a snapshot -- normally because the account
            # has none yet. The tier still has to be settled, or its `boosted`
            # flag survives every tick and the "raised for exactly one tick"
            # invariant quietly stops holding.
            #
            # `mark_failed` and not `defer`, because "produced nothing" is a
            # failure however politely it arrived: a bare deferral by one
            # back-off base is shorter than the master tick, so a tier in this
            # state was retried at every tick for as long as it lasted. The
            # floor lives in `mark_failed`, which is the only difference
            # between the two.
            self.scheduler.mark_failed(tier, self.limiter.retry_delay())
            return

        self.scheduler.mark_collected(tier)
        record.consecutive_failures = 0
        record.last_success = self.gateway.now()
        record.last_duration_ms = int((time.monotonic() - started) * 1000)
        record.last_calls = calls
        self.state.last_collection_tier = tier

        # Events go out only after the snapshots are published, so an
        # automation reacting to "a grade arrived" finds the sensor already
        # holding it.
        for student_id, event in events:
            self._async_fire(student_id, event)

    async def _async_collect_student(
        self, tier: Tier, student_id: str
    ) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
        """Run one ``(student, tier)`` unit and derive its events."""
        from .tiers import collect_tier  # noqa: PLC0415 -- avoids an import cycle

        return await collect_tier(self, tier, student_id)

    @callback
    def _async_fire(self, student_id: str, event: DeltaEvent) -> None:
        """Hand one detected change to the matching ``event`` entity."""
        self.hass.bus.async_fire(
            SIGNAL_DELTA,
            {
                "entry_id": self.entry.entry_id,
                "student_id": student_id,
                "entity_key": event.entity_key,
                "event_type": event.event_type,
                "attributes": event.attributes,
            },
        )

    # -- snapshots and staleness ------------------------------------------

    def snapshot(self, tier: Tier, student_id: str) -> Snapshot[Any] | None:
        """The latest successful snapshot for a (tier, student) pair."""
        coordinator = self.coordinators.get(tier)
        if coordinator is None:
            return None
        return coordinator.snapshot_for(student_id)

    def is_stale(self, tier: Tier) -> bool:
        """Whether a tier's data has aged past ``stale_after`` intervals.

        The session tier is never stale: it holds what the login handed us --
        the child, the class, the period list -- which changes once a school
        year, not on an interval. Ageing it would make the period sensor go
        unavailable overnight for no reason.

        For every other tier the quiet hours are excused from the age. With
        quiet hours on -- the default -- an eight-hour night exceeds
        ``stale_after`` times the interval of every fast tier, so without this
        the timetable, homework, news and discussion entities went
        ``unavailable`` at 06:00 every single morning, and §2.5 is explicit
        that an unavailable entity breaks automations.
        """
        if tier is Tier.SESSION:
            return False
        return self.scheduler.is_stale(
            tier, self.stale_after, excused=self._quiet_seconds_since(tier)
        )

    def _quiet_seconds_since(self, tier: Tier) -> float:
        """Quiet-hours time that elapsed since a tier was last collected."""
        collected_at = self.scheduler.last_collected_at(tier)
        if collected_at is None:
            return 0.0
        return self.limiter.quiet_seconds_between(collected_at, self.gateway.now())

    def has_data(self, tier: Tier, student_id: str) -> bool:
        """Whether a (tier, student) pair ever produced a snapshot."""
        return self.snapshot(tier, student_id) is not None

    def remember_unread(self, student_id: str, unread: Mapping[str, int]) -> None:
        """Store the per-thread unread counts the next cycle compares against."""
        self._unread_by_student[student_id] = dict(unread)

    def previous_unread(self, student_id: str) -> dict[str, int]:
        """The unread counts from the previous discussions collection."""
        return dict(self._unread_by_student.get(student_id, {}))

    def periods_for(self, tier: Tier) -> tuple[Period, ...]:
        """Which periods a period-scoped tier should read.

        ``history`` walks the **closed** periods and nothing else: a closed
        period cannot change, so re-reading it every three hours spends calls on
        a constant result (§5.2).
        """
        now = self.gateway.now()
        if tier is Tier.HISTORY:
            return tuple(
                period
                for period in self.state.periods
                if period.is_closed(now)
                and (
                    self.state.current_period is None
                    or period.id != self.state.current_period.id
                )
            )
        if self.state.current_period is not None:
            return (self.state.current_period,)
        return ()

    # -- credential rotation ----------------------------------------------

    async def _async_persist_credentials(self, credentials: Mapping[str, Any]) -> None:
        """Write the rotated credentials back into the config entry.

        The mobile token rotates at every ``Authentification``; not re-saving it
        means losing access at the next start (§7.2). The write is why the lazy
        session strategy matters beyond arithmetic: one login a day is one window
        a day in which an abrupt shutdown between the server's rotation and this
        write leaves a dead token.
        """
        data = dict(self.entry.data)
        changed = False
        for key in ("username", "password", "client_identifier", "uuid"):
            value = credentials.get(key)
            if value is not None and data.get(key) != value:
                data[key] = value
                changed = True
        if changed:
            self.hass.config_entries.async_update_entry(self.entry, data=data)

    # -- repair issues -----------------------------------------------------

    @callback
    def _async_sync_issues(self) -> None:
        """Open or close the repair issues the limiter's state implies."""
        if self.limiter.cap_warning_open:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_DAILY_CAP_NEAR}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DAILY_CAP_NEAR,
                translation_placeholders={
                    "calls": str(self.limiter.calls_today),
                    "cap": str(self.limiter.config.max_requests_per_day),
                },
            )
        else:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_DAILY_CAP_NEAR}_{self.entry.entry_id}",
            )

    @callback
    def async_open_credentials_issue(self) -> None:
        """Ask the user to check the credentials, and stop trying."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_INVALID_CREDENTIALS}_{self.entry.entry_id}",
            # Not fixable *here*. `ConfigEntryAuthFailed` already starts the
            # re-authentication flow, which is the only thing that can fix
            # this; with `is_fixable=True` and no `repairs.py`, Home Assistant
            # falls back to `ConfirmRepairFlow` -- an empty dialog whose
            # "submit" deletes the issue without doing anything at all.
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INVALID_CREDENTIALS,
            translation_placeholders={
                "attempts": str(self.limiter.failed_logins_last_hour),
                "until": _format_until(self.limiter.hold_until_wallclock),
            },
            data={"entry_id": self.entry.entry_id},
        )

    @callback
    def async_open_bootstrap_issue(self) -> None:
        """Report an impossible bootstrap **without naming a cause**.

        pronotepy decides an address is suspended with ``if "IP" in html`` --
        two capitals anywhere in the page. Specification v1 built an hour-long
        hold and a repair with "explicit text" on that, which would have told
        parents their home address was banned because a school wrote "Espace IP"
        in a footer. This issue enumerates the possible causes and picks none
        (§6.3).
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_BOOTSTRAP_FAILED}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_BOOTSTRAP_FAILED,
            translation_placeholders={
                # Trimmed, not raw. A repair issue's placeholders are written to
                # `.storage` and rendered in the Repairs panel, so they travel
                # into screenshots, support sessions and backups; and what a
                # parent pastes into the address field is regularly a deep link
                # or an ENT bounce carrying a ticket (§8.2).
                "url": public_url(self.entry.data.get(CONF_PRONOTE_URL)),
                "until": _format_until(self.limiter.hold_until_wallclock),
            },
        )

    @callback
    def async_open_unreadable_issue(self) -> None:
        """Report a login whose response could not be decoded.

        A separate repair from ``bootstrap_failed`` because the diagnosis is
        genuinely different and so is the remedy: the address is right, the
        credentials are right, and something in this establishment's response
        is outside what the pinned ``pronotepy`` handles. The user cannot fix
        that, so the text asks for a report rather than for a correction.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_ACCOUNT_UNREADABLE}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_ACCOUNT_UNREADABLE,
            translation_placeholders={
                "url": public_url(self.entry.data.get(CONF_PRONOTE_URL)),
            },
        )

    @callback
    def async_open_mfa_issue(self) -> None:
        """Ask for the 2FA PIN, which is deliberately never stored (§8.1)."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_MFA_REQUIRED}_{self.entry.entry_id}",
            # Same reasoning as `invalid_credentials`: the PIN can only be
            # supplied through the re-authentication flow, and an empty confirm
            # dialog that silently closes the issue would be worse than a plain
            # message telling the user where to go.
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_MFA_REQUIRED,
            data={"entry_id": self.entry.entry_id},
        )

    # -- diagnostics -------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret facts for the diagnostic entities and download.

        The iCal URL, the identity, the tokens and the credentials are absent by
        construction: none of them is ever stored in a snapshot, so there is
        nothing here to redact (§8.2, §8.4).
        """
        return {
            "limiter": self.limiter.snapshot_counters(),
            "session": self.session.diagnostics(),
            "scheduler": self.scheduler.diagnostics(),
            "students": [
                {
                    "id_hash": _short_hash(student.id),
                    "class_name": student.class_name,
                    # Whether PRONOTE says this child has a profile photo, which
                    # is what decides if the `image` platform creates an entity
                    # at all. Without it, "no photo entity" is indistinguishable
                    # from "the flag is being misread", and the first is a
                    # legitimate school setting while the second is a bug.
                    "has_photo": student.has_photo,
                }
                for student in self.state.students
            ],
            "periods": [
                {"index": period.index, "name": period.name}
                for period in self.state.periods
            ],
            "stale_after": self.stale_after,
            "write_enabled": self.write_enabled,
        }


def _client_student_ids(client: Any) -> tuple[str, ...]:
    """The children's identifiers, or -- on a student account -- the student's.

    A module-level function rather than a lambda inside the call, because the
    thing it must *not* do is return the client itself: that hands a live
    `HardenedClient` back to the event loop, which is the shape §3.1 forbids
    and the doorway to `Erreur.G = 22`. Named, it is a seam a reader can check.
    """
    children: tuple[str, ...] = tuple(str(child.id) for child in client.children)
    if children:
        return children
    return (str(client.info.id),)


def _saved_schedule(hass: HomeAssistant) -> dict[str, dict[str, float]]:
    """Per-entry collection schedules, kept across a reload.

    In ``hass.data`` under its own key rather than in the config entry: this is
    runtime state with no business being persisted to disk, and it is only
    meaningful within one process because the values are monotonic.
    """
    store: dict[str, dict[str, float]] = hass.data.setdefault(f"{DOMAIN}_schedule", {})
    return store


def _credentials_from_entry(entry: ConfigEntry) -> SessionCredentials:
    """Read the credentials out of the config entry."""
    data = entry.data
    return SessionCredentials(
        login_mode=LoginMode(data.get(CONF_LOGIN_MODE, LoginMode.CREDENTIALS)),
        pronote_url=str(data.get(CONF_PRONOTE_URL, "")),
        username=str(data.get("username", "")),
        password=str(data.get("password", "")),
        uuid=str(data.get(CONF_UUID, "")),
        client_identifier=data.get(CONF_CLIENT_IDENTIFIER),
        device_name=data.get(CONF_DEVICE_NAME),
        # Deliberately not persisted: read only if a re-authentication flow just
        # supplied it in this session (§8.1).
        account_pin=data.get(CONF_ACCOUNT_PIN),
        ent_provider=data.get(CONF_ENT),
    )


def _format_until(moment: Any) -> str:
    """Format a hold's end for a repair issue's placeholders."""
    if moment is None:
        return ""
    return dt_util.as_local(moment).isoformat(timespec="minutes")


def _short_hash(value: str) -> str:
    """A truncated fingerprint, never the identifier itself.

    A diagnostic must not hand anybody the material to replay a session
    (annexe A §7).
    """
    from hashlib import blake2s  # noqa: PLC0415 -- only needed for diagnostics

    return blake2s(value.encode(), digest_size=4).hexdigest()
