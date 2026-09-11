# Connecteurs et modèle commun — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extraire une couture `SchoolConnector` alimentée par Pronote (comportement identique) puis par Ecoledirecte (**quatre** paliers collectés), vers les DTO gelés existants.

**Architecture:** Un connecteur possède le fil. `PronoteAccount.__init__` ne construit rien de source-spécifique (`build_connector` avant le scheduler). `tiers.py` appelle `async_collect` ; `SESSION` n'y passe jamais. Les services Pronote restent sur `PronoteExtras`. Ecoledirecte : client maison calqué sur `EDClient` 0.3.0 (`ed_client.py`, `ed_mapping.py`, `ed_limiter.py`). DTO = pivot interne ; attributs annexe A = pivot cartes (ha-pronote-ng-cards), pas `matiere` / `is_annule` / `note_sur`.

**Tech Stack:** Home Assistant 2026.9.0+, domaine `pronote_ng`, `pronotepy==2.15.7` (Pronote uniquement), aiohttp (Ecoledirecte, **sans** paquet `ecoledirecte`), pytest, Docker `ha-pronote-test-2026.9.0`.

**Spec:** [`docs/superpowers/specs/2026-09-11-connecteurs-modele-commun.md`](../specs/2026-09-11-connecteurs-modele-commun.md)

## Global Constraints

- Home Assistant plancher **2026.9.0** ; Python **3.14** dans l'image de test.
- Aucun identifiant réel nulle part ; exemples `demo.example.invalid`, `Enfant Un`, `STUDENT-1`.
- Jamais le journaliseur `pronotepy` **ni** `ecoledirecte` / `ecoledirecte_api` en DEBUG ; pas de clé `loggers` dans `manifest.json`.
- Coût déclaré avant l'envoi, pas de remboursement. Login ED : 2, puis +4 si 250, **avant** la suite. EDT ED : 1 POST.
- Rien de `pronotepy` ni de JSON Ecoledirecte au-delà du connecteur concerné. Pas de dépendance PyPI `ecoledirecte`.
- Pivot cartes = clés de l'annexe A (`subject`, `canceled`, `value`, `classroom`), jamais `matiere` / `is_annule` / `note_sur`. Même `translation_key` qu'une entry Pronote. Permanence : `detention=False`, `status="PERMANENCE"`. `typeCours` inconnu → `status is None`, pas le jeton brut. `Delay.minutes` / `Absence.days` ED = `None` (`null`), jamais `0`. Pronote garde `minutes=int(upstream.minutes or 0)` (dette, hors chantier).
- Une entry ED ne crée ni `SerialExecutor`, ni `SessionManager`, ni `RateLimiter` Pronote.
- `manifest.json` version **inchangée**.
- Une branche par sujet, PR vers `main` ; pas de push direct.
- Tests HA-free : `pytest tests/<file> -p no:homeassistant` (hôte). Suite complète : image Docker `ha-pronote-test-2026.9.0` (voir `CONTRIBUTING.md`). Ne jamais `-p no:logging`.
- `GatewayResult` déménage dans `models.py` pour qu'Ecoledirecte n'importe pas `gateway.py`.
- `check_coverage.py` indexe par suffixe de chemin (`pronote_ng/ratelimit.py`), déjà en place. Les modules ED (`ed_limiter.py`, `ed_client.py`, `ed_mapping.py`) rejoignent la table 100 % à la Task 9.

## File map

| Fichier | Rôle |
| --- | --- |
| `custom_components/pronote_ng/connectors/__init__.py` | Paquet public |
| `custom_components/pronote_ng/connectors/protocol.py` | `Source`, `ChallengeKind`, `ConnectorCapabilities`, `SchoolConnector` |
| `custom_components/pronote_ng/connectors/errors.py` | Exceptions de couture |
| `custom_components/pronote_ng/connectors/factory.py` | `build_connector` |
| `custom_components/pronote_ng/connectors/pronote.py` | `PronoteConnector` |
| `custom_components/pronote_ng/connectors/ecoledirecte/ed_client.py` | HTTP GTK / token / codes |
| `custom_components/pronote_ng/connectors/ecoledirecte/ed_mapping.py` | JSON → DTO |
| `custom_components/pronote_ng/connectors/ecoledirecte/ed_limiter.py` | Admission ED |
| `custom_components/pronote_ng/connectors/ecoledirecte/connector.py` | `EcoledirecteConnector` |
| `custom_components/pronote_ng/models.py` | `GatewayResult` + défauts `Lesson` |
| `custom_components/pronote_ng/account.py` | Possède un `SchoolConnector` ; `now` / `today` |
| `custom_components/pronote_ng/tiers.py` | `connector.async_collect` uniquement |
| `custom_components/pronote_ng/const.py` | `CONF_SOURCE` |
| `tests/test_connector_protocol.py` | Couture, `FakeConnector` |
| `tests/fixtures/ecoledirecte/*.json` | Charges utiles manuscrites |
| `tests/test_ecoledirecte_*.py` | Client, mapping, connecteur |

---

### Task 1: Couture — erreurs, capacités, `GatewayResult` dans `models.py`

**Files:**
- Create: `custom_components/pronote_ng/connectors/__init__.py`
- Create: `custom_components/pronote_ng/connectors/protocol.py`
- Create: `custom_components/pronote_ng/connectors/errors.py`
- Create: `tests/test_connector_protocol.py`
- Modify: `custom_components/pronote_ng/models.py` (ajouter `GatewayResult`)
- Modify: `custom_components/pronote_ng/gateway.py` (réexporter `GatewayResult` depuis `models` pour ne pas casser les imports existants, puis les basculer)
- Modify: every current `from .gateway import GatewayResult` / `from custom_components.pronote_ng.gateway import GatewayResult` to `models`

