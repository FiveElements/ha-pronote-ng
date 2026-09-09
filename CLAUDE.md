# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Home Assistant custom integration for PRONOTE (French school platform). Domain
`pronote_ng` — deliberately **not** `pronote`, so it can cohabit with another
custom integration that already owns that domain. Repo `ha-pronote-ng`,
displayed name "Pronote NG".

`CONTRIBUTING.md` is the human-facing version of much of this and is worth
reading in full before a substantial change. `docs/SPECIFICATION.md` and
`docs/annexe-b-rate-limit.md` are cited by number throughout the source
(`§5.1`, `annexe B §2.4`); those references are real and resolvable — follow
them rather than guessing why a value is what it is.

## Two rules that override convenience

**No real credential anywhere in this repo** — no PRONOTE username, password,
2FA PIN, `jetonConnexionAppliMobile`, QR-code content, iCal URL
(`icalsecurise=`), student name, establishment name or real `N` identifier. Not
in code, tests, fixtures, commit messages or screenshots. Example values must
be visibly fictional (`demo.example.invalid`, `Enfant Un`, `STUDENT-1`,
`0000000000A`). Fixtures under `tests/fixtures/` are hand-written, never
recorded from a live server; a genuinely captured payload goes in
`tests/fixtures/_raw/`, which `.gitignore` excludes. An iCal URL grants read
access to a child's entire timetable with no credentials — treat it as a
password.

**Never enable the `pronotepy` DEBUG logger.** It leaks in **two** places, so
filtering by sub-logger does not help. `pronotepy/pronoteAPI.py:139` writes the
reversible hex of every request body, credentials included, and `clients.log is
pronoteAPI.log` — one single logger, so the useful lines cannot be separated
from the leaking ones. Separately, `pronotepy/dataClasses.py:228-231`, in the
strict branch of `_resolver.__call__`, does `log.debug(json.dumps(self.json_dict))`
— the **entire** dictionary of the object being decoded, as readable JSON:
student name, homework text, establishment identifiers. That one is worse in one
respect (it is plain text, not hex to reverse) and it fires on a failed decode
path, which is exactly when somebody has just turned DEBUG on to understand why
a collection is failing. `pronotepy.dataClasses` is a different logger from
`pronotepy.pronoteAPI` and leaks independently of it. The integration therefore
declares no `loggers` key in `manifest.json` (`scripts/check_manifest.py`
enforces this) and
`tests/test_no_secret_in_state.py` checks a full cycle logs no secret. To debug,
enable `custom_components.pronote_ng: debug` only, and never suggest `pronotepy`
to a user in an issue.

## Commands

The gates, which are exactly what `.github/workflows/validate.yml` runs:

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/pronote_ng
python scripts/check_manifest.py
pytest tests --cov=custom_components/pronote_ng --cov-branch --cov-report=xml:coverage.xml
python scripts/check_coverage.py coverage.xml
python scripts/check_doc_citations.py --since=<base-ref>
mkdocs build --strict          # needs requirements_docs.txt
```

Two things about the citation gate. It needs `git`, which the test images do not
have, so run it on the host and not in the container. And `--since` must be
written as one argument: `--since <ref>` in two words used to be ignored
silently, leaving the gate report-only while CI went green — it now rejects any
argument it does not recognise, which is the whole point of a gate that can
otherwise disarm itself.

### Running tests on Windows

The full suite **cannot** run natively: `pytest-homeassistant-custom-component`
registers a pytest entry point that imports `fcntl`, loaded before any
`conftest.py` is read. A local `.venv` on Python 3.13 also installs an old HA
stack, so `mypy --strict` there checks against a version *below* the declared
floor and has already let a removed API through. `ruff` is fine locally; `mypy`
and `pytest` are not.

Use a prebuilt Docker image (build once — the HA test stack is large):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:/project/ai-project/ha-pronote:/work" -w /work \
  ha-pronote-test-2026.9.0 python -m pytest tests -q --no-header --show-capture=no
```

