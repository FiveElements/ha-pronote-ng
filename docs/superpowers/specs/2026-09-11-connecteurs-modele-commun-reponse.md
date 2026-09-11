# Réponse à la revue « Connecteurs et modèle commun »

| | |
| --- | --- |
| **Objet** | Réponse à [`2026-09-11-connecteurs-modele-commun-revue.md`](./2026-09-11-connecteurs-modele-commun-revue.md) |
| **Spec** | [`2026-09-11-connecteurs-modele-commun.md`](./2026-09-11-connecteurs-modele-commun.md) |
| **Plan** | [`docs/superpowers/plans/2026-09-11-connecteurs-modele-commun.md`](../plans/2026-09-11-connecteurs-modele-commun.md) |
| **Date** | 11 septembre 2026 |
| **Méthode** | Chaque constat de la revue a été relu contre `custom_components/pronote_ng/` **avant** d'être intégré. Les objections ci-dessous sont des désaccords techniques, pas des préférences de rédaction. |
| **Suite** | Le §7 de la revue (même fichier) est la suite, pas un quatrième document. O1 : chiffres corrigés ci-dessous. |

---

## 0. Verdict sur la revue

Les trois choix que la revue demande de défendre (limiteurs isolés, pas de PyPI `ecoledirecte`, pivot cartes = JSON d'attributs de l'annexe A §2.1) restent. Les deux familles d'échec qu'elle identifie (construction sous `__init__`, mappings qui fabriquent des faits) tiennent contre le code.

La spec et le plan sont réalignés. Un seul correctif code a été posé tout de suite, parce qu'il désarme une barrière indépendamment du chantier : `scripts/check_coverage.py` indexe par suffixe de chemin.

Le reste de cette réponse est un décompte : accepté, puis les points où on **ne** suit **pas** la revue telle quelle.

---

## 1. Objections

### O1. §3.1 — ne pas traiter les `.gateway` comme une seule PR horloge

La revue compte 46 `gateway.now()` / `gateway.today()` **et** 67 références `.gateway` hors `account.py` / `gateway.py`, et en conclut que l'étape « `account.now` / `today` » est *la* PR du chantier.

Les 46 appels horloge sont bien là : 42 hors `account.py` / `gateway.py` (`sensor.py` 23, `binary_sensor.py` 16, `calendar.py` 1, `tiers.py` 2) plus 4 `now()` dans `account.py`. Ça justifie une PR dédiée, la plus large en nombre de fichiers — une quarantaine de sites dans trois modules d'entités, pas un préalable de 12 appels.

La phrase précédente « les 67 `.gateway` ne sont pas de l'horloge » était fausse. Ventilation relu :

| Module | `.gateway` | dont horloge |
| --- | --- | --- |
| `sensor.py` | 23 | 23 |
| `binary_sensor.py` | 16 | 16 |
| `calendar.py` | 1 | 1 |
| `tiers.py` | 15 | 2 |
| `services.py`, `attachment.py`, `todo.py`, `image.py` | 12 | 0 |

42 des 67 **sont** de l'horloge ; 12 (les modules cités pour le fil) ne le sont pas ; les 13 restants dans `tiers.py` sont protocolaires. Le capteur `session_age` lit `account.session.session_age`, pas `gateway.timetable` — une des 23 horloges de `sensor.py` sert au `remaining` du limiteur.

La coupe en deux PR reste : « qui donne l'heure » / « qui possède le fil Pronote ». Les fondre mélangerait Task 3 et `PronoteExtras` + executor. La Task 3 se dimensionne sur les 42, pas sur le reliquat.

**Décision :** la PR horloge ne fait que `account.now()` / `today()` comme unique chemin d'horloge. Le reste de `.gateway` part avec la Task `PronoteConnector`, pas avant.

Sur le §7.2 de la revue : la fonction s'appelle `_lesson_events`, pas `_timetable_events`. Elle est branchée comme `events_fn` du calendrier `key="timetable"`. Le constat tient : `lesson.status` est le premier élément de `description_parts`.

### O2. §2.4 — pas de conversion de `duration` en heures

La revue offre deux correctifs : `duration=0` Pronote-only, **ou** convertir les minutes ED en heures.

`PronoteGateway` fait `timedelta(hours=duration)` : le champ est un entier d'heures issu de `duree`. Un cours ED de 55 minutes deviendrait `duration ≈ 0.92`. Ça invente une précision que Pronote n'a jamais eue, et ça casse toute comparaison naïve `duration == 1`. `_lesson_dict` ne publie pas `duration` aujourd'hui : le défaut resterait invisible jusqu'au jour où quelqu'un s'en sert.

**Décision :** `duration=0` côté ED, listé §9.4. Pas de conversion.