**Interfaces:**
- Consumes: `Tier` (`const.py`), rien d'autre
- Produces: `Source`, `ChallengeKind`, `ConnectorCapabilities`, `SchoolConnector`, exceptions `Connector*Error`, `GatewayResult` dans `models.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_connector_protocol.py
"""The connector seam: costs are declared, protocol objects do not leak."""

from custom_components.pronote_ng.connectors.errors import (
    ConnectorChildMissingError,
    ConnectorUnsupportedError,
)
from custom_components.pronote_ng.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.pronote_ng.const import Tier
from custom_components.pronote_ng.models import GatewayResult, HomeworkFacts


def test_a_capability_set_that_omits_menus_does_not_list_that_tier() -> None:
    """An Ecoledirecte-shaped connector must not be asked for a canteen page."""
    caps = ConnectorCapabilities(
        source=Source.ECOLEDIRECTE,
        tiers=frozenset(
            {Tier.TIMETABLE, Tier.HOMEWORK, Tier.MARKS, Tier.ATTENDANCE}
        ),
        writes=frozenset(),
        services=frozenset(),
    )
    assert Tier.MENUS not in caps.tiers
    assert caps.source is Source.ECOLEDIRECTE


def test_gateway_result_carries_the_declared_cost_next_to_the_facts() -> None:
    """A cost inferred after the fact is how a lazy property used to double a bill."""
    facts = HomeworkFacts(homework=())
    result = GatewayResult(facts, calls=1)
    assert result.calls == 1
    assert result.facts is facts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_connector_protocol.py -p no:homeassistant -q`

Expected: FAIL — `ModuleNotFoundError: connectors`

- [ ] **Step 3: Minimal implementation**

`protocol.py` : `class Source(StrEnum): PRONOTE = "pronote"; ECOLEDIRECTE = "ecoledirecte"` ; `ChallengeKind` (`pin`, `qcm`) ; dataclass `ConnectorCapabilities` (champs `source`, `tiers`, `writes`, `services`) ; `class SchoolConnector(Protocol):` avec les méthodes de la spec §5.3.

`errors.py` : les six classes de la spec §5.1 (`ConnectorTransportError`, `ConnectorCredentialsError`, `ConnectorChallengeRequired` avec `kind: ChallengeKind`, `ConnectorUndecodableError`, `ConnectorChildMissingError`, `ConnectorUnsupportedError`), toutes sous `ConnectorError`.

`models.py` : déplacer le corps actuel de `GatewayResult` (slots `calls`, `facts`, `__init__(facts, calls)`).

`gateway.py` : `from .models import GatewayResult` puis l'utiliser ; supprimer la classe locale. Grep `GatewayResult` et corriger les imports producteurs (`gateway.py` lui-même).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_connector_protocol.py tests/test_gateway.py -p no:homeassistant -q`

Expected: PASS (gateway tests that import `GatewayResult` still collect).

Puis, image Docker, au minimum : `python -m pytest tests/test_gateway.py tests/test_connector_protocol.py -q`

- [ ] **Step 5: Commit**

```bash
git add custom_components/pronote_ng/connectors custom_components/pronote_ng/models.py custom_components/pronote_ng/gateway.py tests/test_connector_protocol.py
git commit -m "$(cat <<'EOF'
refactor(connecteurs): poser la couture et déplacer GatewayResult

Le coût déclaré ne peut pas vivre dans gateway.py si une seconde source
ne doit jamais importer pronotepy.
EOF
)"
```

---

### Task 1b: `Delay.minutes` / `Absence.days` optionnels

Un `0` publié là où Aplim n'envoie pas de durée entière ferait tenir un déclencheur `numeric_state` qui ne part jamais (même interdiction que `Grade.value`).

**Files:**
- Modify: `custom_components/pronote_ng/models.py` (`days: int | None`, `minutes: int | None`)
- Modify: `custom_components/pronote_ng/sensor.py` (`_absence_dict` / `_delay_dict` publient la valeur telle quelle, y compris `None` → JSON `null`)
- Modify: `custom_components/pronote_ng/delta.py` si la charge utile d'événement recopie `minutes` / `days`
- Test: `tests/test_models.py` s'il existe, sinon `tests/test_connector_protocol.py` (construction HA-free) + un test capteur Docker pour le dict

**Interfaces:**
- Pronote : le gateway **ne change pas**. `minutes=int(upstream.minutes or 0)` reste : un `None` changerait les charges utiles `delay_added` des instances qui tournent. Dette datée, hors ce chantier. Après Task 1b le type est `int | None` ; **seul ED** publie `None`.
- ED (Task 8) : `None`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime

from custom_components.pronote_ng.models import Absence, Delay


def test_a_delay_without_a_duration_is_none_not_zero() -> None:
    """A numeric_state on minutes=0 would stay valid and never fire."""
    delay = Delay(
        id="D1",
        at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        minutes=None,
        justified=False,
        justification=None,
        reasons=(),
    )
    assert delay.minutes is None