`ha-pronote-test-2026.9.0` is the **floor** image (HA 2026.9.0 via
`pytest-homeassistant-custom-component==0.13.363`, plus `mypy` and `ruff`);
`ha-pronote-test-2026.9` carries the newer HA 2026.9.1. Both are `python:3.14`
plus `requirements_test.txt`. `MSYS_NO_PATHCONV=1` and the absolute path are
required under Git Bash, or MSYS rewrites `/work`.

Never add `-p no:logging` — it removes the plugin that provides `caplog`, and
many tests assert on log records. `pytest tests -p no:homeassistant` runs the
HA-free half of the suite on Windows (limiter, scheduler, gateway, delta, DTOs,
budget estimator — the four modules held at 100 %), but it also disables the
plugin's network guard, so never write a test that could reach the network.

A single test: `python -m pytest tests/test_config_flow.py::test_name -q` inside
the container, same `docker run` prefix.

### Generated files — never hand-edit

`strings.json`, `translations/en.json`, `translations/fr.json` and
`services.yaml` are all produced by `scripts/build_translations.py`, and a test
asserts each file equals the generator's output. Edit the table in the script,
then run it. Nothing user-visible is hard-coded under `custom_components/`.
Two hassfest traps: a translation string may not contain a URL (use
`description_placeholders`), and `manifest.json` keys must be ordered `domain`,
`name`, then alphabetically.

### The version floor is one number in 19 files

`hacs.json` `homeassistant` ⇄ the `requirements_test.txt` pin ⇄ the gated matrix
row in `validate.yml` ⇄ 16 blueprint `min_version` values. Currently
**2026.9.0** / `0.13.363`. `tests/test_version_floor.py` holds all 19, and a CI
step compares `homeassistant.const.__version__` to `hacs.json` so the declared
number is the one actually exercised. PyPI mapping observed so far:
`0.13.354`→2026.8.0, `0.13.363`→2026.9.0, `0.13.364`→2026.9.1. Raise the floor
by *running* the new socle green first, then moving the declaration.

## Architecture

### One path to the network, and it is auditable

This is the organising constraint. PRONOTE publishes no rate limit; it applies
sanctions, including an undocumented and costly IP suspension for repeated
failed logins. So there is exactly one heartbeat and one chokepoint:

```
scheduler.py    which tiers are due?      (injectable clock, knows nothing of HA or PRONOTE)
   ↓
account.py      master tick: run due tiers in priority order, in one session
   ↓
session.py      one asyncio.Lock + one single-worker executor per config entry
   ↓
ratelimit.py    spacing, token bucket, daily cap, two login counters
   ↓
hardened_client.py  fixes four upstream behaviours that defeat a limiter
   ↓
gateway.py      one public function per *protocol call*; the only pronotepy importer
   ↓
models.py       frozen, slotted DTOs — nothing from pronotepy passes this line
```

Three properties of that chain break silently if you violate them:

- **Costs are declared, not inferred.** A `GatewayResult` reports its real
  request count (`calls`) and the session reconciles it with the limiter. A
  wrong declaration is a bug, not an approximation — an accidental lazy-property
  access doubles the real cost with nothing failing, which is the regression
  this project exists to prevent (`tiers.py` returns cost per tier so §11.1's
  contract test is possible).
- **Everything is charged at admission, under the lock, before the request** —
  never on return. A login costs five to seven requests and the handshake is
  slow; charged on return, a second caller reads an intact budget for seconds,
  which is how a cap of five logins lets ten through. There is deliberately **no
  refund path**, so an exception raised before the wire is still charged.
- **The DTO boundary is not aesthetic.** `pronotepy` exposes lazy properties
  (`Information.content`, `Period.grades`, `Discussion.messages`) that place an
  HTTP request *when read*. An upstream object escaping to a sensor is a network
  call from the event loop, outside all accounting. Only `gateway.py` and
  `hardened_client.py` see the library; `ClientInfo._cache()` in particular
  posts directly on `communication`, bypassing `ClientBase.post` and — on a
  parent account — the `membre` signature that says which child.