### O3. §1.3 — pas de parse de `libelle` en v1 ; le `or 0` Pronote reste hors chantier

`Delay.minutes: int | None` et `Absence.days: int | None`, ED → `None` → JSON `null` : accepté. C'est la même doctrine que `Grade.value` XOR `status`.

Deux ajouts de la revue qu'on **ne** prend **pas** dans ce chantier :

1. *« Si `libelle` porte une durée exploitable, une fonction de parse testée vaut mieux. »* Le `libelle` observé est du texte d'établissement (« 2 demi-journées »), pas un entier de minutes. Un parseur français sans fixture de formes réelles fabriquerait le même genre de nombre plausible. v1 : `null`. Un parse, plus tard, avec des fixtures manuscrites.

2. *Changer le mapping Pronote `minutes=int(upstream.minutes or 0)`.* C'est un sentinelle **déjà en production** sur les entries Pronote. Le passer à `None` changerait les charges utiles `delay_added` d'instances qui tournent. Le chantier Ecoledirecte n'est pas le véhicule de cette correction. Le type devient `int | None` ; Pronote continue de fournir un `int` quand la bibliothèque en donne un.

### O4. §3.5 — `SESSION` hors `capabilities.tiers`, pas « inoffensif »

La revue dit que mettre `SESSION` dans `capabilities.tiers` est inoffensif, pourvu qu'on documente que `async_collect(Tier.SESSION, …)` n'est pas défini.

`collect_tier` lève déjà `ValueError` sur `SESSION`. `default_plans` l'omet. `scheduled_tiers` est l'intersection options ∩ capacités. Un palier présent dans `capabilities` et oublié de `async_collect` est exactement le trou que `ConnectorUnsupportedError` existe pour crier — à condition qu'on ne le mette pas dans le frozenset.

**Décision :** Pronote et ED : `SESSION` **absent** de `capabilities.tiers`. `session_facts()` est une méthode. `async_collect(Tier.SESSION, …)` lève.

### O5. §5 forme — la citation du handshake 0.3.0 reste

La revue demande d'argumenter par exigences, pas par « on ne copie pas hass-ecoledirecte ». D'accord pour le registre comparatif (`leur` `select`, « on ne le copie pas »). Ces phrases sont sorties de la spec.

On **garde** la citation : handshake = `ecoledirecte_api` 0.3.0 tel qu'utilisé par hass-ecoledirecte, et **pas** la doc EduWire 2024 (`v=4.75.0`, jeton JSON). Ce n'est pas un cadrage produit. C'est la source de vérité du transport (GTK, `isReLogin`, jeton en header). Sans ça, un lecteur prend EduWire et câble le mauvais login.

### O6. §2.3 — les deux barrières, pas l'alternative

La revue offre *basenames distincts **ou** indexation par chemin*. L'alternative « noms distincts seulement » laisse la barrière se désarmer le jour où un fichier s'appelle encore `ratelimit.py`. L'indexation par basename est le défaut ; les noms `ed_*` sont une ceinture.

**Décision :** `check_coverage.py` indexe par suffixe posix (`pronote_ng/ratelimit.py`), fail closed si le suffixe est ambigu. **Et** fichiers ED `ed_limiter.py` / `ed_client.py` / `ed_mapping.py`, 100 %. Le script est déjà corrigé (tests HA-free verts) ; l'ajout des trois modules ED à `CRITICAL_MODULES` attend qu'ils existent (Task 9).

---

## 2. Accepté — bloquants

### 1.1 Construction de `PronoteAccount`

Vérifié : `__init__` crée sans condition `PronoteGateway`, `RateLimiter` (import de `limiter_state_store`), `FetchScheduler(now=gateway.now)`, `SerialExecutor`, `SessionManager(_credentials_from_entry(entry))`.

Le critère « `tiers.py` n'appelle plus `session.run` » ne l'attrape pas. Une entry ED ouvrirait un thread, lirait un état punitif Pronote, et passerait l'entry à un lecteur d'identifiants Pronote.

**Écrit :** `__init__` ne construit rien de source-spécifique. `build_connector()` d'abord, horloge du connecteur, **puis** le scheduler. Critère §12 : une entry ED ne crée ni `SerialExecutor`, ni `SessionManager`, ni `RateLimiter` Pronote. Executor / session / limiteur Pronote vivent dans `PronoteConnector`.

### 1.2 `PERMANENCE → detention`

Vérifié : `_in_class`, `_in_class_transition` et `_in_class_attributes` filtrent `canceled` et `exempted` seulement. Le seul lecteur de `Lesson.detention` est `_lesson_dict`. Le calendrier des retenues part de `Punishment.schedule`.

Les trois phrases « les entités en cours excluent déjà `detention` » étaient fausses. Une permanence n'est pas une retenue.