```

- [ ] **Step 2: Run to verify fail** — `minutes: int` rejects `None` (ou le constructeur l'accepte déjà si le champ n'est pas annoté strictement ; alors le test du sérialiseur doit échouer : aujourd'hui `_delay_dict` publie `0` dès que le DTO a `0`)

- [ ] **Step 3: Implement** les deux `Optional`. **Ne pas** toucher `PronoteGateway` : `minutes=int(upstream.minutes or 0)` et `days=int(upstream.days or 0)` restent. Les sérialiseurs recopient `None` quand le DTO en porte un (chemin ED).

- [ ] **Step 4: Run** HA-free `test_delta.py` + Docker `test_sensor.py` / événements retard.

- [ ] **Step 5: Commit**

```bash
git commit -m "fix(models): une durée absente n'est plus un zéro numérique"
```

---

### Task 2: `FakeConnector` et contrat `async_collect`

**Files:**
- Create: `tests/fakes/connector.py` (ou `tests/fixtures/fake_connector.py`)
- Modify: `tests/test_connector_protocol.py`

**Interfaces:**
- Consumes: `SchoolConnector`, `GatewayResult`, `ConnectorUnsupportedError`, `Priority`
- Produces: `FakeConnector` avec `capabilities`, `async_collect`, `calls_by_tier`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from custom_components.pronote_ng.connectors.errors import ConnectorUnsupportedError
from custom_components.pronote_ng.const import Priority, Tier
from custom_components.pronote_ng.models import HomeworkFacts
from tests.fixtures.fake_connector import FakeConnector


@pytest.mark.asyncio
async def test_collecting_a_supported_tier_returns_the_cost_the_fake_declared() -> None:
    """The seam's only number that matters is the one the connector claims."""
    connector = FakeConnector(
        facts_by_tier={Tier.HOMEWORK: HomeworkFacts(homework=())},
        cost_by_tier={Tier.HOMEWORK: 1},
    )
    result = await connector.async_collect(
        Tier.HOMEWORK, "STUDENT-1", priority=Priority.HIGH
    )
    assert result.calls == 1
    assert result.facts.homework == ()


@pytest.mark.asyncio
async def test_collecting_an_unsupported_tier_is_a_caller_bug() -> None:
    """Capabilities exist so account.py never asks; if it does, that must scream."""
    connector = FakeConnector()
    with pytest.raises(ConnectorUnsupportedError):
        await connector.async_collect(Tier.MENUS, "STUDENT-1", priority=Priority.LOW)


@pytest.mark.asyncio
async def test_collecting_session_is_a_caller_bug() -> None:
    """SESSION is not a collectable tier; collect_tier already raises ValueError."""
    connector = FakeConnector()
    with pytest.raises(ConnectorUnsupportedError):
        await connector.async_collect(Tier.SESSION, "STUDENT-1", priority=Priority.HIGH)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_connector_protocol.py -p no:homeassistant -q`

Expected: FAIL — `fake_connector` missing

- [ ] **Step 3: Implement `FakeConnector`**

Classe concrète (pas un `Protocol`) : `capabilities` Pronote-complet par défaut, ou l'ensemble passé au constructeur. `async_collect` lève `ConnectorUnsupportedError` si `tier not in capabilities.tiers`, sinon `GatewayResult(facts_by_tier[tier], cost_by_tier[tier])`. `now`/`today` injectables. `async_open` no-op. `student_ids` = `("STUDENT-1",)`. Aucun dict brut stocké.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_connector_protocol.py -p no:homeassistant -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "test(connecteurs): un faux connecteur déclare son coût et refuse le reste"
```

---

### Task 3: Horloge `account.now` / `account.today`

**Cette tâche est la grosse PR d'horloge** (spec §5.4) — pas un préalable mécanique de quelques appels à coller entre deux refactors.

Comptage relu (références `.gateway` hors `account.py` / `gateway.py`) :

| Module | `.gateway` | dont `now()` / `today()` |
| --- | --- | --- |
| `sensor.py` | 23 | **23** |
| `binary_sensor.py` | 16 | **16** |
| `calendar.py` | 1 | **1** |
| `tiers.py` | 15 | 2 |
| `services.py`, `attachment.py`, `todo.py`, `image.py` | 12 | 0 |

42 des 67 **sont** de l'horloge ; plus 4 `now()` dans `account.py` = 46. Les 12 de `services` / `attachment` / `todo` / `image` (et les 13 protocolaires de `tiers.py`) **ne** sont **pas** cette tâche : c'est le fil Pronote (`PronoteExtras` + Task 4).

Dimensionner ici : une quarantaine de sites dans **trois modules d'entités** (`sensor.py`, `binary_sensor.py`, `calendar.py`). Après elle, `account.now()` / `today()` est le **seul** chemin d'horloge.

**Files:**
- Modify: `custom_components/pronote_ng/account.py`
- Modify: every `account.gateway.now()` / `account.gateway.today()` in `sensor.py`, `binary_sensor.py`, `calendar.py`, `tiers.py`
- Test: `tests/test_account.py` or a focused test if one already asserts on timezone — sinon ajouter dans `tests/test_connector_protocol.py` un test d'un stub account n'est pas possible (HA). Ajouter un test unitaire sur une fonction extraite si besoin.

**Interfaces:**
- Consumes: fuseau déjà lu par `_establishment_timezone`
- Produces: `PronoteAccount.now() -> datetime`, `PronoteAccount.today() -> date`, délégués au gateway **puis** au connecteur à la Task 4

- [ ] **Step 1: Write the failing test**

Dans un test HA existant qui a la fixture `account` (ex. setup), assert `account.now()` est timezone-aware et `account.today() == account.now().date()`. Si aucun test n'importe `PronoteAccount` hors HA, ajouter :

```python
def test_account_now_is_timezone_aware(account) -> None:
    """Naive datetimes in entity state lose ordering and locale in one stroke."""
    assert account.now().tzinfo is not None
    assert account.today() == account.now().date()
```

Le nom de la fixture réelle est `account` dans `tests/conftest.py` — l'adapter au test module qui l'utilise déjà (souvent `tests/test_init.py` / setup). **Ne pas** créer une fixture parallèle.

- [ ] **Step 2: Run to verify fail**

Run (Docker) : `python -m pytest tests/test_init.py::test_account_now_is_timezone_aware -q` (ajuster le chemin au fichier choisi)

Expected: FAIL — `PronoteAccount` has no `now`

- [ ] **Step 3: Implement**

```python
def now(self) -> datetime:
    return self.gateway.now()

def today(self) -> date:
    return self.gateway.today()
```

Remplacer les appels `account.gateway.now()` / `.today()` et `self.account.gateway.now()` par `account.now()` / `today()`. **Laisser** `account.gateway.timetable` etc. pour cette tâche.

- [ ] **Step 4: Run**

Docker : `python -m pytest tests -q --no-header` si la campagne complète est raisonnable ; sinon les tests d'entités timetable/homework plus le nouveau.

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: l'horloge du compte ne passe plus par le gateway Pronote"
```

