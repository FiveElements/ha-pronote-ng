# Pronote NG — PRONOTE pour Home Assistant

<p align="center">
  <img src="docs/assets/logo.png" alt="Pronote NG" width="180">
</p>

<p align="center">
  <strong>Intégration Home Assistant pour PRONOTE — seconde génération.</strong>
</p>

 **Pronote NG** est une intégration [Home Assistant](<https://www.home-assistant.io/>) permettant d'intégrer **PRONOTE** directement dans votre installation domotique.

 Retrouvez dans Home Assistant les informations scolaires de vos enfants : **emploi du temps, devoirs, notes, moyennes, absences, retards, évaluations, actualités, discussions, menus et bien plus encore.**

 L'intégration est conçue pour les élèves comme pour les parents utilisant un compte PRONOTE ou un ENT.
 > 🚀 **Pronote NG est la nouvelle génération de l'intégration PRONOTE pour Home Assistant.**


<p align="center">
  <a href="https://fiveelements.github.io/ha-pronote-ng/">📖 Documentation</a>
  ·
  <a href="https://fiveelements.github.io/ha-pronote-ng/GUIDE-UTILISATEUR/">📚 Guide de l'utilisateur</a>
  ·
  <a href="https://fiveelements.github.io/ha-pronote-ng/ARCHITECTURE/">🐛 Signaler un problème · [💻 GitHub](<https://github.com/FiveElements/ha-pronote-ng>)
</a>
</p>

<p align="center">
  <a href="https://github.com/FiveElements/ha-pronote-ng/actions/workflows/validate.yml"><img src="https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/validate.yml?branch=main&label=validate&logo=github" alt="Validate"></a>
  <a href="https://github.com/FiveElements/ha-pronote-ng/actions/workflows/hassfest.yml"><img src="https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/hassfest.yml?branch=main&label=hassfest&logo=homeassistant&logoColor=white" alt="Hassfest"></a>
  <a href="https://github.com/FiveElements/ha-pronote-ng/actions/workflows/hacs.yml"><img src="https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/hacs.yml?branch=main&label=HACS" alt="HACS"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.9.0%2B-41BDF5?logo=homeassistant&logoColor=white" alt="Home Assistant 2026.9.0 minimum">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-MIT-green" alt="Licence MIT"></a>
</p>

## Les points forts

**Le coût est affiché avant d'être dépensé.** Chaque page de réglages montre
l'estimation du nombre de requêtes quotidiennes que les valeurs saisies vont
coûter, calculée par la fonction même qui produit les chiffres de
[l'annexe B](docs/annexe-b-rate-limit.md) — la page et le document ne peuvent
donc pas diverger. Un réglage dont on ne voit pas la conséquence se règle au
hasard, et la conséquence se mesure ici en requêtes sur le compte scolaire d'un
enfant. En dessous, l'appareillage qui rend ce chiffre vrai : un limiteur à
trois étages (espacement minimal, seau à jetons horaire, plafond quotidien),
deux compteurs de connexion tenus à part, dix paliers de collecte ayant chacun
sa cadence, et **un seul battement maître**, parce que des minuteries
indépendantes finissent par coïncider et produire des rafales.

**Les données sensibles ne deviennent jamais un état.** L'URL iCal (qui donne
accès à l'emploi du temps complet d'un élève sans aucun identifiant), le bloc
d'identité et le lien du PDF d'emploi du temps sont des réponses de service
(`SupportsResponse.ONLY`) : rien ne les retient, ni un état, ni un attribut, ni
le fichier de diagnostic. Le code PIN à deux facteurs n'est pas persisté, ce
qui est d'ailleurs la raison pour laquelle l'intégration sait le redemander. Et
le diagnostic rapporte la *forme* de chaque collecte — quand elle date, ce
qu'elle a coûté, combien d'éléments elle contient — jamais son contenu : une
note ou le corps d'un message n'ont rien à faire dans un fichier que l'on colle
dans une issue publique. C'est de la conception, pas du filtrage en sortie.

**L'enrôlement par QR code évite de conserver un mot de passe.** C'est le mode
recommandé : Home Assistant s'inscrit auprès de PRONOTE comme un appareil, et
le jeton obtenu tourne à chaque authentification — il est donc réécrit dans
l'entrée de configuration après chaque connexion, sans quoi l'accès serait
perdu au démarrage suivant. Deux autres modes restent disponibles là où
celui-ci ne s'applique pas : identifiant et mot de passe, ou ENT.

**« Absent » n'est pas « vide ».** Quand une clé que le protocole garantit
disparaît de la réponse, le palier **échoue** au lieu de rapporter zéro
élément : il garde son instantané précédent, se marque périmé, puis ouvre un
signalement de réparation. Une liste vide est une information plausible (une
semaine sans devoirs existe), donc la confondre avec une rupture de protocole
publierait un mensonge crédible : les capteurs se videraient, la péremption ne
se déclencherait jamais puisque la collecte a « réussi », et l'automatisation du
matin cesserait simplement de partir, sans une ligne dans le journal.

**Un appareil par enfant, une seule session pour tous.** Sur un compte parent,
chaque enfant est un appareil distinct avec ses propres entités, tandis que la
session et le budget de requêtes sont partagés : un second enfant coûte ses
données, pas une seconde connexion. La sélection de l'enfant et l'appel forment
une fermeture indivisible tenue sous verrou, parce que l'unité de travail
atomique est le couple *(enfant, palier)* — sans quoi un compte parent publie
silencieusement l'emploi du temps d'un enfant sous les entités de l'autre.

**De quoi automatiser sans écrire de template.** Des entités `event` disent *ce
qui vient de changer* (quelle note, dans quelle matière, avec quel coefficient)
là où un déclencheur d'état ne sait dire que « le compteur a bougé » ; elles
sont émises après la publication des instantanés, de sorte qu'une automatisation
réagissant à l'arrivée d'une note trouve le capteur déjà à jour. S'y ajoutent
des déclencheurs, conditions et actions d'appareil, des blueprints prêts à
importer, et des services d'écriture — cocher un devoir, envoyer un message —
**désactivés par défaut**.

Ces choix sont tenus par la chaîne d'intégration continue : `ruff`,
`mypy --strict`, quelques centaines de tests, et un portail de couverture qui
exige **100 %** sur les quatre modules où une erreur ne produit aucun symptôme
visible — le limiteur, l'ordonnanceur, la passerelle et le détecteur de
changements.

## Ce que ça fait

- **Sept plateformes d'entités** : `sensor`, `binary_sensor`, `calendar`,
  `todo`, `image`, `event`, `button` — emploi du temps, devoirs, notes et
  moyennes, absences et retards, punitions, évaluations par compétences,
  actualités, discussions, menus, personnel, bulletins.
- **Huit services**, dont l'URL iCal, l'identité, le PDF d'emploi du temps et
  l'état du limiteur. Ces quatre-là sont des réponses de service
  (`SupportsResponse.ONLY`) et ne transitent par aucun état d'entité — les
  trois premiers parce qu'ils sont sensibles, le quatrième parce qu'un état de
  limiteur n'a pas d'utilisateur en dehors du moment où on le demande.
- **Écritures optionnelles**, coupées par défaut : cocher un devoir, marquer
  une actualité comme lue, envoyer un message.
- **Automatisations d'appareil** : 14 déclencheurs, 10 conditions, 4 actions,
  plus **sept blueprints**, livrés chacun en français et en anglais.
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

L'intégration s'appelle **Pronote NG** et son domaine Home Assistant est
**`pronote_ng`** : c'est le nom du dossier sous `custom_components/`, et le
préfixe de toutes ses entités et de tous ses services.

### HACS (dépôt personnalisé)

1. HACS → Intégrations → menu ⋮ → *Dépôts personnalisés*.
2. Ajouter `https://github.com/FiveElements/ha-pronote-ng`, catégorie
   *Intégration*.
3. Installer **Pronote NG**, puis redémarrer Home Assistant.
4. *Paramètres → Appareils et services → Ajouter une intégration → PRONOTE*.

### Manuellement

Copier `custom_components/pronote_ng/` dans le dossier `custom_components/` de
votre configuration, puis redémarrer.

Home Assistant **2026.9.0** minimum.

### Les cartes, dans un dépôt séparé

Neuf cartes Lovelace faites pour cette intégration — élève, prochain cours,
emploi du temps, devoirs, notes, évaluations, cantine, vie scolaire, limiteur —
vivent dans **[FiveElements/ha-pronote-ng-cards](https://github.com/FiveElements/ha-pronote-ng-cards)**
([documentation](https://fiveelements.github.io/ha-pronote-ng-cards/)). Elles
sont optionnelles : l'intégration publie des états primitifs, donc les cartes
intégrées de Home Assistant suffisent pour l'essentiel — voyez
[`docs/AFFICHER-LES-DONNEES.md`](docs/AFFICHER-LES-DONNEES.md), qui couvre les
deux chemins.

Elles se configurent avec **l'appareil de l'enfant**, jamais avec un identifiant
d'entité : les identifiants dérivent du nom affiché de l'enfant et changent
silencieusement s'il est renommé, tandis que la clé technique d'une entité est
stable dans toutes les langues.

## Documentation

Publiée sur <https://fiveelements.github.io/ha-pronote-ng/>, construite par
MkDocs Material depuis ce même dossier `docs/` — il n'y a donc pas de copie à
maintenir, et `mkdocs build --strict` fait échouer la *pull request* qui casse
un lien interne.

| Document | Contenu |
| --- | --- |
| [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) | **v2** — architecture, session, ordonnanceur, configuration, sécurité, i18n, qualité, jalons |
| [`docs/annexe-a-entites.md`](docs/annexe-a-entites.md) | Catalogue complet des entités et services, avec l'origine de chaque champ |
| [`docs/annexe-b-rate-limit.md`](docs/annexe-b-rate-limit.md) | Le limiteur de débit : couches, options, arithmétique du budget, repli |
| [`docs/revue-contradictoire-v1.md`](docs/revue-contradictoire-v1.md) | La revue qui a produit la v2 — conservée, parce que ses raisons valent mieux que ses conclusions seules |
| [`docs/GUIDE-UTILISATEUR.md`](docs/GUIDE-UTILISATEUR.md) | **Guide utilisateur** — installation, entités, services, réglages, dépannage |
| [`docs/AFFICHER-LES-DONNEES.md`](docs/AFFICHER-LES-DONNEES.md) | Construire un tableau de bord, avec les cartes intégrées de Home Assistant ou la bibliothèque de cartes |
| [`docs/BLUEPRINTS.md`](docs/BLUEPRINTS.md) | Chaque réglage des sept blueprints, et le piège que chacun évite |
| [`docs/MIGRATION-AUTOMATISATIONS.md`](docs/MIGRATION-AUTOMATISATIONS.md) | Déplacer des automatisations écrites contre l'autre intégration `pronote` |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Architecture interne, pour qui veut contribuer |

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

Pour la documentation :

```bash
pip install -r requirements_docs.txt
mkdocs serve           # aperçu local, rechargement à chaud
mkdocs build --strict  # ce que la CI exige
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

## Contribuer

[`CONTRIBUTING.md`](CONTRIBUTING.md) : comment monter l'environnement, faire
tourner la suite sous Windows, les quatre fichiers qui se génèrent et ne
s'éditent pas, et les deux règles non négociables — jamais un identifiant
PRONOTE réel dans le dépôt, jamais le journaliseur `pronotepy` en DEBUG.

Pour signaler un problème, utilisez les
[formulaires d'issue](https://github.com/FiveElements/ha-pronote-ng/issues/new/choose) :
ils demandent ce qu'il faut, et rappellent surtout ce qu'il ne faut **pas**
coller dans une issue publique.

## Licence

[MIT](LICENSE).
