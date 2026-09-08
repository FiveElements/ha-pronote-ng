# Revue contradictoire — spécification v1

Relecture de `SPECIFICATION.md`, `annexe-a-entites.md`, `annexe-b-rate-limit.md`,
vérifiée contre `pronotepy` 2.15.6. Les références `fichier:ligne` ont été lues,
pas supposées. Aucun appel réseau n'a été émis.

Verdict global : l'objectif du §1 est le bon, et la hiérarchie « entité primitive
> `event` > `device_trigger` » est la meilleure décision du document. Les six
décisions tiennent toutes **sauf la n° 2**, mais cinq ont des trous d'exécution
qui ne sont pas des détails : deux peuvent publier les données du mauvais enfant,
une peut boucler sur des authentifications, une rend inopérant l'évènement le
plus important du projet.

---

## 1. Sérialisation des appels — prémisse juste, mise en œuvre incomplète

**La prémisse est confirmée.** `_Communication.post` chiffre le numéro de requête
(`no`), l'inscrit *aussi* dans le chemin de l'URL
(`/appelfonction/{a}/{h}/{no}`) et fait `self.request_number += 2`. Deux appels
concurrents désynchronisent effectivement le compteur. Le §3.2 a raison d'être
non négociable. Quatre choses manquent.

### 1.1 `pronotepy` ne pose aucun délai d'expiration HTTP

`grep -rn timeout pronotepy/*.py pronotepy/ent/*.py` → **zéro occurrence**. Le GET
d'amorçage (`_Communication.initialise`) et le POST (`_Communication.post`)
appellent `session.request(...)` sans `timeout=`.

Conséquence sur le cycle de vie HA que tu demandes de vérifier : un serveur qui
accepte la connexion et ne répond jamais **bloque définitivement l'unique thread
de l'exécuteur**. Alors :

- `executor.shutdown(wait=True)` dans `async_unload_entry` ne rend jamais la
  main → le rechargement d'entrée et l'arrêt de Home Assistant se figent ;
- l'`asyncio.Lock` reste pris → tous les paliers suivants sont en interblocage ;
- et le §5.4 rend la panne **invisible** : « obsolescence plutôt
  qu'indisponibilité » fait qu'un interblocage définitif ressemble, pendant six
  intervalles, à un réseau lent — sans une ligne d'erreur.

**Exigence à ajouter.** La passerelle injecte un délai d'expiration. `requests`
n'a pas de défaut au niveau session : il faut substituer à
`client.communication.session` une sous-classe de `requests.Session` dont
`request()` renseigne `timeout` par défaut (30 s connexion / 60 s lecture,
configurable). Et `async_unload_entry` n'appelle **jamais** `shutdown(wait=True)`
depuis la boucle : `shutdown(wait=False, cancel_futures=True)`, plus un
`asyncio.wait_for` borné sur l'appel en vol. Un thread fuité vaut mieux qu'un
Home Assistant figé.

### 1.2 Le verrou doit couvrir la sélection de l'enfant, pas seulement l'appel

`ParentClient.post` (`clients.py:1017`+) estampille
`Signature.membre = {"N": self._selected_child.id, "G": 4}`, et `set_child()`
**mute le client** en réécrivant
`parametres_utilisateur["dataSec"]["data"]["ressource"]`. Or `Client.lessons()`
relit ce même emplacement muable pour construire le corps de la requête
(`clients.py:627`).

Donc, sur un compte parent à deux enfants, un entrelacement
`set_child(A) · post() · set_child(B) · post()` au mauvais grain publie
**l'emploi du temps de B sous les entités de A**. Silencieusement. Le
`_call(self, fn, *args)` du §3.2 ne protège que si `fn` fait *lui-même* le
`set_child` : l'unité de travail atomique est `(enfant, palier)`, jamais
`palier`. Le §3.2 ne le dit pas, et le schéma du §3.1 — une passerelle par
compte — invite précisément à l'erreur.

**Exigence à ajouter.** L'unité atomique est `set_child` + appel d'onglet +
construction des DTO. Test de contrat : deux enfants, rafraîchissements
entrelacés, chaque DTO porte le bon `student_id`.

### 1.3 `pronotepy` se réauthentifie dans le dos du limiteur — et peut boucler

`ClientBase.post` (`clients.py:526`+) intercepte **toute** `PronoteAPIError`
(sauf `ExpiredObject`) et appelle `self.refresh()`, qui est une poignée de main
complète : `session.close()` + GET HTML + `FonctionParametres` +
`Identification` + `Authentification` + `ParametresUtilisateur`, soit
**1 GET + 4 POST**, puis rejoue l'appel initial. Un `call()` budgété peut donc
consommer **six requêtes réseau**. L'exigence de l'annexe B §6 — « le limiteur est
le seul chemin vers la passerelle » — est fausse telle qu'écrite.