---

### Task 4: `PronoteConnector` et `tiers.py` via `async_collect`

**Files:**
- Create: `custom_components/pronote_ng/connectors/pronote.py`
- Modify: `custom_components/pronote_ng/account.py` (possède `self.connector`, plus `self.gateway` public si possible ; garder `.gateway` comme propriété qui délègue tant que services/todo/image/attachment en ont besoin)
- Modify: `custom_components/pronote_ng/tiers.py` (plus de `session.run` + lambda)
- Test: la suite existante `tests/test_session.py`, collecte, `tests/test_init.py`

**Interfaces:**
- Consumes: `SessionManager.run`, `PronoteGateway.*`, `SchoolConnector`
- Produces: `PronoteConnector.async_collect(tier, student_id, *, priority) -> GatewayResult`

- [ ] **Step 1: Write the failing test**

```python
def test_pronote_connector_capabilities_list_every_collectable_tier() -> None:
    """A Pronote account that silently drops history is a missing bulletin, not a setting."""
    from custom_components.pronote_ng.connectors.pronote import PronoteConnector
    from custom_components.pronote_ng.const import Tier

    collectable = frozenset(tier for tier in Tier if tier is not Tier.SESSION)
    assert collectable <= PronoteConnector.CAPABILITIES.tiers
    assert Tier.SESSION not in PronoteConnector.CAPABILITIES.tiers
```

(Constante de classe `CAPABILITIES` sur `PronoteConnector`.)

Ajouter un test d'intégration existant : après setup, `hass.config_entries` + `account.connector.capabilities.source == Source.PRONOTE`.

- [ ] **Step 2: Run to verify fail**

Expected: FAIL — `connectors.pronote` missing

- [ ] **Step 3: Implement `PronoteConnector`**

Constructeur du **connecteur** : les objets que `PronoteAccount.__init__` crée aujourd'hui (`limiter`, `executor`, `session`, `gateway`, credentials, timeouts). `async_open` appelle le login actuel (`session` / `build_client`). `async_collect` : déplacer **tels quels** les bras de `tiers.py` (`include_next_week`, `with_report`). `previous_unread` / `remember_unread` vivent **sur** `PronoteConnector` (décision de coût DISCUSSIONS) ; la signature `async_collect(tier, student_id, priority)` ne s'élargit pas.

Si la période courante est absente : `GatewayResult` à `calls=0` et faits vides — pas `None`, pas une exception. `async_collect(Tier.SESSION, …)` lève `ConnectorUnsupportedError`.

`PronoteAccount.__init__` : options bornées → `self.connector = build_connector(...)` → `self.scheduler = FetchScheduler(..., now=self.connector.now)` → delta → coordinateurs. **Pas** de `PronoteGateway` / `RateLimiter` / `SerialExecutor` / `SessionManager` sur le compte. Les propriétés `gateway` / `session` / `limiter` pour `services.py` passent par `PronoteExtras` (le connecteur), pas par des champs construits dans `__init__`.

`tiers.py` : chaque bras garde son `account.delta.timetable` / `.homework` / etc. Seul l'appel réseau change :

```python
result = await account.connector.async_collect(
    tier, student_id, priority=priority
)
snapshot = _snapshot(account, tier, student_id, result.facts, result.calls)
events = account.delta.timetable(student_id, result.facts)  # bras TIMETABLE ; idem pour les autres
return snapshot, result.calls, events
```

