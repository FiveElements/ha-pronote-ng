# Contribuer à Pronote NG

Merci de l'intérêt. Ce document dit comment travailler sur ce dépôt, et
surtout **pourquoi** certaines règles sont plus strictes qu'ailleurs : cette
intégration manipule les identifiants scolaires d'enfants et parle à un serveur
qui sanctionne une adresse IP. Deux contraintes qui façonnent tout le reste.

Le dépôt s'appelle `ha-pronote`, l'intégration s'appelle **Pronote NG**, et son
domaine Home Assistant est `pronote_ng`.

---

## 1. Les deux règles non négociables

### 1.1 Jamais un identifiant réel dans ce dépôt

Aucune *pull request* ne peut contenir un identifiant PRONOTE réel, un mot de
passe, un code PIN, un jeton `jetonConnexionAppliMobile`, le contenu d'un QR
code, une URL iCal (`icalsecurise=`), un nom d'élève, un nom d'établissement ou
un identifiant `N` réel — ni dans le code, ni dans un test, ni dans une
*fixture*, ni dans un message de commit, ni dans une capture d'écran.

Les *fixtures* de `tests/fixtures/` sont **écrites à la main** et non
enregistrées depuis un serveur réel. Toute valeur d'exemple doit être
manifestement fictive : `demo.example.invalid`, `Enfant Un`, `STUDENT-1`,
`not-a-real-password`, `0000000000A`. Si vous avez besoin d'une charge utile
capturée en vrai pour comprendre un bug, elle va dans `tests/fixtures/_raw/`,
que `.gitignore` exclut, et vous en dérivez une version anonymisée.

Une URL iCal mérite une mention à part : elle donne accès à l'emploi du temps
complet d'un élève **sans aucun identifiant**. Traitez-la comme un mot de
passe.

### 1.2 Jamais le journaliseur `pronotepy` en DEBUG

`pronotepy/pronoteAPI.py` écrit l'hexadécimal de chaque corps de requête au
niveau DEBUG, identifiants compris. Cette intégration ne configure donc aucun
journaliseur et ne déclare **aucune clé `loggers`** dans son manifeste — c'est
`scripts/check_manifest.py` qui l'empêche, et un test qui vérifie qu'un cycle
complet ne journalise aucun secret.

Pour déboguer, activez le journaliseur de l'intégration seulement :

```yaml
logger:
  logs:
    custom_components.pronote_ng: debug
```

N'ajoutez jamais `pronotepy` à cette liste, et ne le suggérez à personne dans
une issue.

---

## 2. Mettre en place l'environnement

### 2.1 Sous Linux, macOS ou WSL

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
```

### 2.2 Sous Windows

**La suite de tests complète ne tourne pas sous Windows.**
`pytest-homeassistant-custom-component` s'enregistre comme *entry point* pytest
et importe `fcntl`, qui n'existe pas. Ce n'est pas contournable : le plugin est
chargé avant toute lecture de `conftest.py`.

Deux options.

La moitié de la suite qui ne dépend pas de Home Assistant s'exécute quand même
en désactivant le plugin — et c'est précisément cette moitié qui porte le
portail à 100 % : le limiteur, l'ordonnanceur, la passerelle, le détecteur de
changements, les DTO et l'estimateur de budget.

```powershell
pytest tests -p no:homeassistant
```

Attention : cette invocation désactive aussi le garde-fou réseau du plugin.
N'écrivez jamais un test qui sortirait sur le réseau, et vérifiez toujours vos
changements avec la suite complète avant d'ouvrir une PR.

Pour la suite complète, passez par Docker :

```bash
docker run --rm -v "$PWD:/work" -w /work python:3.13 bash -c \
  "pip install -q -r requirements_test.txt && python -m pytest tests"
```

Sous Git Bash pour Windows, préfixez par `MSYS_NO_PATHCONV=1` et donnez le
chemin en absolu, sinon MSYS réécrit `/work` :

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:/chemin/vers/ha-pronote:/work" -w /work \
  python:3.13 bash -c "pip install -q -r requirements_test.txt && python -m pytest tests"
```

Construire une image une fois pour toutes évite de réinstaller les dépendances
à chaque exécution — la pile de test de Home Assistant est volumineuse.

---

## 3. Les portails

