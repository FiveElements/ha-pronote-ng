# Pronote NG — PRONOTE pour Home Assistant
[![Validate](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/validate.yml?branch=main&label=validate&logo=github)](https://github.com/FiveElements/ha-pronote-ng/actions/workflows/validate.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/hassfest.yml?branch=main&label=hassfest&logo=homeassistant&logoColor=white)](https://github.com/FiveElements/ha-pronote-ng/actions/workflows/hassfest.yml)
[![HACS](https://img.shields.io/github/actions/workflow/status/FiveElements/ha-pronote-ng/hacs.yml?branch=main&label=HACS)](https://github.com/FiveElements/ha-pronote-ng/actions/workflows/hacs.yml)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.9.0%2B-41BDF5?logo=homeassistant&logoColor=white)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)


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
  <a href="https://github.com/FiveElements/ha-pronote-ng">🐛 Signaler un problème </a>

</p>


 ## ✨ Fonctionnalités

 ### 📅 Vie scolaire

 - Emploi du temps
- Prochains cours
- Devoirs
- Notes et moyennes
- Évaluations par compétences
- Absences
- Retards
- Punitions
- Bulletins
- Actualités
- Discussions
- Menus de cantine
- Informations sur le personnel

 ### 🤖 Automatisations Home Assistant

 Pronote NG s'intègre nativement dans le moteur d'automatisation de Home Assistant.

 Vous pouvez notamment créer des automatisations lorsqu'une nouvelle note apparaît, lorsqu'un devoir est ajouté ou lorsqu'un événement scolaire change.

 L'intégration fournit :

 - 14 déclencheurs d'appareil
- 10 conditions
- 4 actions
- 7 blueprints prêts à l'emploi
- des blueprints disponibles en français et en anglais

 Aucune programmation en YAML n'est nécessaire pour les automatisations courantes.


 ### 👨‍👩‍👧‍👦 Comptes parents

 Les comptes parents avec plusieurs enfants sont pris en charge.

 Chaque enfant est représenté comme un appareil Home Assistant distinct, tout en partageant la même session PRONOTE et le même budget de requêtes.

 ### 🔐 Connexion sécurisée

 Trois méthodes de connexion sont disponibles :

 1. **QR code — recommandé**
2. Identifiant et mot de passe
3. Connexion via ENT

 L'enrôlement par QR code permet à Home Assistant de s'enregistrer comme appareil auprès de PRONOTE sans avoir à conserver votre mot de passe.

 ### 🛡️ Protection contre les requêtes excessives

 PRONOTE peut limiter les requêtes provenant d'une même adresse IP.

 Pronote NG intègre donc un limiteur de requêtes à plusieurs niveaux afin d'éviter les sollicitations excessives :

 - espacement minimal entre les requêtes
- seau à jetons horaire
- plafond quotidien
- compteurs de connexion séparés
- cadence configurable des différentes collectes

 Le budget par défaut est d'environ **180 requêtes par jour et par enfant**.

 L'intégration estime également le coût des réglages avant leur application afin de rendre leur impact visible.

---

 # 🚀 Installation

 ## Avec HACS

 Pronote NG peut être installé avec [HACS](<https://www.hacs.xyz/>) comme dépôt personnalisé.

 1. Ouvrez **HACS → Intégrations**
2. Ouvrez le menu **⋮**
3. Sélectionnez **Dépôts personnalisés**
4. Ajoutez :

```
https://github.com/FiveElements/ha-pronote-ng
```

 5. Sélectionnez la catégorie **Intégration**
6. Installez **Pronote NG**
7. Redémarrez Home Assistant
8. Allez dans **Paramètres → Appareils et services**
9. Cliquez sur **Ajouter une intégration**
10. Recherchez **PRONOTE**

 > 💡 Une intégration HACS officielle pourra être utilisée lorsqu'elle sera disponible. En attendant, l'installation par dépôt personnalisé permet d'utiliser Pronote NG directement depuis HACS.

 ## Installation manuelle

 Copiez le dossier :

```
custom_components/pronote_ng/
```

 dans le dossier `custom_components/` de votre configuration Home Assistant, puis redémarrez Home Assistant.

 ### Version minimale

 Pronote NG nécessite actuellement :

```
Home Assistant 2026.9.0 ou supérieur
```

---

 # 📊 Afficher les données PRONOTE

 Pronote NG fournit des entités Home Assistant natives :

 - `sensor`
- `binary_sensor`
- `calendar`
- `todo`
- `image`
- `event`
- `button`

 Vous pouvez donc utiliser les cartes natives de Home Assistant pour construire votre propre tableau de bord.

 La documentation explique également comment créer un tableau de bord complet à partir des données PRONOTE.

 👉 Afficher les données PRONOTE dans Home Assistant

---

 # 🎨 Cartes Lovelace

 Des cartes Lovelace spécialement conçues pour Pronote NG sont disponibles dans un dépôt séparé :

 **FiveElements/ha-pronote-ng-cards**

 Elles permettent notamment d'afficher :

 - 👨‍🎓 Informations sur l'élève
- 📅 Prochain cours
- 🗓️ Emploi du temps
- 📝 Devoirs
- 📊 Notes
- 🎯 Évaluations
- 🍽️ Cantine
- 🏫 Vie scolaire
- 🚦 État du limiteur

 Les cartes sont **optionnelles** : Pronote NG fonctionne avec les cartes natives de Home Assistant.

---

 # 🔧 Services et actions

 Pronote NG fournit plusieurs services Home Assistant.

 Certains services permettent notamment d'accéder à :

 - l'URL iCal de l'emploi du temps
- l'identité de l'élève
- l'emploi du temps au format PDF
- l'état du limiteur de requêtes

 Des opérations d'écriture sont également disponibles mais sont **désactivées par défaut**, notamment :

 - cocher un devoir
- marquer une actualité comme lue
- envoyer un message

---

 # 🔒 Vie privée et sécurité

 La protection des données PRONOTE est une priorité du projet.

 Les informations sensibles ne sont pas stockées dans les états des entités ou dans leurs attributs.

 Cela concerne notamment :

 - l'URL iCal
- les informations d'identité
- le lien vers le PDF de l'emploi du temps
- les informations d'authentification

 Les données sensibles sont fournies uniquement lorsqu'elles sont explicitement demandées par un service.

 Le fichier de diagnostic ne contient pas le contenu des données scolaires. Les identifiants d'élèves y sont également protégés.

 > ⚠️ **Ne configurez jamais le journaliseur `pronotepy` en niveau DEBUG.**
>
>  Le mode DEBUG de `pronotepy` peut contenir des informations sensibles provenant des requêtes PRONOTE.

---

 # 🧠 Pourquoi Pronote NG ?

 Pronote NG a été conçu autour de quelques principes importants.

 ### Ne jamais confondre une absence de données avec une donnée vide

 Une liste vide peut être parfaitement valide : par exemple, une semaine sans devoirs.

 À l'inverse, la disparition inattendue d'une donnée obligatoire peut indiquer un changement du protocole PRONOTE.

 Pronote NG distingue donc ces deux situations afin d'éviter de publier silencieusement des données incorrectes dans Home Assistant.

 ### Une session partagée pour plusieurs enfants

 Avec un compte parent, chaque enfant dispose de son propre appareil Home Assistant, tout en partageant la même session PRONOTE.

 Cela évite de multiplier inutilement les connexions.

 ### Des événements utilisables directement dans les automatisations

 Les événements permettent de réagir précisément aux changements.

 Par exemple :

 > « Une nouvelle note vient d'être publiée en mathématiques. »

 plutôt que simplement :

 > « Le nombre de notes a changé. »

 Cela permet de créer des automatisations Home Assistant plus fiables et plus simples.

---

 # 📚 Documentation

 La documentation complète est disponible ici :

 **📖 Documentation Pronote NG**

 Elle contient notamment :

 - Guide utilisateur
- Architecture de l'intégration
- Catalogue des entités et services
- Configuration du limiteur de requêtes
- Création de tableaux de bord
- Blueprints
- Migration depuis l'ancienne intégration `pronote`
- Documentation technique pour les contributeurs

---

 # 🔄 Migration depuis l'ancienne intégration

 Pronote NG est une nouvelle génération de l'intégration PRONOTE pour Home Assistant.

 Une documentation dédiée explique comment migrer les automatisations existantes depuis l'autre intégration `pronote`.

 👉 Guide de migration

---

 # 🧑‍💻 Développement

 Le projet utilise notamment :

 - Python
- Home Assistant
- `ruff`
- `mypy`
- `pytest`
- MkDocs Material

 Les tests et contrôles qualité sont exécutés automatiquement par la CI.

 Pour contribuer :

```
pip install -r requirements_test.txt

ruff check .
ruff format --check .

mypy --strict custom_components/ scripts/

pytest tests --cov=custom_components/pronote_ng --cov-branch
```

 Pour travailler sur la documentation :

```
pip install -r requirements_docs.txt

mkdocs serve
mkdocs build --strict
```

 Consultez également CONTRIBUTING.md avant de proposer une contribution.

---

 # 🤝 Contribuer

 Les contributions, rapports de bugs et suggestions sont les bienvenus.

 Avant d'ouvrir une issue :

 - vérifiez la documentation
- utilisez les formulaires d'issue disponibles
- ne partagez jamais d'identifiants PRONOTE réels
- ne joignez jamais de données scolaires personnelles
- ne joignez jamais de logs `pronotepy` en mode DEBUG

 👉 Créer une issue

---

 # ⚠️ Important

 PRONOTE est un service tiers.

 **Pronote NG n'est pas développé, maintenu ou officiellement supporté par Index Éducation / PRONOTE.**

 Le fonctionnement de l'intégration dépend de l'API et du comportement de PRONOTE. Une évolution de PRONOTE ou de l'ENT utilisé par votre établissement peut nécessiter une adaptation de l'intégration.

---


 ## ⭐ Vous utilisez Pronote NG ?

 Si cette intégration vous est utile :

 - ⭐ ajoutez une étoile au projet GitHub
- 🐛 signalez les problèmes rencontrés
- 💡 proposez des améliorations
- 📖 contribuez à la documentation
- 📣 partagez le projet avec d'autres utilisateurs de Home Assistant

 Chaque étoile et chaque contribution aide le projet à être découvert par les utilisateurs qui recherchent une intégration **PRONOTE pour Home Assistant**.


## Contribuer

[`CONTRIBUTING.md`](CONTRIBUTING.md) : comment monter l'environnement, faire
tourner la suite sous Windows, les quatre fichiers qui se génèrent et ne
s'éditent pas, et les deux règles non négociables — jamais un identifiant
PRONOTE réel dans le dépôt, jamais le journaliseur `pronotepy` en DEBUG.

Pour signaler un problème, utilisez les
[formulaires d'issue](https://github.com/FiveElements/ha-pronote-ng/issues/new/choose) :
ils demandent ce qu'il faut, et rappellent surtout ce qu'il ne faut **pas**
coller dans une issue publique.

 # 📄 Licence

 Pronote NG est distribué sous licence [MIT](LICENSE).

---


