# ha-pronote

Intégration Home Assistant pour PRONOTE — seconde génération.

> **État : spécification.** Aucun code n'est encore écrit. Ce dépôt contient
> pour l'instant la conception détaillée de l'intégration.

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
subir : ordonnanceur multi-cadence, déduplication au niveau de l'appel, et un
limiteur de débit configurable dont l'état est lui-même observable dans Home
Assistant.

## La spécification

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
défaut ramené de ≈ 423 à **≈ 180 requêtes par jour**. Le détail est au §14 de la
spécification.

Le protocole sous-jacent est décrit à part, dans une spécification issue de la
lecture de `pronotepy` 2.15.6 :
<https://claude.ai/code/artifact/d839541d-519c-4345-bb3f-c2252a05e13b>

## Licence

MIT (à créer avant la première publication).