`PronoteAccount` : `self.connector = build_connector(...)` (Task 6 pose la fabrique ; ici, construction directe `PronoteConnector` acceptable **si** Task 6 n'est pas encore fusionnée, mais **aucun** objet Pronote à la racine du compte). `now()` délègue à `self.connector.now()`. `tiers.py` n'appelle plus `session.run`.

- [ ] **Step 4: Run the Pronote suite**

Docker : `python -m pytest tests -q --no-header --show-capture=no`

Expected: PASS, mêmes coûts (`calls`) sur les tests de collecte.

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor(connecteurs): Pronote passe derrière SchoolConnector"
```

---

### Task 5: Filtrer l'ordonnanceur et les plateformes sur `capabilities`

**Files:**
- Modify: `custom_components/pronote_ng/account.py` (plans de l'ordonnanceur)
- Modify: `custom_components/pronote_ng/sensor.py` (et autres plateformes qui itèrent les paliers) — création d'entités
- Create: `tests/test_capabilities_filter.py`

**Interfaces:**
- Consumes: `ConnectorCapabilities.tiers`
- Produces: pas d'entité, pas de `mark_failed`, pour un palier absent

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_a_connector_without_menus_never_collects_that_tier() -> None:
    """Unavailable-forever menu entities were how a missing ED page would look like an outage."""
    connector = FakeConnector(
        capabilities=ConnectorCapabilities(
            source=Source.ECOLEDIRECTE,
            tiers=frozenset({Tier.TIMETABLE}),
            writes=frozenset(),
            services=frozenset(),
        )
    )
    # Brancher le fake sur un compte de test (fixture) ou tester la fonction
    # pure `scheduled_tiers(caps, enabled)` extraite de account.py.
    from custom_components.pronote_ng.account import scheduled_tiers

    assert Tier.MENUS not in scheduled_tiers(connector.capabilities, enabled=None)
    assert Tier.TIMETABLE in scheduled_tiers(connector.capabilities, enabled=None)
```

Extraire `scheduled_tiers(capabilities, enabled: Mapping[Tier, bool] | None) -> frozenset[Tier]` : intersection options ∩ capacités ; `enabled is None` = tous les flags True.

- [ ] **Step 2: Run to verify fail**

Expected: FAIL — `scheduled_tiers` missing

- [ ] **Step 3: Implement filter** ; dans `async_setup` des plateformes, sauter les descriptions dont le `Tier` n'est pas dans `account.connector.capabilities.tiers`. Boutons de refresh : idem. Services : si le nom n'est pas dans `capabilities.services`, `ServiceValidationError`.

- [ ] **Step 4: Run**

HA-free : `scheduled_tiers`. Docker : setup Pronote (toutes les entités encore là).

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(connecteurs): ne pas collecter un palier que la source n'a pas"
```

---

### Task 6: Fabrique et `CONF_SOURCE`

**Files:**
- Create: `custom_components/pronote_ng/connectors/factory.py`
- Modify: `custom_components/pronote_ng/const.py` (`CONF_SOURCE: Final = "source"`)
- Modify: `custom_components/pronote_ng/account.py` (`build_connector`)
- Modify: `tests/test_connector_protocol.py`

**Interfaces:**
- Consumes: `entry.data.get(CONF_SOURCE, Source.PRONOTE)`
- Produces: `build_connector(hass, entry, **deps) -> SchoolConnector`

- [ ] **Step 1: Write the failing test**

```python
def test_an_entry_without_a_source_key_is_pronote() -> None:
    """Existing installs must not wake up as Ecoledirecte."""
    from custom_components.pronote_ng.connectors.factory import source_from_entry_data
    from custom_components.pronote_ng.connectors.protocol import Source

    assert source_from_entry_data({}) is Source.PRONOTE
    assert source_from_entry_data({"source": "ecoledirecte"}) is Source.ECOLEDIRECTE
```

- [ ] **Step 2: Run to verify fail**

Expected: FAIL — `factory` missing

- [ ] **Step 3: Implement** `source_from_entry_data` + `build_connector` qui, pour Pronote, construit `PronoteConnector` comme `PronoteAccount` le faisait. Pour Ecoledirecte : lever `ConnectorUnsupportedError` **tant que** la Task 9 n'existe pas, **ou** brancher déjà le symbole et skip ce bras. Préférer : `if source is Source.ECOLEDIRECTE: raise ValueError("ecoledirecte connector is not wired yet")` dans cette tâche, testé, remplacé à la Task 9.

- [ ] **Step 4: Run** + suite Docker setup Pronote

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(connecteurs): une entry sans source reste Pronote"
```

---

### Task 7: Client HTTP Ecoledirecte (GTK, login, codes)

**Files:**
- Create: `custom_components/pronote_ng/connectors/ecoledirecte/__init__.py`
- Create: `custom_components/pronote_ng/connectors/ecoledirecte/ed_client.py`
- Create: `tests/fixtures/ecoledirecte/login_ok.json` (token fictif `not-a-real-token`, compte `Enfant Un`, id `1`)
- Create: `tests/fixtures/ecoledirecte/login_505.json`
- Create: `tests/fixtures/ecoledirecte/login_250.json`
- Create: `tests/test_ecoledirecte_client.py`

**Interfaces:**
- Consumes: session aiohttp **injectée** (dans les tests : `aiohttp.ClientSession` + `aioresponses` si déjà en deps, sinon un fake `request` injectable — **préférer un transport Protocol** `async def request(method, url, **kwargs) -> FakeResponse` pour ne pas ajouter de dépendance)
- Produces: `EcoleDirecteClient.login`, `.request(path, *, verbe, data)`, `.calls: int`, exceptions de couture

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from custom_components.pronote_ng.connectors.ecoledirecte.ed_client import (
    ECOLEDIRECTE_API_VERSION,
    EcoleDirecteClient,
)
from custom_components.pronote_ng.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
)
from custom_components.pronote_ng.connectors.protocol import ChallengeKind


@pytest.mark.asyncio
async def test_a_login_fetches_gtk_before_posting_credentials() -> None:
    """Aplim treats a missing GTK as a wrong password, which would trip the IP-like hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    await client.login("demo.example.invalid", "not-a-real-password")
    assert transport.urls[0].endswith("gtk=1")
    assert transport.headers_on[1].get("x-gtk") == "gtk-demo"
    assert client.calls == 2
    assert ECOLEDIRECTE_API_VERSION  # named constant used in both URLs


@pytest.mark.asyncio
async def test_a_505_is_credentials_and_not_transport() -> None:
    """The HTTP status is 200; classifying on it would never increment the hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_505.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorCredentialsError):
        await client.login("demo.example.invalid", "wrong")


@pytest.mark.asyncio
async def test_a_250_keeps_the_token_for_the_qcm() -> None:
    """The unofficial docs require the login token to answer the quiz."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorChallengeRequired) as caught:
        await client.login("demo.example.invalid", "not-a-real-password")
    assert caught.value.kind is ChallengeKind.QCM
    assert client.token == "not-a-real-token"
```

Les fixtures JSON : `code` 200/505/250, `token` fictif, `data.accounts` avec un élève nommé `Enfant Un`. Pas d'établissement réel.

`RecordingTransport` vit dans le fichier de test ou `tests/fixtures/ecoledirecte/transport.py`.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ecoledirecte_client.py -p no:homeassistant -q`

Expected: FAIL — module missing

- [ ] **Step 3: Implement client**

Constante `API_BASE = "https://api.ecoledirecte.com/v3"`, `ECOLEDIRECTE_API_VERSION = "4.101.3"` (celle de `ecoledirecte==0.3.0`). En-têtes = copie de `EDClient` (UA Chrome fixe + origin ecoledirecte). GET gtk → cookie `GTK` → header `x-gtk`. POST login `isReLogin: false`. Jeton depuis le header de réponse `x-token` (pas le JSON). Compteur `calls`. Codes 505 / 250 / 517 / 520 / 525 selon spec §7.3. Deadline = `read_timeout` (60 s), pas 120. Le mot de passe ne doit apparaître ni dans `__repr__` ni dans les logs. Chemins : tableau spec §7.2, casse comprise.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ed): client HTTP avec GTK, jeton et codes métier"
```

---

### Task 8: Mapping Ecoledirecte → DTO

**Files:**
- Create: `custom_components/pronote_ng/connectors/ecoledirecte/ed_mapping.py`
- Create: `tests/fixtures/ecoledirecte/emploi_du_temps.json` (deux cours, un `isAnnule`, noms fictifs)
- Create: `tests/fixtures/ecoledirecte/cahier_de_texte.json`
- Create: `tests/fixtures/ecoledirecte/notes.json`
- Create: `tests/fixtures/ecoledirecte/vie_scolaire.json`
- Create: `tests/test_ecoledirecte_mapping.py`

**Interfaces:**
- Consumes: dict JSON (jamais aiohttp)
- Produces: `Lesson`, `TimetableFacts`, `Homework`, `HomeworkFacts`, `Grade`, `MarksFacts`, `Absence`/`Delay`/`Punishment`, `AttendanceFacts`, `Student`/`SessionFacts`

- [ ] **Step 1: Write the failing tests** (un test par tableau de la spec §7.6–§7.9). Exemples :

```python
def test_an_annulled_ed_lesson_maps_to_a_canceled_lesson() -> None:
    """binary_sensor.cours_annules must turn on without a Jinja walk of the JSON."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    canceled = [lesson for lesson in facts.lessons if lesson.canceled]
    assert len(canceled) == 1
    assert canceled[0].end.tzinfo is not None
    assert canceled[0].num == 0
    assert facts.lessons == facts.all_lessons


