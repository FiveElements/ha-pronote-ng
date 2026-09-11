# Revue de la spécification « Connecteurs et modèle commun »

| | |
| --- | --- |
| **Objet** | Critique de [`2026-09-11-connecteurs-modele-commun.md`](./2026-09-11-connecteurs-modele-commun.md), version « corrigée après revue » (801 lignes) |
| **Date** | 11 septembre 2026 |
| **Méthode** | Chaque constat est vérifié contre le code de `custom_components/pronote_ng/` et les barrières de `scripts/`. Les symboles sont cités, jamais les numéros de ligne : ils bougent. |
| **Objectif de référence** | Un enfant suivi par Pronote et un enfant suivi par Ecoledirecte, **sur la même instance**, alimentant **les mêmes cartes**. Le rebranding n'est pas le sujet. |

---

## 0. Verdict

La coupe tient. Le §0 (tableau de décisions tranchées, avec le *pourquoi*) est le
meilleur ajout de cette version, et trois choix méritent d'être défendus tels
quels :

- **deux entries, deux limiteurs, jamais `RateLimiter` sur Aplim** — un limiteur
  qui encode l'annexe B mentirait sur `calls_by_tier` et appliquerait un coût de
  login faux ;
- **pas de dépendance PyPI** — l'argument est une exigence (un `backoff` qui
  relogue hors admission défait le chokepoint, un DEBUG de jeton est la même
  famille de fuite que `pronotepy`), pas une préférence ;
- **le pivot cartes est le JSON d'attributs de l'annexe A §2.1, pas le
  dataclass** — c'est la seule formulation qui rende l'objectif « mêmes cartes »
  vérifiable, et elle mérite de rester en tête du §9.

Ce qui ne tient pas se range en deux familles. La spec décrit le **contrat** de
la couture mais pas la **construction** de ce qui est en dessous ; et trois
mappings Ecoledirecte **fabriquent des faits** au lieu de les omettre, ce qui est
l'interdit que ce dépôt encode partout ailleurs.

---

## 1. Bloquants

### 1.1 `PronoteAccount.__init__` construit toute la pile Pronote, quelle que soit la source

`PronoteAccount.__init__` crée sans condition : `PronoteGateway` ; un
`RateLimiter` dont il importe aussitôt l'état punitif depuis
`limiter_state_store` ; un `FetchScheduler` dont le `now` **vient de ce
gateway** ; un `SerialExecutor`, qui démarre un **thread** ; et un
`SessionManager` construit sur `_credentials_from_entry(entry)`, qui lit des
identifiants Pronote.

Le §0 dit « `account.py` **peut** encore voir le gateway Pronote » et le §12 ne
vérifie que `tiers.py`. Conséquence directe : une entry Ecoledirecte ouvrirait un
thread executor inutile, importerait un état punitif Pronote, et passerait une
entry ED à un lecteur d'identifiants Pronote. Le critère d'acceptation actuel ne
peut pas l'attraper.

L'ordre de construction est lui aussi porteur : le scheduler prend son horloge du
gateway, donc **ce qui fournit l'horloge doit exister avant le scheduler**. C'est
une contrainte sur `build_connector`, pas un détail d'implémentation.

**À ajouter à la spec :** une décision explicite — `__init__` ne construit rien
de source-spécifique, `build_connector()` d'abord, le scheduler ensuite — et un
critère d'acceptation : *« une entry ED ne crée ni `SerialExecutor`, ni
`SessionManager`, ni `RateLimiter` Pronote »*.

### 1.2 La justification de `PERMANENCE → detention` est fausse

La spec l'affirme trois fois (tableau §7.6, §9.1, §9.2) : « les entités "en
cours" **excluent** déjà `detention` », donc « `in_class` reste éteint ».

`_in_class` ne filtre que `canceled` et `exempted`. Idem `_in_class_transition`
et `_in_class_attributes`. Le seul lecteur de `Lesson.detention` est
`_lesson_dict` ; le calendrier des retenues (`_punishment_events`) part de
`Punishment.schedule`, pas du cours.

Donc le mapping ne produit **rien** de ce qu'on lui prête : une permanence ED
allumerait `in_class`, et poserait `"detention": true` dans un attribut de carte.
C'est peut-être la bonne réponse produit — l'enfant *est* à l'établissement —
mais alors il faut mapper `detention=False`, l'assumer comme un cours, et le
dire. Une permanence n'est pas une retenue.

**À ajouter à la spec :** trancher explicitement, et supprimer les trois
affirmations sur le filtrage.

