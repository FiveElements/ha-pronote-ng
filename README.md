# ha-pronote

Intégration Home Assistant pour PRONOTE — seconde génération.

Domaine Home Assistant : **`pronote_ng`**, choisi pour cohabiter avec
l'intégration existante sans conflit de domaine.

> **État : implémentée.** 494 tests, `mypy --strict` propre, couverture sous
> portail (80 % global, 100 % sur le limiteur, l'ordonnanceur, la passerelle et
> le détecteur de changements).

## Pourquoi un nouveau module

L'intégration existante (`delphiki/hass-pronote`) fonctionne, mais sa forme
plafonne sur trois points structurels qu'on ne corrige pas par retouches :

1. **Un seul coordinateur, un seul intervalle.** Tout est rafraîchi au même
   rythme — l'emploi du temps comme les bulletins du trimestre passé.
2. **Aucune mutualisation des appels.** `pronotepy` ne met rien en cache : lire
   `period.grades` puis `period.averages` déclenche deux fois le même appel
   `DernieresNotes`. Le coordinateur actuel émet **26 appels par
   rafraîchissement** là où une dizaine suffirait.
3. **Pas de garde-fou côté serveur.** Rien ne limite la cadence, alors que le
   protocole PRONOTE punit l'excès (erreur `G=25`, suspension d'adresse IP).

Cette seconde génération part de ces trois contraintes plutôt que de les
subir : ordonnanceur multi-cadence à dix paliers, déduplication au niveau de
l'appel, et un limiteur de débit configurable dont l'état est lui-même
observable dans Home Assistant.

## Ce que ça fait

- **Sept plateformes d'entités** : `sensor`, `binary_sensor`, `calendar`,
  `todo`, `image`, `event`, `button` — emploi du temps, devoirs, notes et
  moyennes, absences et retards, punitions, évaluations par compétences,
  actualités, discussions, menus, personnel, bulletins.
- **Huit services**, dont l'URL iCal, l'identité, le PDF d'emploi du temps et
  l'état du limiteur. Les trois premiers sont des réponses de service
  (`SupportsResponse.ONLY`) et ne transitent par aucun état d'entité.
- **Écritures optionnelles**, coupées par défaut : cocher un devoir, marquer
  une actualité comme lue, envoyer un message.
- **Automatisations d'appareil** : 14 déclencheurs, 10 conditions, 4 actions,
  plus **14 blueprints** livrés en français et en anglais.
- **Trois modes de connexion** : QR code (recommandé), identifiants directs,
  ENT.
- **Comptes parents multi-enfants**, chaque enfant étant un appareil distinct.

## Ce que ça protège

PRONOTE sanctionne une **adresse IP**, pas seulement un compte. Le limiteur
(`custom_components/pronote_ng/ratelimit.py`) est donc au centre du design :
trois couches — espacement minimal, seau à jetons, plafond journalier — plus
deux compteurs de connexion, tous **débités à l'admission** sous un verrou,
jamais à la sortie. Un login coûte cinq à sept requêtes et la poignée de main
est lente ; facturé au retour, il laisse une fenêtre de plusieurs secondes
pendant laquelle un second appelant lit un budget intact.

Le budget par défaut est d'environ **180 requêtes par jour** pour un enfant,
contre ≈ 423 dans la première version de la spécification.

## Installation

### HACS (dépôt personnalisé)

1. HACS → Intégrations → menu ⋮ → *Dépôts personnalisés*.
2. Ajouter `https://github.com/FiveElements/ha-pronote-ng`, catégorie
   *Intégration*.
3. Installer **ha-pronote**, puis redémarrer Home Assistant.
4. *Paramètres → Appareils et services → Ajouter une intégration → PRONOTE*.

### Manuellement

Copier `custom_components/pronote_ng/` dans le dossier `custom_components/` de
votre configuration, puis redémarrer.

Home Assistant **2026.2.0** minimum.

## Documentation

| Document | Contenu |
| --- | --- |
| [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) | **v2** — architecture, session, ordonnanceur, configuration, sécurité, i18n, qualité, jalons |
| [`docs/annexe-a-entites.md`](docs/annexe-a-entites.md) | Catalogue complet des entités et services, avec l'origine de chaque champ |
| [`docs/annexe-b-rate-limit.md`](docs/annexe-b-rate-limit.md) | Le limiteur de débit : couches, options, arithmétique du budget, repli |
| [`docs/revue-contradictoire-v1.md`](docs/revue-contradictoire-v1.md) | La revue qui a produit la v2 — conservée, parce que ses raisons valent mieux que ses conclusions seules |

La v1 a été révisée après une revue contradictoire dont **quatorze affirmations
porteuses sur quatorze** se sont vérifiées dans la source. Les changements :
stratégie de session inversée, règle de détection des changements dédoublée,
client durci rendu prérequis, paliers réduits de treize à dix, et budget par
défaut ramené de ≈ 423 à ≈ 180 requêtes par jour. Le détail est au §14 de la
spécification.

## Développement

La suite de tests **ne tourne pas sous Windows** :
`pytest-homeassistant-custom-component` importe `fcntl`. Sous Linux ou WSL :

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
mypy --strict custom_components/ scripts/
pytest tests --cov=custom_components/pronote_ng --cov-branch
python scripts/check_coverage.py coverage.xml
```

Sous Windows, la moitié de la suite sans dépendance à Home Assistant — le
limiteur, l'ordonnanceur, la passerelle, le détecteur de changements, les DTO,
l'estimateur de budget — s'exécute directement ; le reste se saute avec un
marqueur explicite. C'est cette moitié qui porte le portail à 100 %.

Le portail de couverture exige 80 % global et **100 %** sur `ratelimit.py`,
`scheduler.py`, `gateway.py` et `delta.py`, en prenant pour chacun le minimum
de la couverture de lignes et de branches.

## Vie privée

Aucun secret ne transite par un état d'entité, un attribut ou le fichier de
diagnostic : ni l'URL iCal — qui donne accès à l'emploi du temps complet d'un
élève sans aucun identifiant — ni le bloc d'identité, ni le lien du PDF. Ce
sont des réponses de service, et rien ne les stocke. Les identifiants d'élève
n'apparaissent dans les diagnostics que sous forme d'empreinte tronquée.

Le journaliseur `pronotepy` ne doit **jamais** être passé en DEBUG : il y écrit
l'hexadécimal de chaque corps de requête, identifiants compris. L'intégration
ne configure aucun journaliseur et ne déclare aucune clé `loggers` dans son
manifeste, et un test le vérifie.

## Licence

[MIT](LICENSE).