**Écrit :** `detention=False`, `status="PERMANENCE"`, `in_class` **s'allume**. Les trois affirmations sont supprimées.

### 1.3 `minutes=0` / `days=0`

Vérifié : `_delay_dict` publie `minutes` ; `delta.py` le recopie dans `delay_added`. `Grade.__post_init__` interdit déjà une sentinelle numérique.

**Écrit :** `int | None`, ED `None` → `null`, jamais `0`. Voir O3 pour ce qu'on ne parse pas et ce qu'on ne change pas côté Pronote.

---

## 3. Accepté — sérieux

### 2.1 Période sans chemin de retour

Vérifié : `_async_load_session_facts` est le seul écrivain de `AccountState.periods` / `current_period`. Sans republie, `current_period` reste `unknown` après `MARKS`.

**Écrit :** après une collecte `MARKS` réussie, le connecteur met à jour ses périodes internes ; le compte republie le coordinateur `SESSION` (`GatewayResult` à `calls=0`).

### 2.2 Clé de baseline delta

`DeltaDetector.attendance` compose `f"absences:{facts.period_id}"`. Une bascule `""` → id réel absorbe le premier instantané.

**Écrit :** `AttendanceFacts.period_id` = `""` **constant** pour la vie du connecteur ED. Pas de copie de l'id `MARKS`.

### 2.4 Unité de `Lesson.duration`

Vérifié : `timedelta(hours=duration)` dans `PronoteGateway`. Voir O2.

### 2.3 Couverture par basename

Vérifié : l'ancienne `_module_coverage` rangeait sous `Path(filename).name`. Un second `ratelimit.py` écrasait la ligne Pronote.

**Fait** (code, pas encore commité) : clés `pronote_ng/ratelimit.py` etc., lookup par suffixe, fail closed si ambigu. `tests/test_check_coverage.py` : collision de basename, préfixe `coverage.xml`, ambiguïté. Voir O6.

---

## 4. Accepté — manques

| # | Constat | Écrit |
| --- | --- | --- |
| 3.2 | `session_age` / `select` stratégie hors `ConnectorCapabilities` | Uniquement `PronoteExtras`. ED : capteurs limiteur seulement. `pronote-ng-mode-collecte` : **non**. |
| 3.3 | `previous_unread` orphelin | Mémoire **sur** `PronoteConnector`. Signature `async_collect` inchangée. Pas d'`UnreadStore` injecté depuis le compte. |
| 3.4 | Court-circuit période absente | `GatewayResult(calls=0, faits vides)`, pas `None`, pas une exception. |
| 3.6 | Sonde `APIVERSION` report-only | Workflow `ecoledirecte-watch`, forme `pronotepy-watch` : **échec + PR** de constante, pas un warning. |

3.1 et 3.5 : voir O1 et O4.

---

## 5. Accepté — objectif foyer mixte et forme

- Critère produit §1 / §12 : deux entries, deux appareils, les cartes du recouvrement se résolvent sur les deux (`translation_key` + `platform === "pronote_ng"` + attributs annexe A).
- §8.3 : suffixe d'`entity_id` = nom traduit slugifié. Homonymes / même enfant des deux côtés → `_2` silencieux. `unique_id` reste la clé stable ; les cartes résolvent par appareil.
- §9.3 : exigence **vers** ha-pronote-ng-cards (clés qui ne bougent pas), pas « aucun changement requis dans ce dépôt ».
- Forme : sorties les phrases « on ne le copie pas » / « chez eux ». Conservée la citation de handshake (O5).

---

## 6. Ce qui est écrit où

| Livrable | État |
| --- | --- |
| Spec §0, §5.5, §8.1, §11, §12 | Construction du compte, critère executor |
| Spec §7.6, §9.1, §9.2, §9.4 | Permanence, `duration=0` |
| Spec §7.9, Task 1b du plan | `minutes` / `days` optionnels |
| Spec §7.10, §8.1 | Republie `SESSION` après `MARKS` |
| Spec §3.5, §5.2 | Quatre paliers ; `SESSION` jamais collecté |
| Spec §7.1, plan Task 11 | `ecoledirecte-watch` |
| Plan Task 3 | Horloge = grosse PR (~42 `now`/`today` dans 3 modules d'entités), **seulement** `now` / `today` (O1, chiffres corrigés) |
| Plan Task 4 | Objets Pronote dans le connecteur, pas dans `__init__` |
| Plan Tasks 7–9 | `ed_client.py`, `ed_mapping.py`, `ed_limiter.py` |
| `scripts/check_coverage.py` + tests | PR #12 ; réserves revue §7.5 (test `main()`, message « absent » vs ambigu) |

Rien d'autre n'est implémenté. La suite de revue est le §7 du fichier revue, pas un quatrième document.