Ce sont exactement ceux que la CI applique
(`.github/workflows/validate.yml`). Faites-les passer avant d'ouvrir une PR :
ils échouent plus vite chez vous que dans un *runner*.

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/pronote_ng
python scripts/check_manifest.py
pytest tests --cov=custom_components/pronote_ng --cov-branch --cov-report=xml:coverage.xml
python scripts/check_coverage.py coverage.xml
```

### 3.1 La couverture

Le portail exige **80 %** globalement, et **100 %** sur quatre modules :

| Module | Pourquoi 100 % |
| --- | --- |
| `ratelimit.py` | un limiteur qui laisse passer un appel ne produit aucun symptôme visible — jusqu'à la sanction |
| `scheduler.py` | un palier sauté ressemble à des données un peu vieilles |
| `gateway.py` | un décodage faux produit une valeur plausible |
| `delta.py` | un changement manqué, c'est un évènement qui n'arrive jamais |

Pour chacun, la métrique retenue est le **minimum de la couverture de lignes et
de branches** : 100 % de lignes avec une branche manquante ne passe pas.

### 3.2 La documentation

```bash
pip install -r requirements_docs.txt
mkdocs serve           # aperçu local
mkdocs build --strict  # ce que la CI exige
```

`--strict` échoue sur un lien interne ou une ancre qui ne résout plus. Les
documents utilisent les *slugs* de GitHub — accents compris — pour se lire
identiquement sur GitHub et sur le site.

---

## 4. Ce qui se génère et ne s'édite pas

Quatre fichiers sont produits par un script. Les modifier à la main casse un
test qui compare le fichier à ce que le générateur produit.

| Fichier | Source |
| --- | --- |
| `custom_components/pronote_ng/strings.json` | `scripts/build_translations.py` |
| `custom_components/pronote_ng/translations/en.json` | idem |
| `custom_components/pronote_ng/translations/fr.json` | idem |
| `custom_components/pronote_ng/services.yaml` | idem |

Modifiez la table dans le script, puis :

```bash
python scripts/build_translations.py
```

**Rien de visible par l'utilisateur n'est écrit en dur sous
`custom_components/`.** C'est ce qui rend réel le test qui vérifie que les deux
langues ont les mêmes clés et les mêmes espaces réservés : trois fichiers JSON
maintenus à la main gardent ce test rouge pour des raisons sans intérêt.

Deux détails qui font échouer hassfest si on les ignore : une chaîne de
traduction **ne peut pas contenir d'URL** (passez-la en
`description_placeholders`), et les clés de `manifest.json` doivent être
ordonnées `domain`, `name`, puis alphabétiquement.

---

## 5. La frontière `pronotepy`

Deux modules seulement voient la bibliothèque amont : `gateway.py` et
`hardened_client.py`. Tout le reste manipule des DTO gelés (`frozen`, `slots`)
définis dans `models.py`.

Ce n'est pas une préférence esthétique. `pronotepy` expose des propriétés
paresseuses — `Information.content`, `Period.grades`, `Discussion.messages` —
dont chaque lecture **place une requête HTTP**. Un objet amont qui s'échappe
vers un capteur, c'est un appel réseau depuis la boucle d'évènements, hors de
tout comptage du limiteur. La frontière DTO l'exclut par construction.

Corollaires, si vous touchez à ces deux modules :

- une `GatewayResult` déclare son coût réel en requêtes (`calls`), et la
  session réconcilie ce coût avec le limiteur ; une déclaration fausse est un
  bug, pas une approximation ;
- une entrée illisible coûte **cette entrée**, jamais la collecte entière —
  mais une **clé absente** fait échouer le palier, parce que « absent » et
  « vide » ne sont pas la même information et qu'un changement de protocole
  doit se voir ;
- `hardened_client.py` existe pour corriger des bugs de l'amont, avec la
  version épinglée `pronotepy==2.15.6`. Si vous relevez cette épingle, relisez
  chaque divergence documentée dans ce fichier.

---

## 6. Le limiteur

Toute modification qui ajoute un appel réseau doit passer par le limiteur, et
déclarer son coût. Les trois couches — espacement minimal, seau à jetons,
plafond journalier — plus les deux compteurs de connexion sont **débités à
l'admission**, sous un verrou, jamais à la sortie.

La raison est mesurable : une connexion coûte cinq à sept requêtes et la
poignée de main PRONOTE est lente. Facturée au retour, elle laisse une fenêtre
de plusieurs secondes pendant laquelle un second appelant lit un budget intact
— c'est ainsi qu'un plafond de cinq connexions en laisse passer dix.

Si votre changement modifie le budget par défaut (≈ 180 requêtes par jour pour
un enfant), dites-le explicitement dans la PR et mettez à jour
`docs/annexe-b-rate-limit.md`.

---

## 7. Écrire des tests

Les tests de ce dépôt suivent trois conventions.

**Un nom de test est une phrase.** `test_a_login_is_charged_before_the_handshake_returns`,
pas `test_login_charge`. Le lecteur suivant doit comprendre ce qui est protégé
sans lire le corps.

**Une docstring dit pourquoi le test existe.** Idéalement quel défaut il
empêche de revenir. Un test dont personne ne sait à quoi il sert est un test
que quelqu'un supprimera pour faire passer la CI.

**Une seule couture est bouchonnée.** La *fixture* `account` de
`tests/conftest.py` remplace `session.build_client` et fait tourner tout le
reste pour de vrai — limiteur, ordonnanceur, session, passerelle,
coordinateurs et les sept plateformes ensemble. Les *fixtures* de
`tests/fixtures/protocol.py` rendent les dictionnaires bruts du protocole, que
les vraies classes `pronotepy.dataClasses` décodent : la fidélité est
structurelle, pas déclarative.

Si vous ajoutez une méthode à `FakeClient`, faites-lui compter ses requêtes
comme la vraie bibliothèque les place. Un double qui sous-compte rend le
contrat de coût indémontrable.

---

## 8. Ouvrir une pull request

1. Une branche par sujet, depuis `main`.
2. Les portails de la section 3 au vert **en local**.
3. Un message de commit qui dit ce qui change **et pourquoi**. Si vous
   corrigez un bug, décrivez le symptôme qu'un utilisateur voyait ; c'est ce
   qui permet à quelqu'un de juger si votre correction est la bonne.
4. Les quatre workflows (`Validate`, `Hassfest`, `HACS`, `Docs`) au vert sur la
   PR.

N'incrémentez **pas** la version dans `manifest.json` : c'est fait au moment de
la publication, et `release.yml` refuse un tag dont le manifeste diverge.

---

## 9. Publier une version

Réservé aux mainteneurs.

1. Porter `manifest.json` à la version voulue, sans le `v`.
2. Fusionner sur `main`, workflows au vert.
3. `git tag -a vX.Y.Z && git push origin vX.Y.Z`.

`release.yml` vérifie que le manifeste et le tag concordent — une intégration
HACS dont le manifeste diverge du tag s'installe une fois et ne se met plus
jamais à jour — puis construit `pronote_ng.zip` et publie la *release*.

---

## 10. Signaler un problème

Utilisez les formulaires d'issue : ils demandent exactement ce qu'il faut et,
surtout, ils rappellent ce qu'il ne faut **pas** coller.

Le fichier de diagnostic (*Paramètres → Appareils et services → Pronote NG →
⋯ → Télécharger les diagnostics*) est conçu pour être joint à une issue
publique : les identifiants y sont rédigés, l'adresse retaillée, et les
identifiants d'élève remplacés par une empreinte tronquée. C'est presque
toujours la pièce jointe la plus utile.

En revanche, **relisez toujours** ce que vous collez. Un extrait de journal, un
message d'erreur brut ou une capture d'écran ne passent par aucune rédaction.

---

## 11. Où lire la suite

| Document | Contenu |
| --- | --- |
| [`docs/GUIDE-UTILISATEUR.md`](docs/GUIDE-UTILISATEUR.md) | ce que voit l'utilisateur : entités, services, réglages, dépannage |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | les couches, le limiteur, l'ordonnanceur, la session, et pourquoi c'est construit ainsi |
| [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) | le raisonnement d'origine — à lire avec le §12 de l'architecture, qui liste où le code s'en écarte |
| [`docs/annexe-b-rate-limit.md`](docs/annexe-b-rate-limit.md) | l'arithmétique du budget de requêtes |
| [`docs/revue-contradictoire-v1.md`](docs/revue-contradictoire-v1.md) | la revue qui a produit la v2, conservée parce que ses raisons valent mieux que ses conclusions |

Le tout est publié sur <https://fiveelements.github.io/ha-pronote-ng/>.

Sous licence [MIT](LICENSE).