def test_a_permanence_is_a_followed_slot_not_a_detention() -> None:
    """Study hall is not a detention; _in_class filters only canceled and exempted."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    study = next(lesson for lesson in facts.lessons if lesson.status == "PERMANENCE")
    assert study.detention is False
    assert study.canceled is False
    assert study.duration == 0


def test_an_unknown_type_cours_does_not_become_a_calendar_status() -> None:
    """_lesson_events puts Lesson.status on the first line of the calendar description."""
    raw = {**minimal_ed_lesson(), "typeCours": "JETON-INCONNU"}
    lesson = lesson_from_ed(raw, zone="Europe/Paris")
    assert lesson.status is None
    assert lesson.detention is False


def test_homework_v1_has_no_body_because_the_list_endpoint_does_not_pay_for_it() -> None:
    """Fetching each due date would explode the daily cap; empty prose is honest."""
    facts = homework_facts(load_json("cahier_de_texte.json"))
    item = facts.homework[0]
    assert item.description == ""
    assert item.done in {True, False}
    assert item.id  # from idDevoir


def test_a_french_decimal_grade_becomes_a_float_and_never_a_string_state() -> None:
    """A state of '14,5' cannot trip numeric_state."""
    grade = grades_from_notes(load_json("notes.json"))[0]
    assert isinstance(grade.value, float)
    assert grade.status is None


def test_a_missing_notes_key_is_undecodable_not_an_empty_term() -> None:
    """An empty tuple is a plausible term; a broken payload must fail the tier."""
    with pytest.raises(ConnectorUndecodableError):
        marks_facts({}, current_period_id="A001")


def test_an_ed_absence_does_not_invent_a_period_unique_id() -> None:
    """History sensors are Pronote-only; a sentinel period_id would mint _pN anyway."""
    facts = attendance_facts(load_json("vie_scolaire.json"), period_id="")
    assert facts.period_id == ""
    assert facts.absences[0].days is None
    assert facts.delays[0].minutes is None
    assert all(p.schedule == () for p in facts.punishments)


def test_an_ed_lesson_serializes_to_the_card_contract_not_the_aplim_keys() -> None:
    """ha-pronote-ng-cards read subject/canceled; a matiere/is_annule payload would render empty."""
    from custom_components.pronote_ng.sensor import _lesson_dict

    facts = timetable_facts(load_json("emploi_du_temps.json"), zone="Europe/Paris")
    payload = _lesson_dict(facts.lessons[0])
    assert "subject" in payload
    assert "canceled" in payload
    assert "classroom" in payload
    assert "matiere" not in payload
    assert "is_annule" not in payload
    assert payload["detention"] is False
    assert "salle" not in payload
```

- [ ] **Step 2: Run to verify fail**

- [ ] **Step 3: Implement mapping** selon spec §7. Fonction unique `_parse_fr_decimal(text: str) -> float | None`. Dates naïves → `ZoneInfo` passé en argument (jamais `datetime.now()`).

- [ ] **Step 4: Run to verify pass** + `ruff check` sur les nouveaux fichiers

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ed): mapper l'emploi du temps, les devoirs, les notes et l'absence vers les DTO"
```

---

### Task 9: `EcoledirecteConnector` (quatre paliers collectés)

**Files:**
- Create: `custom_components/pronote_ng/connectors/ecoledirecte/ed_limiter.py`
- Create: `custom_components/pronote_ng/connectors/ecoledirecte/connector.py`
- Create: `tests/test_ecoledirecte_connector.py`
- Modify: `connectors/factory.py` (brancher ED)
- Modify: `scripts/check_coverage.py` (`CRITICAL_MODULES` += `pronote_ng/connectors/ecoledirecte/ed_limiter.py`, `ed_client.py`, `ed_mapping.py`)
- Modify: `tests/test_check_coverage.py` (les trois suffixes)

**Interfaces:**
- Consumes: `EcoleDirecteClient`, mapping, `SchoolConnector`, options d'espacement / plafond quotidien
- Produces: `EcoledirecteConnector` avec `CAPABILITIES.tiers` = `{TIMETABLE, HOMEWORK, MARKS, ATTENDANCE}` (pas `SESSION`), `async_collect` → `GatewayResult`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_timetable_collect_declares_the_posts_the_client_actually_made() -> None:
    """A declared 1 with two POSTs is the lazy-property bug, on a new protocol."""
    client = EcoleDirecteClient(
        RecordingTransport.scripted(
            [
                ("GET", "login.awp", {"gtk": "gtk-demo"}),
                ("POST", "login.awp", load_fixture("login_ok.json")),
                ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
            ]
        )
    )
    connector = EcoledirecteConnector(client=client, zone="Europe/Paris")
    await connector.async_open()
    calls_before = client.calls
    result = await connector.async_collect(
        Tier.TIMETABLE, "1", priority=Priority.HIGH
    )
    assert result.calls == client.calls - calls_before
    assert result.facts.lessons[0].start.tzinfo is not None


@pytest.mark.asyncio
async def test_two_collects_on_one_connector_do_not_run_concurrently() -> None:
    """The token and GTK cookie are mutable; overlapping POSTs are a session bug."""
    import asyncio

    order: list[str] = []

    class SlowTransport(RecordingTransport):
        async def request(self, method: str, url: str, **kwargs: object) -> object:
            order.append("start")
            await asyncio.sleep(0.02)
            response = await super().request(method, url, **kwargs)
            order.append("end")
            return response

    transport = SlowTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
        ]
    )
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(transport), zone="Europe/Paris"
    )
    await connector.async_open()
    first, second = await asyncio.gather(
        connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH),
        connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH),
    )
    collect_order = order[4:]  # skip GTK + login (2 requests = 4 start/end)
    assert collect_order == ["start", "end", "start", "end"]
    assert first.calls >= 1 and second.calls >= 1
```

Limiter : un test « 505 charge le hold identifiants de **cette** entry » ; un test « 505 ED ne touche pas `login_guard()` Pronote » ; un test « 250 n'incrémente pas failed_logins » ; un test « le palier MENUS lève `ConnectorUnsupportedError` » ; un test « `async_collect(Tier.SESSION)` lève » ; un test « le seau attend le coût 2, pas `REQUESTS_PER_LOGIN` » ; un test « après MARKS, `session_facts().current_period` est renseigné » (`calls=0` sur la republie).

Construction : un test HA (Docker) « une entry ED n'instancie ni `SerialExecutor`, ni `SessionManager`, ni `RateLimiter` Pronote » — patcher `build_connector`, setup, `isinstance(account.connector, EcoledirecteConnector)`, et absence de ces types sur `account`.

- [ ] **Step 2: Run to verify fail**

- [ ] **Step 3: Implement**

`EdRateLimiter` : lock asyncio, trois couches (espacement, seau horaire, plafond jour), hold 505, hold QCM, backoff transport, heures creuses — **mêmes `OPT_*` et mêmes `LimiterState` / `Priority` que Pronote** (spec §3.3 et §7.5). **Ne pas** instancier `ratelimit.RateLimiter` (coût login 5, `G = 25`). Cadences = `DEFAULT_TIER_INTERVALS` des paliers capables (spec §8.2). Flow ED : garde-fou **distinct** de `login_guard()` Pronote. État punitif dans `limiter_state_store[entry_id]`.

`EcoledirecteConnector.async_open` : si jeton mémoire présent, 0 appel ; sinon GTK + login, coût **2** à l'admission. Un 250 : **+4 avant** la suite QCM. `async_collect` : match sur `Tier` (**jamais** `SESSION`), `renewtoken` (coût 1, clé login) si `idLogin` change, un POST métier, mappe, `GatewayResult(facts, calls)`. EDT : exactement 1 POST, `calls==1` hors renewtoken. Après `MARKS` réussi : mettre à jour les périodes internes ; le compte republie le coordinateur `SESSION` (`calls=0`). `AttendanceFacts.period_id` reste `""`.

`factory.py` : `Source.ECOLEDIRECTE` → `EcoledirecteConnector` (session aiohttp créée par l'appelant HA dans une tâche suivante ; ici le constructeur **exige** un client déjà prêt pour rester testable hors HA).

- [ ] **Step 4: Run** `tests/test_ecoledirecte_*.py -p no:homeassistant` + `ruff` + `mypy --strict` (Docker) sur `custom_components/pronote_ng`

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ed): connecteur lecture seule pour quatre paliers"
```

---

### Task 10: Config flow Ecoledirecte (identifiant, mot de passe, QCM)

**Files:**
- Modify: `custom_components/pronote_ng/config_flow.py` (étape source **seulement pour une nouvelle entrée** ; le chemin Pronote actuel reste le défaut si on détecte l'ancien user flow — plus simple : nouvelle `async_step_user` qui demande la source, puis `async_step_pronote_*` existant ou `async_step_ed_login`)
- Modify: `scripts/build_translations.py` (table — ne pas éditer `strings.json` à la main)
- Run: `python scripts/build_translations.py`
- Test: `tests/test_config_flow.py` (nouveaux tests, les anciens restent verts)

**Interfaces:**
- Consumes: `EcoleDirecteClient.login`, `ConnectorChallengeRequired`
- Produces: entry `data={CONF_SOURCE, username, password, qcm_json, child_keys}` ; **pas** de jeton ni GTK ni `cn`/`cv` ; QCM seulement après un login 200

- [ ] **Step 1: Write the failing tests**

Ajouter un item de menu `ecoledirecte` à côté de `ent` / identifiants / QR — **ne pas** intercaler un écran « source » devant, les tests Pronote casseraient tous.

```python
async def test_an_ecoledirecte_login_stores_password_and_not_the_token(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Aplim has no durable device token; the password is the only replayable secret."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.pronote_ng.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"]["password"] == "not-a-real-password"
    assert "token" not in created["data"]
    assert created["data"][CONF_SOURCE] == "ecoledirecte"


async def test_a_250_opens_the_qcm_step_and_does_not_create_the_entry(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """An entry that exists without a 200 login would be half-born."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.pronote_ng.config_flow._probe_ecoledirecte",
        side_effect=ConnectorChallengeRequired(ChallengeKind.QCM),
    ):
        challenged = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
    assert challenged["type"] is FlowResultType.FORM
    assert challenged["step_id"] == "ecoledirecte_qcm"
    assert hass.config_entries.async_entries(DOMAIN) == []