Serialisation matters for a second reason: the protocol numbers requests with an
encrypted counter that advances by two, and two concurrent calls on one session
desynchronise it and break the session. `set_child()` also *mutates* the client,
so the lock covers more than the call itself.

`pronotepy==2.15.7` is pinned and `hardened_client.py` documents each divergence
it corrects. Raising the pin means re-reading every one of them.

### Data flow out to entities

One config entry is one **account**; a parent account holds several children who
share a session, a budget and an IP address. `coordinator.py` runs with
`update_interval = None` on purpose — the scheduler decides and calls
`async_set_updated_data()`. There is one coordinator per **tier**, and an entity
subscribes only to its own, so collecting canteen menus does not rewrite the
timetable's state. Data is keyed by student.

Set-up order in `__init__.py` is load-bearing: the account logs in once, learns
its own shape (children, periods, current period), and *only then* are platforms
forwarded — an entity that does not know its child has no stable `unique_id`.

Two entity behaviours in `entity.py`:

- **Stale beats unavailable.** An entity goes `unavailable` only if it never had
  data or the snapshot aged past `stale_after` × its tier's interval; in between
  it keeps its value and flags its age. A `numeric_state` trigger on an entity
  that flaps to `unavailable` and back fires spuriously.
- **Clock-driven re-evaluation.** Entities whose state depends on the *time*
  ("in class", "next lesson", "absence in progress") schedule
  `async_track_point_in_time` at their next known transition, or they would be
  up to fifteen minutes wrong.

### The design objective, and what it implies

`docs/SPECIFICATION.md` §1: **any ordinary school automation must be writable
without a Jinja template.** That is the test for new work, and it explains
several choices that otherwise look redundant:

- Anything an automation may trigger on gets its own entity whose **state**
  carries the fact — a timestamp, a number, a boolean — never text and never a
  structure to walk. Lists stay available as attributes for cards but are never
  the only route to a fact. A state reading `8h30` or `14,5` loses sorting,
  graphing, thresholds and locale in one stroke.
- `event.py` exists because a state trigger cannot say "a *new* grade arrived".
  One `lesson_changed` entity declares **six** event types
  (`LESSON_EVENT_TYPES` in `const.py`: cancelled, restored, moved, room
  changed, teacher changed, status changed) and fires **one event per changed
  aspect**, so an automation asking "was a room changed?" never inspects a
  payload. `lesson_restored` is a distinct type from `lesson_canceled` on
  purpose: a detector that merges them fires "no first lesson, sleep in" on the
  morning the lesson is reinstated.
- `delta.py` splits the change rule: identifier-delta for most collections, but
  content comparison for lessons, because a room change keeps the same `N` and a
  replacement arrives with a fresh `N` while the original is still present.
- `device_trigger.py` / `device_condition.py` / `device_action.py` are thin
  wrappers over the same bus signals, existing entities and domain services — so
  there is one implementation of "is it a school day", and the automation trace
  shows the service call that really happened.
- `button.py` presses raise tier priority and wake the tick; they never call
  PRONOTE and get no dispensation from the limiter, so ten impatient presses
  cost one batch.

### Secrets by construction, not by redaction

The iCal URL, the identity block and the timetable PDF link are
`SupportsResponse.ONLY` service results (`services.py`), so no state, attribute
or runtime field holds them — which is also why they are absent from
`diagnostics.py` rather than redacted there. What *is* in the config entry
(token, username, UUID, client identifier) is redacted explicitly, and child
identifiers become a truncated fingerprint: still useful in a bug report,
useless for replaying a session. `urls.py` trims what the user pasted down to
the bare page URL, because ENT bounces and address-bar copies carry session
parameters that would otherwise reach `.storage` and repair placeholders.

Writes to PRONOTE are **off by default**; `todo.py`'s tick is the only write
reachable without a service call, and with writes off the list is read-only
through its supported features rather than by failing when tapped.