Pire : `Erreur.G = 25` (« trop de demandes d'autorisation ») est une
`PronoteAPIError` ordinaire → elle **déclenche une authentification**, exactement
la classe de requête que G=25 sanctionne. L'annexe B §4 la met dans le repli
exponentiel ; à ce moment-là `pronotepy` s'est déjà reconnecté une fois.

Pire encore : `ParentClient.post` (`clients.py:1044`+) **duplique le gestionnaire
sans la garde `_refreshing`**. `ClientBase.post` protège la récursion
(`if self._refreshing: raise e`) ; `ParentClient.post` non. Un serveur qui répond
en erreur pendant le `_login()` du `refresh()` produit une **récursion non bornée
de connexions complètes**. Sur un compte parent — le cas de ce projet.

Et enfin : **`refresh()` perd silencieusement la sélection de l'enfant.** Il
appelle `_login()`, qui remet `parametres_utilisateur[...]["ressource"]` sur la
ressource du parent ; `ParentClient` ne reconstruit `children` /
`_selected_child` que dans `__init__`, jamais dans `refresh()`. Après un
rafraîchissement automatique, `post()` estampille encore le bon `membre`, mais
`lessons()` envoie **la ressource du parent** dans le corps.

**Exigence à ajouter, non négociable, et c'est cinq lignes.** La passerelle
emploie son propre `post()` qui ne se réauthentifie jamais :

```python
class _NoRefreshParentClient(pronotepy.ParentClient):
    def post(self, function_name, onglet=None, data=None):
        post_data: dict = {}
        if onglet:
            post_data["Signature"] = {
                "onglet": onglet,
                "membre": {"N": self._selected_child.id, "G": 4},
            }
        if data:
            post_data["data"] = data
        return self.communication.post(function_name, post_data)
```

La reconnexion redevient une décision du `SessionManager`, comptée par le
limiteur. Bénéfice de bord indispensable à la décision n° 2 : `Erreur.G = 10`
(session expirée) devient un évènement explicite et **mesurable**.

### 1.4 Deux fuites d'appel de plus

- `ClientBase.__init__` se termine par `self.logged_in = self._login()`
  (`clients.py:150`) : **le constructeur est une connexion réseau**. Il doit être
  exécuté dans l'exécuteur, sous verrou ; `async_setup_entry` ne construit jamais
  le client depuis la boucle.
- `ClientInfo._cache()` appelle `self._client.communication.post(...)`
  directement — court-circuitant `ClientBase.post` **et** la signature `membre`
  du parent ; le code le dit en commentaire. Tout accès à
  `ClientInfo.address / email / phone / ine_number` est un appel non budgété
  depuis n'importe où. C'est la frontière DTO du §3.1 qui l'empêche : cette
  raison précise mérite d'y être écrite, elle est plus forte que le « attribut
  paresseux » générique.

---

## 2. Pas de maintien de session — **c'est ici que je ne suis pas d'accord**

### 2.1 L'arithmétique est juste, le modèle ne l'est pas

`16 × 3600 / 110 = 523,6` ✔. `64 × 4 = 256` ✔.

Mais `_KeepAlive.alive()` (`pronoteAPI.py:356`) ne ping **que si la session est
inactive depuis ≥ 110 s**, et `last_ping` est remis à jour par *chaque* POST. Le
maintien ne coûte donc pas 523 appels : il coûte 523 moins les pings déplacés par
le trafic réel, soit **≈ 523 − 167 ≈ 356**. La comparaison réelle est
**356 contre 256** : un facteur 1,4, pas 2.

(Au passage : la docstring de `keep_alive()` dit « 5 minutes », le code dit
110 s. La spécification a suivi le code — c'est le bon choix.)

### 2.2 Une connexion ne coûte pas 4 appels

Elle coûte **1 GET + 4 POST**. Le GET d'amorçage sur la page HTML
(`_Communication.initialise`) n'est pas optionnel : c'est lui qui rapporte les
attributs de session `Start({...})`. Si le limiteur ne compte que les POST, il
sous-compte de 64 requêtes par jour et la marge « ×4,7 » du §5.3 porte sur le
mauvais dénominateur.

Et en mode `token` / `qr_code`, `Authentification` peut renvoyer
`actionsDoubleAuth` → `_do_2fa` poste `SecurisationCompteDoubleAuth`
**une ou deux fois de plus** (`clients.py:430`, `clients.py:455`). Une connexion
vaut donc 5 à 7 requêtes, pas 4.

Budget corrigé : `64 × 5 = 320` au lieu de 256, **total ≈ 487 et non 423**. On
reste sous les plafonds — mais le test de contrat de l'annexe B §8
(« entre 380 et 460 appels ») est **calibré sur un modèle faux** et échouera pour
la mauvaise raison.

### 2.3 Il n'existe pas de déconnexion

`grep -n "Deconnexion\|logout"` → rien. « Fermer la session » se réduit à
`communication.session.close()`, qui jette le pool TCP et le bocal à cookies
**côté client**. La session **côté serveur reste vivante jusqu'à son propre délai
d'inactivité**.

La conception ne crée donc pas 64 sessions successives : elle crée 64 sessions
**qui se chevauchent**. Trois coûts que la spécification ne chiffre pas :

- quelle que soit la politique de sessions concurrentes par compte chez PRONOTE,
  c'est la conception qui maximise l'exposition. Inconnue, et non mesurée ;
- `derniereConnexion` est renvoyé à chaque `Authentification`
  (`clients.py:379`+) et PRONOTE présente le journal de connexions à
  l'établissement. **64 connexions par jour, sept jours sur sept, depuis une
  seule adresse IP** est exactement la signature que la spécification cherche à
  ne pas dessiner ;
- et surtout : **`Erreur.G = 25` signifie littéralement « dépassement du nombre
  maximal de demandes d'autorisation »**. L'annexe B §1 la nomme correctement,
  puis budgète contre les *appels*. Si le compteur qu'elle nomme est bien le
  compteur d'*autorisations*, alors la variable de risque est **le nombre de
  connexions par jour**, et cette conception la multiplie par 64 pour économiser
  la variable qui n'est probablement pas mesurée. Cela inverse tout l'arbitrage.
  C'est la sanction que tu demandais d'identifier, et c'est ta propre annexe qui
  la nomme.

### 2.4 Deux coûts qui écrasent la différence de 100 appels

**La rotation du jeton.** En mode `token` / `qr_code`, `Authentification` renvoie
`jetonConnexionAppliMobile` et `pronotepy` écrase `self.password` avec
(`clients.py:376`+). Le §7.2 exige à juste titre de le réenregistrer après chaque
connexion. Cela fait **64 `async_update_entry` par jour**, donc 64 réécritures de
`.storage/core.config_entries`, et surtout **64 fenêtres par jour** pendant
lesquelles un arrêt brutal de Home Assistant entre la rotation côté serveur et la
persistance locale laisse **un jeton mort et une intégration verrouillée** —
récupérable seulement en rescannant un QR code. Une connexion par jour : une
fenêtre par jour.

**La double authentification.** Le §8.1 exige que le PIN ne soit « jamais
conservé après usage ». Mais `_do_2fa` lève
`MFAError("PIN is required for this account")` dès que PRONOTE renvoie
`actionsDoubleAuth` sans PIN disponible (`clients.py:427`+), et c'est PRONOTE qui
décide quand redemander — typiquement sur une nouvelle session ou un nouvel
appareil. 64 connexions par jour, c'est **64 occasions par jour** d'être
interrogé sans PIN sous la main : l'intégration échoue durement à 3 h du matin et
ne peut pas se rétablir sans intervention. **Le §8.1 et le §6.5 se
contredisent**, et la spécification ne le remarque pas.

### 2.5 Contre-proposition : la reconnexion paresseuse domine les deux options

Ne choisis pas entre « ping toutes les 110 s » et « reconnexion à chaque lot » :
les deux sont des paris sur un paramètre que personne n'a mesuré.

1. **Garder une session et se reconnecter paresseusement sur `Erreur.G = 10`**
   (session expirée par inactivité). Coût zéro tant que la session vit, coût
   d'une connexion quand elle meurt. Avec `timetable` à 15 min, si le délai
   d'inactivité de l'établissement est ≥ 15 min (le réglage courant est 30 min),
   le trafic réel maintient la session et le nombre de connexions tombe de 64 à
   **1 à 3 par jour**. Budget total ≈ 167 + 15 ≈ **180 appels/jour**, moins de la
   moitié du plan actuel, avec une rotation de jeton par jour et une exposition
   2FA quasi nulle.
2. Cela **exige** le §1.3 (désactiver le rafraîchissement automatique de
   `pronotepy`) — dont tu as besoin de toute façon.
3. **Mesurer au lieu de supposer** : le `SessionManager` enregistre l'intervalle
   observé entre le dernier appel réussi et le premier `G = 10`, et l'expose en
   `sensor.<compte>_duree_vie_session`. Si le délai mesuré s'avère inférieur à
   l'intervalle de `timetable`, la stratégie paresseuse **dégénère exactement en
   la conception actuelle** (une connexion par lot). Elle n'est donc jamais pire.
   C'est tout l'argument : **la reconnexion paresseuse domine les deux options de
   la spécification, pour toute valeur du paramètre inconnu.**
4. Garder `max_logins_per_day` comme garde-fou, mais ramener le défaut de 120 à
   **24**. Une conception qui attend 2 connexions par jour doit alarmer à 24, pas
   à 120 : un plafond à 120 ne peut pas attraper le défaut pour lequel il existe.

---

## 3. Contournement des propriétés — bonne décision, mauvaise route

### 3.1 Les décomptes sont exacts

- `DernieresNotes` 198 : `dataClasses.py:525`, `534`, `548`, `578` →
  `grades`, `averages`, `overall_average`, `class_overall_average`. Quatre POST
  identiques, même `json_data`. ✔
- `PagePresence` 19 : `dataClasses.py:606`, `621`, `636` → `absences`, `delays`,
  `punishments`, et **les trois lisent la même liste `listeAbsences`** en filtrant
  sur `a["G"]` (13 / 14 / 41). ✔

Donc oui, fais-le. Mais le §3.3 surestime le coût et prend la mauvaise route.

### 3.2 Tu n'as pas à réimplémenter le décodage

`Grade(json)`, `Average(json)`, `Absence(json)`, `Delay(json)`,
`Evaluation(json)`, `Report(data)` prennent **le seul dictionnaire brut** — aucun
client. `Punishment(client, json)` et `Lesson(client, json)` prennent un client et
rien d'autre.

La bonne forme est donc : un `post()` brut par onglet, puis **les listes brutes
passées aux classes de données de `pronotepy`**, puis correspondance vers tes DTO
gelés. Tu obtiens la déduplication *et* tu conserves le décodage amont et ses
correctifs futurs. L'exemple du §3.3 — `_grade()`, `_average()`, `_grade_value()`
écrits à la main — n'achète rien et **reprend précisément la dette que le §3.4
déclare refuser**. Les deux sections se contredisent.

### 3.3 Une exception, et c'est un piège qu'il faut connaître

`Grade.__init__` résout `self.period` par
`Util.get(Period.instances, id=p)[0]` (`dataClasses.py:711`), où
`Period.instances` est un **`set` de classe jamais vidé** (`dataClasses.py:489`,
`494`). Trois conséquences :

- **C'est une fuite.** Chaque `Period` jamais construite y reste, et chacune
  retient `_client` → un `Client` mort → une `requests.Session` morte. Ta
  conception à 64 reconnexions par jour ajoute ~3 `Period` par reconnexion :
  **~190 par jour, ~35 000 sur une année scolaire**, chacune épinglant tout un
  graphe d'objets morts. Le §3.1 nomme ce mode de défaillance (« une entité qui
  garde une référence au client garde vivant tout le graphe ») et la frontière
  DTO **ne le corrige pas**, parce que le `set` est peuplé par `pronotepy` à la
  connexion, pas par tes entités.
- **C'est un canal entre entrées de configuration.** `Period.instances` est
  global au processus. L'affirmation du §3.2 — « deux entrées de configuration
  distinctes n'interfèrent pas » — est **fausse**. Deux comptes, deux exécuteurs,
  deux verrous, **un attribut de classe muable écrit en concurrence**. Et
  `Util.get(...)[0]` ne compare que `id` : la note du compte A peut résoudre la
  période du compte B. Les `N` étant propres à l'établissement, la collision est
  probable, pas exotique.
- **C'est porteur.** Vider le `set` pour corriger la fuite fait renvoyer `[]` par
  `Util.get(...)` → `[0]` → `IndexError` → enveloppé en `ParsingError` → **tout le
  lot de notes échoue**.

Donc pour `Grade` en particulier, le décodage maison **est** justifié — non pour
économiser un appel, mais pour éviter un registre global qu'on ne peut ni garder
ni vider. Écris cette raison dans le §3.3 ; c'est « réimplémenter le décodage »
comme politique générale qui est faux.

Seconde raison, indépendante : `Util.grade_parse` transforme `|1`–`|8` en
`"Absent"`, `"Dispense"`, … donc `pronotepy.Grade.grade` est **déjà lossy**.
Décoder `note.V` toi-même est la seule voie vers la sentinelle brute dont
l'énumération `GradeStatus` du §4.3 a besoin.

Et un durcissement : `grade_translate` a huit entrées, indexées par
`int(string[1]) - 1`. Un hypothétique `|9` est un `IndexError` — **une seule
nouvelle sentinelle de PRONOTE casse toutes les notes**. L'exigence de décodage
non strict du §3.3 doit couvrir ce cas nommément : sentinelle inconnue →
`GradeStatus.UNKNOWN`, jamais une exception.

### 3.4 Deux identifiants d'onglet du §5.2 sont faux

- `news` est **`PageActualites`** onglet 8 (`clients.py:856`), pas
  `SaisieActualites`. `SaisieActualites` 8 est **l'écriture**
  (`Information.mark_as_read`, `dataClasses.py:1148`).
- `discussions` est **`ListeMessagerie`** onglet 131 (`clients.py:828`).

Ces tables seront recopiées dans `gateway.py` et dans les tests de contrat
d'appels par palier. À corriger avant, pas après.

---

## 4. Treize paliers — surdimensionné, et une fusion est gratuite

### 4.1 `timetable` et `timetable_week` sont le même appel

`Client.lessons()` (`clients.py:613`+) boucle
`for week in range(first_week, last_week + 1)` et poste `PageEmploiDuTemps`
**une fois par semaine**, puis filtre côté client. Chercher « aujourd'hui et
demain » coûte donc **exactement le même appel** que chercher la semaine entière.

Fusionne-les : un seul palier `timetable`, 15 min, qui charge la **semaine
courante** (plus l'appel de la semaine suivante uniquement quand demain franchit
la limite — la même moyenne de 1,14 que tu as déjà budgétée). Tu perds un palier,
tu gagnes 3 appels par jour, et la vue semaine comme `binary_sensor.vacances`
passent de 6 heures de retard à 15 minutes. Il n'y a aucun argument pour la
scission : elle existe parce que la spécification raisonne en *jours* là où le
protocole facture en *semaines*.

Même remarque pour `menus` (`clients.py:908`, boucle hebdomadaire) : aujourd'hui
+ demain vaut 1 appel sauf au franchissement — l'annexe B dit 1, elle devrait dire
1,14.

### 4.2 `reports` et `history` se recouvrent

`history` (24 h, périodes closes, onglets 198/19) et `reports` (24 h,
`PageBulletins` 13, un POST par période, `dataClasses.py:518`) parcourent tous
deux la liste des périodes une fois par jour. Et le bulletin d'une période close
est aussi constant que ses notes — le §5.2 le plaide déjà pour `history`. Fusionne
en un `history` qui parcourt chaque période close une fois en émettant
198 + 19 + 13, et laisse le bulletin de la période **courante** voyager avec
`marks`.

### 4.3 `static` est surbudgété et détient ce qu'il ne devrait pas

De ce que le §5.2 range sous `static` : `ClientInfo.class_name`,
`.establishment`, `.name` et `ClientBase.periods` sont tous lus depuis
`func_options` / `parametres_utilisateur`, **déjà en main après la connexion —
zéro appel**. `get_teaching_staff()` vaut 1 (`PageEquipePedagogique` 37,
`clients.py:800`). `export_ical()` vaut 1 (`PageInfosPerso` 16,
`clients.py:670`). L'annexe B dit 4 ; c'est **2**. Et l'URL iCal n'a rien à faire
dans ce palier — voir §6.

### 4.4 Ce qui manque n'est pas un palier, c'est un déclencheur horaire

L'annexe A §3 exige à juste titre `async_track_point_in_time` pour
`binary_sensor.en_cours` et `_absence_en_cours`. Mais `sensor.prochain_cours`,
`_fin_des_cours` et `_prochain_reveil` ont exactement la même propriété : ils
doivent basculer **à un instant d'horloge**, pas quand `timetable` repasse. Tels
que spécifiés, ils accusent jusqu'à 15 minutes de retard — sur un capteur de
réveil, c'est tout l'objet du capteur. À ajouter à la liste.

**Verdict n° 4 :** 13 → **10 paliers**. Et la réduction vient de lire
correctement l'unité de facturation du protocole, pas de rogner l'ambition.

---

## 5. `event` + `device_trigger` — la meilleure décision, avec un défaut au centre

### 5.1 « Delta sur `N`, jamais sur le contenu » ne peut pas implémenter `cours_modifie`

`Lesson.num` est le champ `P`, et sa propre docstring dit : *« For the same lesson
time, the biggest num is the one shown on pronote »*. Donc `PageEmploiDuTemps`
renvoie **plusieurs entrées pour un même créneau** — l'originale et ses
remplacements — et `Client.lessons()` **ne filtre pas**. Par conséquent :

- un changement de salle **conserve le même `N`** → aucun delta d'identifiant →
  **aucun évènement**. `room_changed` et `teacher_changed`, tels que spécifiés,
  ne se déclencheront jamais ;
- un remplacement arrive avec un **nouveau `N`** tandis que l'ancien est toujours
  présent → ton détecteur signale un *ajout*, pas une *modification*, et
  `previous_start` / `previous_classroom` n'ont aucune source ;
- côté affichage, c'est un défaut de correction : `sensor.cours_du_jour`
  (état = `len(lessons)`) **surcompte** tous les jours comportant une
  modification, `calendar.emploi_du_temps` affiche des **évènements dupliqués et
  superposés**, et `sensor.prochain_cours` comme `binary_sensor.en_cours` peuvent
  s'accrocher à une entrée périmée.

**Contre-proposition : deux règles, pas une.**

- **Collections en ajout seul** (notes, devoirs, actualités, absences, retards,
  punitions, messages, évaluations) → delta sur `N`. Garde la règle telle
  qu'écrite, elle y est juste, et ces classes portent toutes `N`.
- **L'emploi du temps** → d'abord **dédoublonner par créneau en gardant le `num`
  maximal** (correction dont les entités d'affichage ont besoin de toute façon,
  indépendamment des évènements), puis comparer un **tuple de champs sur liste
  blanche** par clé de créneau : `(canceled, status, classroom, teachers, start,
  end)`. `memo`, `background_color` et le contenu sont exclus, donc un libellé
  corrigé ne déclenche toujours pas — ce que la règle du §2.2 cherchait
  réellement à acheter. Clé de créneau : `(date, place, subject_id)` depuis le
  JSON brut, pas `N`.

Reformule l'exigence du §2.2 en : « delta sur l'identité là où l'identité est
stable, sur un sous-ensemble de champs en liste blanche là où le protocole
supersède au lieu d'ajouter ». Et inscris la règle de dédoublonnage par `num`
dans le DTO `Lesson` du §4.1, qui porte aujourd'hui `num` sans dire à quoi il
sert.

### 5.2 Le catalogue est surdimensionné d'une manière précise : les jumeaux

`sensor.devoirs_a_faire` (décompte) **et** `binary_sensor.devoirs_a_faire`
(décompte > 0) ; idem pour `actualites_non_lues` et `messages_non_lus`. Le binaire
de chaque paire est un `numeric_state above: 0` sur le capteur — une ligne de
YAML, sans template, exactement le niveau que fixe le §1. Trois entités par enfant
qui ne gagnent rien.

Garde les paires **seulement quand le binaire n'est pas un seuil sur un nombre
déjà exposé** : `cours_annules`, `devoirs_en_retard`, `jour_de_classe`,
`en_cours`, `absence_en_cours`, `punition_a_venir`, `controle_prevu`,
`sortie_pedagogique`, `vacances` — ceux-là encodent un prédicat qu'il faudrait
sinon écrire en template. **C'est le bon test à appliquer à tout le catalogue, et
c'est le test qui manque au §2.1.**

À l'inverse, **ce qu'un parent automatise vraiment est absent** :

- `sensor.<é>_prochain_controle` (horodatage). `binary_sensor.controle_prevu` ne
  répond qu'à « aujourd'hui » ; donc « réviser la veille au soir » exige un
  template sur `attributes.lessons` — l'échec exact que le §1 se donne pour
  critère.
- `sensor.<é>_prochaine_punition` (horodatage de la prochaine retenue : il faut y
  amener l'enfant).

Ces deux capteurs valent plus que les trois jumeaux ci-dessus.

### 5.3 Corrections factuelles à l'annexe A

- `Absence` porte **`hours` (str) et `days` (int)** — **pas de `minutes`**. C'est
  `Delay` qui porte `minutes`. L'annexe A §4 liste `minutes` pour `absence_added`
  *et* `delay_added`.
- `Discussion.unread` est un **`int`** (`nbNonLus`, `dataClasses.py:1353`), pas un
  booléen. `sensor.messages_non_lus` doit valoir `sum(d.unread)`, pas
  `len([d for d in … if d.unread])`. Tranche, sinon le capteur et le binaire
  associé se contrediront.
- `Average` **n'a pas d'`id`**. Sans conséquence pour les évènements, mais la
  règle de stabilité du §2.4 exige pour les éléments de `sensor.moyennes` une clé
  qui ne soit pas un rang : utilise `Subject.id`.
- `Client.current_period` (`clients.py:918`+) lit `listeOngletsPourPeriodes` et
  **retombe sur `onglets[0]`** si l'onglet 198 est absent. Donc
  `sensor.periode_en_cours` peut silencieusement désigner la mauvaise période dans
  un établissement qui ne publie pas les notes. Préfère un chemin `None` à une
  retombée.

---

## 6. L'URL iCal en service à réponse — d'accord, et c'est la bonne forme

`SupportsResponse.ONLY` est correct pour HA 2026.9. L'alternative que HA offre —
une entité `text` ou une entité `EntityCategory.CONFIG` — remet la valeur dans la
machine à états, donc dans l'enregistreur, dans les sauvegardes, dans
`/api/states`, dans toute capture des outils de développement et dans
`hass.states.async_all()` pour n'importe quel module complémentaire porteur d'un
jeton. Une réponse de service ne va qu'à un appelant et n'est jamais persistée.

Une seule réserve réelle, et elle ne remet pas la décision en cause : **la réponse
est écrite dans la trace** si l'appel a lieu dans une automatisation ou un script,
et les traces sont stockées (`.storage/trace.saved_traces`) et visibles dans
l'interface. Cela plaide pour `SupportsResponse.ONLY` **plus** une note de
documentation disant que le service est à usage interactif — pas contre.

**Mais le §5.2 contredit le §8.2.** Le palier `static` est spécifié comme
collectant « identité, classe, équipe pédagogique, **URL iCal** », et l'annexe B
§5.2 lui budgète 4 appels. Si l'URL ne doit exister dans aucun état (§8.2) et dans
aucun diagnostic (§8.4), alors la collecter toutes les 24 h dans le
`SnapshotStore` est (a) un appel gaspillé et (b) le placement du porteur
d'authentification dans une structure mémoire longue durée **dont la fonction est
d'être vidée dans un rapport** — le danger exact que le §8.2 existe pour prévenir,
déplacé de la machine à états vers le magasin d'instantanés.

**Retire l'iCal de `static`.** `get_ical_url` appelle `export_ical()` à la demande
(1 appel budgété, `PageInfosPerso` 16, `clients.py:670`) et renvoie sans stocker.
Idem pour `get_identity` : `ClientInfo._cache()` court-circuite `ClientBase.post`,
donc ce doit être une fonction de la passerelle, pas une lecture de propriété.

---

## 7. Ce que tu n'as pas soumis — dont deux points plus graves que les six

### F1. `_parse_html` décide « IP suspendue » avec `if "IP" in html`

Deux majuscules, n'importe où dans la page, et `pronotepy` lève
`PronoteAPIError("Your IP address is suspended.")`. N'importe quel établissement
dont la page contient « IP » dans un titre, un menu, une classe CSS, une bannière
ENT, `SKIP`, `ZIP`, `EQUIPE`… déclenche la détection.

Or le §6.3 et l'annexe B §3 construisent **une attente d'une heure et une
réparation dédiée « au texte explicite »** sur cette chaîne. Tu diras à des
parents que leur adresse IP est bannie parce qu'un collège a écrit « Espace IP »
dans un pied de page — et le §6.3 interdit tout appel pendant l'attente, donc
l'intégration s'arrête vraiment.

**Correctif.** Traiter comme « amorçage échoué, cause indéterminée » : une attente
générique, une réparation dont le texte dit que la connexion n'a pas pu être
établie et énumère les deux causes possibles. **Ne jamais affirmer la suspension
d'IP depuis ce signal.** Si tu veux un vrai test, c'est l'absence du bloc
d'attributs `Start({...})` — et même là, ne nomme pas la cause.

### F2. `max_failed_logins_per_hour = 3` compte la mauvaise chose

Un mot de passe faux **ne lève pas** de `PronoteAPIError` dans `pronotepy` : le
déchiffrement du défi échoue et `_login` lève **`CryptoError`**
(`clients.py:333`+), ou bien `_login` renvoie `False` quand `"cle"` est absent de
la réponse d'`Authentification`.

Donc la classification d'échec de l'annexe B §3 doit s'accrocher à `CryptoError`
et à `logged_in is False`, **pas** à une erreur HTTP ou protocolaire. Tel que
spécifié, le compteur qui « protège l'adresse IP » risque de ne jamais
s'incrémenter. C'est la chose la plus importante à ne pas rater dans
`ratelimit.py`, et la spécification ne nomme pas l'exception.

### F3. L'enrôlement par QR code coûte deux connexions, pas une

`qrcode_login` construit un client (connexion n° 1), poste `PageInfosPerso` 49,
puis appelle `token_login(**client.export_credentials())` → **une seconde
connexion complète** (`clients.py:196`+). Le §7.2 — « La validation n'effectue
**qu'une** connexion » — est faux sur le chemin QR, qui est celui que le QR code
rend prioritaire. À budgéter, et à exclure du compteur d'échecs pour que le
doublement volontaire ne le déclenche pas.

### F4. `Lesson.end` est parfois calculé, avec un commentaire d'aveu

Quand `DateDuCoursFin` est absent, `Lesson.__init__` calcule la fin depuis
`client.func_options[...]["ListeHeuresFin"]` via `Util.place2time`, dont le
commentaire dit *« might be wrong... works with demo »*. Or **tout** — réveil,
`en_cours`, agenda, `fin_des_cours` — dépend de `end`.

La passerelle doit donc porter `place` et `duree` bruts **et** un drapeau disant si
`end` était fourni ou inféré, et `test_gateway.py` a besoin d'une fixture pour le
chemin inféré. Sinon une configuration d'établissement rend toutes les entités
d'emploi du temps subtilement fausses, sans une erreur nulle part.

### F5. `homework_horizon` n'est pas un levier de budget

`Client.homework()` (`clients.py:710`) construit le domaine comme une **plage de
semaines** `[w1..w2]` et fait retomber `date_to` sur la fin de l'année scolaire
(`General.DerniereDate`). **Un appel, quelle que soit l'étendue.** L'horizon est un
filtre côté client.

Donc : charge l'année entière en un appel, filtre dans la couche DTO, et
`homework_horizon` devient une option de présentation sans conséquence
budgétaire — alors que le §7.3 la présente comme un réglage de coût. Bénéfice
gratuit : `calendar.<é>_devoirs` et `binary_sensor.devoirs_en_retard` s'améliorent
tous les deux.

### F6. `mypy --strict` va buter sur l'amont

`pronotepy` livre bien `py.typed`, mais `dataClasses.py` importe `autoslot.Slots`
avec un `# type: ignore` et porte plusieurs `# type: ignore` de signature. Prévois
un `[[tool.mypy.overrides]] module = "pronotepy.*"` avec
`follow_imports = "skip"`, sinon la première exécution de CI se battra contre les
annotations d'amont et non contre les tiennes. À écrire dans le §11.

### F7. 100 % sur `gateway.py` demande des fixtures par groupe de champs optionnels

Le §11 exige 100 % sur `gateway.py` alors que le §3.4 en fait le **seul** module
qui touche le réseau. Ce n'est atteignable qu'avec des fixtures couvrant chaque
branche, **y compris toutes les branches `strict=False` de champs absents** —
c'est-à-dire l'exigence de décodage non strict du §3.3. Dis explicitement qu'il
faut une fixture **par groupe de champs optionnels**, pas une par onglet, sinon le
100 % sera dispensé au jalon M5.

---

## 8. Récapitulatif des modifications demandées

| # | Décision | Verdict | Modification |
| --- | --- | --- | --- |
| 1 | Exécuteur mono-thread + verrou | **Tient, incomplet** | délai HTTP injecté ; `shutdown(wait=False)` ; unité atomique = `(enfant, palier)` ; `post()` sans réauthentification |
| 2 | Pas de maintien de session | **Ne tient pas** | reconnexion **paresseuse** sur `G = 10`, durée de vie **mesurée** ; connexion = 1 GET + 4–6 POST ; `max_logins_per_day` 120 → 24 |
| 3 | `client.post()` direct | **Tient** | réutiliser les classes de données amont ; décodage maison **pour `Grade` seulement**, motif = `Period.instances` ; corriger `PageActualites` / `ListeMessagerie` |
| 4 | Treize paliers | **Surdimensionné** | 13 → 10 : fusionner `timetable_week`→`timetable`, `reports`→`history`, sortir l'iCal de `static` ; réveil / prochain cours sur point d'horloge |
| 5 | `event` + `device_trigger` | **Tient, défaut central** | deux règles de delta ; dédoublonnage par `num` ; retirer 3 jumeaux ; ajouter `prochain_controle` et `prochaine_punition` |
| 6 | iCal en service à réponse | **Tient** | retirer l'iCal du palier `static` ; note de documentation sur les traces |

**Budget après modifications** : ≈ 180 appels/jour au lieu de ≈ 423 annoncés
(≈ 487 réels), pour une fraîcheur **supérieure** sur l'emploi du temps de la
semaine. Le test de contrat de l'annexe B §8 doit être recalibré ; sa borne est
une bonne idée, sa valeur est calculée sur un modèle faux.

---

## 9. Les deux limites de la revue, respectées

Aucun identifiant, mot de passe, PIN ni URL iCal réel n'apparaît dans ce document.
Le journaliseur `pronotepy` n'a pas été activé : la revue est une lecture de la
source, aucun appel réseau n'a été émis. Le §8.1 a raison sur `pronoteAPI.py` —
`_Communication.post` journalise en `DEBUG` la charge utile complète et, avant
chiffrement, l'hexadécimal en clair. L'exigence d'absence de clé `loggers` dans
`manifest.json` est correcte et doit être **testée**, pas seulement écrite.