```

Les tests Pronote existants (`next_step_id` QR / identifiants / `ent`) ne changent pas.

- [ ] **Step 2: Run to verify fail**

Docker : `python -m pytest tests/test_config_flow.py -q`

- [ ] **Step 3: Implement flow** ; traductions via le script ; hassfest : pas d'URL dans les chaînes.

`async_setup_entry` : `build_connector` ; pour ED, `async_create_clientsession(hass)` passé au client (cookies GTK).

- [ ] **Step 4: Run** `tests/test_config_flow.py` + hassfest local si possible + `python scripts/build_translations.py` puis re-test que les JSON générés matchent

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(ed): flow de configuration identifiant, jeton et QCM"
```

---

### Task 11: Documentation d'architecture (après code vert)

**Files:**
- Modify: `docs/ARCHITECTURE.md` §2.1 (couche connecteur). Ne pas ajouter de citations `module.py` avec un numéro de ligne : préférer les numéros de spec. Après édition, lancer le portail des citations sur l'hôte (`--since=origin/main`).
- Create: `.github/workflows/ecoledirecte-watch.yml` (même forme que `pronotepy-watch.yml` : le job **échoue** et ouvre une PR de constante si `APIVERSION` ≠ `4.101.3`)
- Modify: `docs/superpowers/specs/2026-09-11-connecteurs-modele-commun.md` statut → « partiellement implémenté » seulement si tout le plan l'est