`login_guard.py` holds the two things that outlive a `PronoteAccount` — punitive
limiter state and login counters — in `hass.data`, because a `ConfigEntryNotReady`
retry every eighty seconds otherwise rebuilt a fresh limiter with all its holds
reset, producing thousands of logins against a cap of twenty-four while
`calls_today` reported five.

### The config flow

Three entry modes because PRONOTE has three — QR code, username/password, ENT
federated — each its own step rather than one form with sometimes-ignored
fields. `flow_login.py` is deliberately separate and **synchronous**: building a
`pronotepy` client *is* a network login (`ClientBase.__init__` ends with
`self.logged_in = self._login()`), so it must be handed to
`async_add_executor_job`, and `config_flow.py` must not import `pronotepy` at
module scope or a broken dependency turns "cannot log in" into "cannot be
added". Nothing is retried: one attempt per user gesture.

The 2FA PIN is never stored — asked for, used, dropped. Everything else (token,
UUID, client identifier) is persisted, and `_NEVER_PERSISTED` must be applied on
every persistence path.

## Coverage gate

80 % global, and **100 %** on `ratelimit.py`, `scheduler.py`, `gateway.py` and
`delta.py`, measured as `min(line, branch)`. Those four because their failures
are invisible: a limiter that lets a call through shows nothing until the
sanction; a skipped tier looks like slightly old data; a wrong decode produces a
plausible value; a missed change is an event that never arrives.

## Test conventions

- **A test name is a sentence**: `test_a_login_is_charged_before_the_handshake_returns`,
  not `test_login_charge`.
- **A docstring says why the test exists** — ideally which defect it stops from
  returning. A test nobody can justify is one somebody will delete to make CI
  green.
- **One seam is doubled.** The `account` fixture in `tests/conftest.py` replaces
  `session.build_client` and runs everything else for real — limiter, scheduler,
  session, gateway, coordinators and all seven platforms together.
  `tests/fixtures/protocol.py` yields raw protocol dictionaries that the real
  `pronotepy.dataClasses` decode, so fidelity is structural rather than
  declared. If you add a method to `FakeClient`, make it count requests the way
  the real library places them.
- A test that triggers a reload must drain it with
  `await hass.async_block_till_done()` *inside* the patch that suppresses set-up
  — `async_update_reload_and_abort` only schedules the reload, which otherwise
  runs after the patch is released and reaches the network.
- **Never derive an `entity_id` from a translation key.** The suffix comes from
  the entity's *translated name*, slugified by Home Assistant: the key
  `averages` is named "Moyennes par matière", so the entity is
  `sensor.<child>_subject_averages` in English and
  `sensor.<child>_moyennes_par_matiere` in French — never
  `sensor.<child>_averages`. Read the name from `translations/<lang>.json` and
  slugify it with `homeassistant.util.slugify`; do not re-implement the accent
  folding, which is where this project's two repositories have already
  disagreed once. The suite's entity ids are the **English** slugs; the French
  ones exist only on a French instance. This has now cost real time twice: a
  fixture that invented `sensor.enfant_un_averages`, and eleven identifiers in
  annexe A that named entities which do not exist. Home Assistant does not
  reject an unknown entity id in an automation — the trigger simply never
  fires, which reads as a broken integration.
- `tests/test_doc_contract.py` is the barrier for that second failure: every
  translated entity name must appear as a suffix in `docs/annexe-a-entites.md`.
  Names carrying a placeholder (`Notes ({period})`) are skipped, because Home
  Assistant substitutes the establishment's own period label and the suffix
  depends on runtime data — note that `_p<n>` is the `unique_id` suffix
  (`PronoteHistorySensor.__init__`), not the entity's.

## Releasing

Do **not** bump `manifest.json` in an ordinary PR — the version moves at
publication time and `release.yml` refuses a tag whose manifest diverges (a HACS
integration whose manifest disagrees with its tag installs once and never
updates again). Then: merge to `main` with the four workflows green (`Validate`,
`Hassfest`, `HACS`, `Docs`), and `git tag -a vX.Y.Z && git push origin vX.Y.Z`.
`release.yml` builds `pronote_ng.zip` and publishes the release.