### 1.3 `Delay.minutes=0` et `Absence.days=0` fabriquent des nombres

C'est exactement l'interdit qu'encode `Grade.__post_init__` — `value` et `status`
mutuellement exclusifs, pour qu'une sentinelle n'atteigne jamais un déclencheur
`numeric_state` sous forme de nombre — et que `_marks` cite dans sa docstring
(§3.3.3 : une moyenne fausse est pire qu'une moyenne absente).

`minutes` est publié en attribut par `_delay_dict` **et** en charge utile
d'événement (`delay_added`, annexe A). Une automatisation « retard de plus de
quinze minutes » resterait valide, ne lèverait rien, et ne se déclencherait
**jamais**. C'est le mode d'échec que ce dépôt a déjà payé deux fois avec les
`entity_id` inventés : Home Assistant n'objecte pas, le déclencheur ne part
simplement pas, et ça se lit comme une intégration cassée.

Le §9.2 assume aujourd'hui « la carte pourra montrer "0 min" — défaut accepté
v1 ». C'est le seul endroit du document qui contredise la doctrine du dépôt.

**Correctif :** `Delay.minutes: int | None`, `Absence.days: int | None`, `None`
côté ED, sérialiseurs qui publient `null`. Coût réel : deux `Optional`, deux
tests. Si `libelle` porte une durée exploitable, une fonction de parse testée
vaut mieux — mais jamais `0`.

---

## 2. Sérieux

### 2.1 La période ED n'a aucun chemin de retour

`_async_load_session_facts` est le **seul** écrivain de `AccountState.periods` et
`AccountState.current_period`. Le §7.10 et le §9.1 promettent
« `current_period` dégradé jusqu'au palier `MARKS`, **puis** le libellé
`periode` » : rien ne réalise ce « puis ». Aucun mécanisme ne republie
`SessionFacts` dans le coordinateur `SESSION` après une collecte réussie, et
`periods_for` continue de lire le tuple vide.

En l'état, l'entité période ne reste pas `unknown` *jusqu'à* `MARKS` : elle reste
`unknown`.

### 2.2 La clé de baseline du delta bascule sous les pieds

`DeltaDetector.marks`, `.attendance` et `.evaluations` composent leur clé de
collection comme `f"absences:{facts.period_id}"` et voisines. Le §7.9 fait passer
`period_id` de `""` à l'identifiant réel dès que `MARKS` a répondu.

Bonne nouvelle : `_new_ids` amorce une clé inconnue **silencieusement** — pas de
tempête de notifications. Mauvaise : l'effet est plus discret et plus gênant.
Tout ce que porte l'instantané au moment de la bascule est absorbé dans une
baseline neuve et n'est **jamais** annoncé ; et si `current_period` redevient
`None`, la clé rebascule.

**Correctif :** côté ED, `period_id` est constant pour la durée du run. Lier une
clé de baseline à un champ amorcé de façon asynchrone est un piège.

### 2.3 La barrière de couverture est indexée par *basename*

`_module_coverage` (dans `scripts/check_coverage.py`) range ses résultats sous
`Path(filename).name`, et la table des modules à 100 % contient `"ratelimit.py"`
et `"gateway.py"`. Un `connectors/ecoledirecte/ratelimit.py` **écraserait**
l'entrée du limiteur Pronote : la barrière 100 % mesurerait alors le module ED,
et personne ne le verrait.

C'est la même famille que le `--since` en deux mots : une barrière qui se désarme
en silence pendant que CI passe au vert.

**Correctif :** basenames distincts (`ed_limiter.py`, `ed_client.py`,
`ed_mapping.py`) **ou** indexation sur le chemin relatif dans le script.

Par ailleurs le §10 a perdu l'exigence de couverture que portait la version
précédente. Par la doctrine du dépôt — échec invisible, donc 100 % — le mapping
ED et le limiteur ED y ont leur place : un mauvais décodage produit une valeur
plausible, ce qui est précisément le critère.

### 2.4 `Lesson.duration` changerait d'unité selon la source

`PronoteGateway` lit `duree` et s'en sert comme `timedelta(hours=duration)` :
`Lesson.duration` est en **heures**. Le §7.6 demande « minutes entières de
`end - start` ». Un cours ED de 55 minutes porterait `duration=55`, un cours
Pronote d'une heure `duration=1`.

`_lesson_dict` ne publie pas `duration` aujourd'hui, donc le défaut serait
invisible — ce qui en fait exactement le genre de régression que ce dépôt existe
pour empêcher.

**Correctif :** faire rejoindre `duration` la liste §9.4 des champs Pronote-only
à `0`, ou convertir en heures. Pas deux unités derrière un même nom.

---

## 3. Manques

### 3.1 L'horloge est sous-estimée

L'étape 2 de l'ordre d'atterrissage (« `account.now` / `today` ») porte
46 appels `gateway.now()` / `gateway.today()` répartis dans `account.py`,
`sensor.py`, `binary_sensor.py`, `calendar.py` et `tiers.py` — et il reste
67 références `.gateway` hors `account.py` et `gateway.py` au total
(`sensor.py` 23, `binary_sensor.py` 16, `tiers.py` 15, `services.py` 8,
`attachment.py` 2, `todo.py` / `image.py` / `calendar.py` 1 chacun).

Ce n'est pas un préalable mécanique : c'est la PR qui touche le plus de fichiers
du chantier.

### 3.2 Les entités diagnostiques Pronote-only ne sont dans aucune capacité

Le capteur de durée de vie de session (dans `sensor.py`) lit
`account.session.session_age` et `account.session.lifetime.observed_minutes`.
`ConnectorCapabilities` ne porte que `tiers`, `writes` et `services` : rien ne
dit ce que devient ce capteur sur une entry ED. Même question pour le `select` de
stratégie de session, que le §9.3 déclare « oui si le select existe » — il
n'existe que pour Pronote, et `OPT_SESSION_STRATEGY` pilote `SessionManager`.

### 3.3 `previous_unread` / `remember_unread` sont orphelins

`_discussions` passe `account.previous_unread(student_id)` dans l'appel et
réécrit la mémoire ensuite, sous une règle que son propre commentaire explique :
un fil que le gateway n'a **pas** déplié doit conserver son **ancien** compteur,
sinon il n'est plus jamais éligible et tous ses messages arrivent avec
`author: null`.

C'est une décision de **coût**, au même titre que `include_next_week` que le §6
déplace explicitement. Elle doit suivre dans `PronoteConnector`, et la signature
`async_collect(tier, student_id, priority)` ne lui laisse aucune place.

### 3.4 Le court-circuit « pas de période courante » n'est pas porté

`_marks`, `_attendance` et `_evaluations` renvoient un instantané **vide à zéro
appel** quand `account.state.current_period is None` — avec, pour `_marks`, la
justification §3.3.3 dans la docstring. Sous le contrat
`async_collect -> GatewayResult`, ce chemin doit exister dans le connecteur
Pronote (un `GatewayResult` à `calls=0` portant des faits vides). Non écrit, on
remonte un `None` ou une exception là où `tiers.py` produisait un instantané.

### 3.5 « Cinq paliers » en vaut quatre

La docstring de `Tier` dit que `SESSION` n'en est pas un ; `collect_tier` lève
`ValueError` dessus ; `default_plans` itère `DEFAULT_TIER_INTERVALS`, qui l'omet.
Le mettre dans `capabilities.tiers` est inoffensif, mais §0, §3.5 et §5.2
annoncent cinq paliers de **données** là où il y en a quatre, et
`async_collect(Tier.SESSION, …)` n'est défini nulle part.

**À ajouter :** dire explicitement qu'il n'est jamais appelé.

### 3.6 La sonde d'`APIVERSION` doit faire rougir, pas avertir

Le §7.1 demande « échouer si ≠ notre constante » sans dire où ni comment. Ce
dépôt a déjà perdu une barrière qui tournait en report-only pendant que CI
passait au vert. Nommer le workflow et le comportement attendu (bloquer, ouvrir
une issue, les deux).

---

## 4. Sur l'objectif : un enfant Pronote, un enfant ED, les mêmes cartes

Le §9 sert bien l'objectif — trois couches, trois contrats, le pivot cartes
identifié comme le JSON d'attributs. Mais l'objectif n'apparaît pas comme critère
d'acceptation *produit*. Trois manques :

- **Le test du foyer mixte.** Deux entries côte à côte sur la même instance, deux
  appareils enfants, et les six cartes du recouvrement qui se résolvent sur les
  deux sans fourche. Le §12 ne le couvre que par des critères négatifs (« un 505
  ED sans effet sur… »).
- **La contrainte d'`entity_id`.** Le suffixe vient du **nom traduit slugifié**,
  pas de la clé de traduction. Deux enfants de prénoms différents ne
  collisionnent pas ; deux homonymes, ou le même enfant présent des deux côtés
  pendant une migration, produisent un `_2` silencieux. Une phrase au §8.3.
- **Le §9.3 est un engagement inter-dépôt.** « Aucun changement requis dans
  ha-pronote-ng-cards » n'est pas une affirmation que cette spec puisse tenir
  seule. À formuler comme une exigence *vers* le dépôt frère — la liste des clés
  qui ne doivent pas bouger — et non comme un constat.

---

## 5. Forme

La spec compare quinze fois à l'intégration Ecoledirecte existante, dont
plusieurs fois sur un registre comparatif (« on ne le copie pas », « ce n'est pas
compatible avec », « leur »).

Les arguments **techniques** sont bons et doivent rester : un `backoff` qui
relogue hors admission défait le chokepoint ; un DEBUG de jeton est une fuite ;
un login par cycle est incompatible avec un plafond de vingt-quatre par jour. Ce
sont des arguments d'exigence.

Le cadrage comparatif, lui, tombe sous la même logique que la consigne permanente
du dépôt sur l'autre intégration PRONOTE : argumenter par les exigences, jamais
par comparaison. Deux ou trois reformulations suffisent, et le document devient
publiable tel quel s'il est un jour fusionné dans `SPECIFICATION.md`.

---

## 6. Ordre de traitement suggéré

1. **§2.3** — la barrière de couverture : correction de
   `scripts/check_coverage.py`, indépendante du chantier, bonne à prendre tout de
   suite.
2. **§1.1**, **§1.2**, **§1.3** — les trois qui changent le design.
3. **§2.1**, **§2.2**, **§2.4**, **§3.3**, **§3.4** — à écrire dans la spec avant
   l'étape 1 de l'ordre d'atterrissage : ce sont des décisions, pas des détails
   d'implémentation.
4. **§3.1**, **§3.2**, **§3.5**, **§3.6**, **§4**, **§5** — rédaction.

---

## 7. Suite — après la réponse du 11 septembre

[`…-reponse.md`](./2026-09-11-connecteurs-modele-commun-reponse.md) répond point
par point ; la spec est passée de 801 à 840 lignes. Relecture faite : **les
treize constats sont traités, aucun n'a été abandonné en silence**, et les six
objections sont recevables. Deux appellent une correction de détail, le
correctif du §1.2 introduit un défaut neuf, et le seul changement de code a été
vérifié plutôt que relu.

### 7.1 Objections : recevables

- **O2** — `duration=0`, pas de conversion. L'arithmétique est juste :
  `Lesson.duration` est un entier, un cours ED de 55 minutes se convertirait en
  `0` de toute façon, et la conversion inventerait une précision que `duree` n'a
  jamais eue. Retenu tel quel.
- **O4** — `SESSION` **hors** `capabilities.tiers`. Va plus loin que la revue ne
  demandait, et a raison : un palier présent dans le frozenset et oublié
  d'`async_collect` est exactement le trou que `ConnectorUnsupportedError`
  existe pour crier. Meilleure réponse que « inoffensif si documenté ».
- **O5** — garder la citation du handshake 0.3.0. La distinction est juste :
  « on ne copie pas leur `select` » est un cadrage comparatif ; « la source de
  vérité du transport est 0.3.0 et non la doc EduWire 2024 » est une exigence,
  sans laquelle un lecteur câble le mauvais login. Les premières sont sorties,
  la seconde reste. La revue visait le registre, pas la référence.
- **O6** — les deux barrières plutôt que l'alternative. Correct, et c'est la
  bonne lecture d'une revue qui offrait un choix : l'indexation par chemin est
  le correctif, les noms `ed_*` sont une ceinture.
- **O1** et **O3** — retenues, avec les réserves des §7.3 et §7.4.

### 7.2 Un défaut neuf, introduit par le correctif du §1.2

`detention=False` + `status="PERMANENCE"` est le bon arbitrage. La ligne qui
l'accompagne ne l'est pas : « `typeCours` autre que `COURS` / `PERMANENCE` →
`status` = la **valeur brute** » fait passer du vocabulaire de protocole Aplim
dans une chaîne visible par l'utilisateur, ce qui n'était pas le cas avant.

`Lesson.status` n'est pas un champ interne. `_timetable_events` le place en
**première ligne de la description de l'événement de calendrier** ;
`_lesson_dict` le publie en attribut de carte ; `change_signature` en fait un
`lesson_status_changed`. Côté Pronote, `Statut` est de la prose d'établissement
(« Cours annulé », « Prof absent ») ; côté ED ce serait un jeton d'énumération
inconnu à l'avance, en capitales, rendu verbatim dans un agenda que des gens
lisent.

**Correctif :** un ensemble **fermé** de `typeCours` connus, mappés
explicitement ; tout le reste à `None`. C'est aussi ce qu'impose la règle
« rien de visible par l'utilisateur n'est codé en dur sous
`custom_components/` ».

### 7.3 O1 — la décision est bonne, les chiffres qui la portent sont faux

La réponse écrit « les 67 `.gateway` ne sont pas de l'horloge » et cite
`services.py`, `attachment.py`, `todo.py`, `image.py` et le capteur de durée de
vie de session. Ventilation réelle :

| Module | `.gateway` | dont horloge |
| --- | --- | --- |
| `sensor.py` | 23 | **23** |
| `binary_sensor.py` | 16 | **16** |
| `calendar.py` | 1 | **1** |
| `tiers.py` | 15 | 2 |
| `services.py`, `attachment.py`, `todo.py`, `image.py` | 12 | 0 |

42 des 67 *sont* de l'horloge ; 12 ne le sont pas. La coupe en deux PR — « qui
donne l'heure » / « qui possède le fil » — est exactement la bonne et tombe pile
sur cette frontière. Mais la Task 3 du plan doit être dimensionnée sur une
quarantaine de sites dans trois modules d'entités, pas sur le reliquat. Une
phrase à corriger, sinon la PR est sous-évaluée au moment de la planifier.

### 7.4 O3 — accepté, avec une dette à écrire

Ne pas toucher au mapping Pronote est un bon arbitrage de périmètre : changer le
producteur modifierait les charges utiles `delay_added` d'instances qui tournent.

Reste que `PronoteGateway` construit son `Delay` avec `minutes=int(… or 0)`.
Après ce chantier, le type sera `int | None` et **seul ED** honorera la règle :
Pronote continuera de fabriquer un `0` quand la bibliothèque ne donne rien.
C'est défendable comme dette datée, pas comme état stable. Une ligne dans la
spec évite qu'on la redécouvre plus tard comme une incohérence.

### 7.5 `scripts/check_coverage.py` — vérifié, deux réserves

Vérifié et non relu :

- `ruff check` et `ruff format --check` : verts sur le script et son test ;
- `pytest tests/test_check_coverage.py` dans `ha-pronote-test-2026.9.0` :
  **4 passed** ;
- la logique tient — `pronote_ng/connectors/ecoledirecte/ratelimit.py` ne se
  termine pas par `/pronote_ng/ratelimit.py`, donc pas de collision ; un suffixe
  ambigu rend `None`, et `main` en fait un échec. **Fail closed confirmé.**

Deux réserves :

1. **Rien ne teste `main()`.** Les quatre tests portent sur `coverage_for` et
   `_module_coverage`. Le docstring du script dit qu'un seuil qui n'échoue pas
   n'est pas un seuil : il manque l'assertion qui le démontre — un rapport où un
   module critique est ambigu, et `main()` qui rend `1`.
2. Le message d'échec dit `is absent from the coverage report` dans le cas
   **ambigu**. Pour qui lit un CI rouge, c'est une fausse piste : le fichier est
   là, en double.

### 7.6 §2.1 atterrit au bon endroit

Confirmation utile : `_current_period_name` prend un `SessionFacts` — le
snapshot du coordinateur — et non `account.state`. Republier le coordinateur
`SESSION` déplace donc bien l'entité.

À préciser tout de même que `AccountState.periods` / `current_period` est une
comptabilité **séparée**, lue par `periods_for` et par les garde-fous `_marks` /
`_attendance` / `_evaluations`. Inerte côté ED, puisque `HISTORY` n'est pas
capable — mais quelqu'un qui n'en corrige qu'une moitié ne verra rien casser
tout de suite.

### 7.7 Reste ouvert

| # | Point | Nature |
| --- | --- | --- |
| 7.2 | `typeCours` inconnu → `status` brut dans un agenda | à trancher dans la spec |
| 7.3 | Dimensionnement de la Task 3 horloge | correction de chiffres |
| 7.4 | `minutes=int(… or 0)` côté Pronote | dette à dater par écrit |
| 7.5.1 | Test `main()` de la barrière de couverture | code, non écrit |
| 7.5.2 | Message « absent » pour un suffixe ambigu | code, non écrit |
| 7.6 | `AccountState` vs coordinateur `SESSION` | précision de rédaction |

Rien d'autre n'est en suspens : les §1 à §5 de cette revue sont soldés.