- [ ] **Step 1:** Pas de test code. Vérifier `python scripts/check_doc_citations.py --since=origin/main` sur l'hôte (pas le conteneur) après toute citation `*.py:N`.

- [ ] **Step 2:** `mkdocs build --strict`

- [ ] **Step 3:** Commit docs

```bash
git commit -m "docs: la voie réseau est un connecteur, pas le gateway Pronote"
```

---

## Spec coverage

| Spec | Task |
| --- | --- |
| §3.1–3.3 couture, DTO Pronote, limiteurs isolés / options partagées | 1, 4, 9 |
| §3.4–3.5 capacités, quatre paliers ED, `SESSION` jamais collecté | 2, 5, 9 |
| §3.6 entry sans source = Pronote | 6 |
| §5 contrat + erreurs | 1, 2 |
| §5.4 horloge (grosse PR : ~42 `now`/`today` dans 3 modules d'entités) | 3 |
| §5.5 fabrique + `__init__` sans objets Pronote | 4, 6 |
| §6 PronoteConnector + PronoteExtras + `previous_unread` | 4 |
| §7.1 pas de PyPI `ecoledirecte` + sonde `ecoledirecte-watch` | 7, 11 |
| §7.2–7.4 transport, codes, QCM | 7, 10 |
| §7.5 session, couches, auth, flow ED ≠ `login_guard` Pronote | 9, 10 |
| §7.6–7.9 mapping (permanence, `typeCours` inconnu → `status is None`, `minutes`/`days` `None`, `duration=0`) | 1b, 8 |
| §7.10–7.11 enfants, secrets, republie `SESSION` | 9, 10 |
| §8.1 `tiers.py` sans `session.run` | 4 |
| §8.2 cadences Pronote + estimateur ED | 5, 9 |
| §8.3 plateformes + `translation_key` + pas de `select` stratégie ED | 5 |
| §8.4 flow | 10 |
| §9 pivot cartes / foyer mixte | 5, 8, 9 |
| §10 tests / secrets / couverture 100 % `ed_*` | 9, toutes |
| §12 foyer mixte, pas d'executor sur entry ED | 4, 9 |
| PR séparables | Tasks 1–6 + 1b = PR Pronote ; 7–10 = PR ED |

## Type consistency

- `async_collect(self, tier: Tier, student_id: str, *, priority: Priority) -> GatewayResult[Any]` identique Tasks 2, 4, 9.
- `ConnectorCapabilities(source, tiers, writes, services)` identique Tasks 1, 5, 9.
- `ChallengeKind.QCM` (pas `qcm` en attribut Python : `QCM = "qcm"`).
- `GatewayResult` toujours dans `models.py` après Task 1.
- `Delay.minutes` / `Absence.days` : `int | None` après Task 1b ; ED passe `None`. Pronote garde `int(upstream.minutes or 0)` / `int(upstream.days or 0)` (dette).
- `Lesson.status` ED : ensemble fermé (`PERMANENCE` → `"PERMANENCE"`) ; tout autre `typeCours` → `None`.
- Fichiers ED : `ed_client.py`, `ed_mapping.py`, `ed_limiter.py` (jamais un basename déjà tenu par Pronote).
- `CONF_SOURCE = "source"` ; valeur persistée `"pronote"` / `"ecoledirecte"` = `Source`.

## Ouvert après revue §7 (pas un quatrième fichier)

Les §1 à §5 de [`…-revue.md`](../specs/2026-09-11-connecteurs-modele-commun-revue.md) sont soldés. Reste, d'après le §7.7 de **ce** fichier revue :

| # | Point | Où |
| --- | --- | --- |
| 7.2 | `typeCours` inconnu → `status is None` | spec §7.6 / §9.2 ; Task 8 (test ci-dessus) |
| 7.3 | Dimensionnement Task 3 = ~42 sites d'horloge | Task 3 (tableau) |
| 7.4 | `minutes=int(… or 0)` côté Pronote | Task 1b (dette, pas de correctif Pronote) |
| 7.5.1 | Test `main()` de `check_coverage.py` | PR #12 (`chore/coverage-gate-path-suffix`), pas ce plan |
| 7.5.2 | Message « absent » vs suffixe ambigu | idem #12 |
| 7.6 | `AccountState` vs coordinateur `SESSION` | spec §7.10 |
