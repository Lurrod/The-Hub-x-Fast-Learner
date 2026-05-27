# Audit Python - 2026-05-27

> Mode `READ_ONLY` : audit + rapport, **aucune modification de code appliquée**.
> Cible : tout le repo (`bot.py`, `cogs/`, `services/`, `scripts/`). Exclu : `tests/`.
> Stack : bot Discord (discord.py 2.5, pymongo 4.6, pillow, requests) - Python 3.11/3.12 (CI), 3.13 en local.

## Synthèse

- **État général** : codebase de très haute qualité pour un bot Discord. Architecture propre (repository centralisé, CAS atomiques Mongo partout, concurrence asyncio maîtrisée, client réseau avec retry/timeout/circuit-breaker). Outillage lint/format/sécurité quasi parfait. Le principal problème est un **bug fonctionnel silencieux** sur `/match-cleanup` masqué par un test, plus de la dette de typage et une couverture sous le seuil interne.
- **Score : 8.5 / 10**
- **Top 3 actions prioritaires** :
  1. Corriger `/match-cleanup` : il compare un `_id` ObjectId avec une `str` → ne trouve jamais un vrai match (MAJEUR).
  2. Remonter la couverture à ≥ 80 % (actuel 76 %), en priorité `cogs/admin.py` 50 %, `cogs/match/_cog.py` 60 %, `cogs/elo_admin.py` 61 %.
  3. Réduire la dette mypy `--strict` (303 erreurs, la config en annonçait ~180) et refactorer les 2 hotspots de complexité (`on_queue_full` E=32, `match_replace` D=24).

## Métriques

| Outil | Résultat |
|-------|----------|
| ruff check | ✅ **0 erreur** (`select = E,F,W,B,C90,SIM,N,A,UP,RET,RUF`) |
| ruff format --check | ✅ **63 fichiers déjà formatés** |
| mypy (config projet, laxiste) | ⚠️ **1 erreur** (`applications.py:390`) |
| mypy `--strict` | ⚠️ **303 erreurs / 21 fichiers** (dette assumée, mais en hausse vs ~180 annoncés) |
| bandit | ✅ **0 HIGH, 0 MEDIUM** ; 7 LOW (5× B311 pseudo-random non-crypto, 2× B101 assert) |
| pip-audit (requirements.txt) | ✅ **0 CVE connue** (29 paquets) |
| radon CC | ⚠️ 1× E (32), 1× D (24), ~22 fonctions en C |
| radon MI | ⚠️ 1 fichier rang B : `cogs/match/_cog.py` (10.27) ; reste A |
| vulture (conf. 80) | ✅ **0 code mort** |
| pytest | ✅ **454 passed** en 89 s |
| coverage | ⚠️ **76 %** (seuil interne projet : 80 %) |

---

## Findings

### [MAJEUR] `/match-cleanup` ne trouve jamais un vrai match (ObjectId vs str)
- **Localisation** : `cogs/match/_cog.py:1406` (et `:1428`)
- **Catégorie** : Correction / Testabilité
- **Problème** : `match_cleanup(... match_id: str)` exécute `self.db["matches"].find_one({"_id": match_id})` avec la chaîne saisie par l'admin. Or `repository.create_match` (`services/repository.py:462`) fait `insert_one(doc)` **sans champ `_id`** → MongoDB génère un **ObjectId**. `FONCTIONNALITES.txt:441` documente d'ailleurs `_id: ObjectId`. pymongo ne convertit pas une hex-string en ObjectId : `{"_id": "abc..."} != {"_id": ObjectId("abc...")}`. La commande renvoie donc toujours « Match introuvable » en production.
- **Impact** : l'outil admin de récupération des matchs disputés/bloqués est **inopérant**. Mitigation partielle par le filet auto `expire_stale_contested` et `/match-cancel` (basé sur `channel_id`), mais la commande dédiée échoue silencieusement.
- **Cause racine (test trompeur)** : `tests/test_match_cleanup_command.py` insère des matchs avec `_id` **string** (`"m1"`, `"old"`, `"unknown"`), ce qui fait passer le test « happy path » alors que la prod utilise des ObjectId. Le test donne une fausse confiance.
- **Correctif minimal** :
  ```python
  from bson import ObjectId
  from bson.errors import InvalidId
  ...
  try:
      oid = ObjectId(match_id)
  except InvalidId:
      await interaction.response.send_message(f"ID invalide : `{match_id}`.", ephemeral=True)
      return
  match = self.db["matches"].find_one({"_id": oid})
  # ... et utiliser `oid` pour mark_match_cleanup_started / update_one
  ```
  Compléter le test avec un `_id` ObjectId réel (et idéalement vérifier comment l'admin obtient cet ID - il n'est pas exposé dans les embeds, qui n'affichent que `match_number`).
- **Référence** : pymongo ObjectId, CWE-697 (Incorrect Comparison)

---

### [MINEUR] Couverture de tests à 76 % (< seuil interne de 80 %)
- **Localisation** : global ; trous principaux `cogs/admin.py` 50 %, `cogs/match/_cog.py` 60 %, `cogs/elo_admin.py` 61 %, `cogs/applications.py` 65 %, `services/leaderboard_refresh.py` 65 %, `bot.py` 65 %
- **Catégorie** : Testabilité
- **Problème** : la règle projet (`rules/common/testing.md`) impose 80 % minimum ; la suite est à 76 %. Les chemins non couverts incluent des branches d'erreur Discord et le flow `on_queue_full`.
- **Impact** : régressions possibles non détectées sur les cogs admin et la formation de match - précisément là où le bug ci-dessus s'est glissé.
- **Correctif** : ajouter des tests sur `cogs/admin.py` (setup_bot, clear) et les branches d'échec de `match/_cog.py`. Optionnel : `--cov-fail-under=80` dans la CI une fois le seuil atteint.
- **Référence** : `rules/common/testing.md`

### [MINEUR] Dette de typage mypy `--strict` en hausse (303 erreurs)
- **Localisation** : 21 fichiers ; ex. `cogs/match/_cog.py:1523` (param non annoté), `applications.py:390` (index `Member | User` sur un dict typé)
- **Catégorie** : Typage & contrats
- **Problème** : `pyproject.toml` désactive `--strict` et annonce « ~180 erreurs à fix » ; le compte réel est de **303**. La dette grossit au lieu de se résorber. L'unique erreur en mode laxiste (`applications.py:390`) est réelle : `interaction.user` peut être `User` (hors-guild) là où `add_roles`/overwrites attendent un `Member`.
- **Impact** : la garde de type ne progresse pas ; risque d'`AttributeError` runtime sur les chemins non guild-only.
- **Correctif** : durcir incrémentalement (activer `check_untyped_defs` module par module), commencer par `services/` (déjà presque typé) puis retirer les overrides `union-attr/arg-type` au fur et à mesure.
- **Référence** : PEP 484

### [MINEUR] `add_match_vote` interpole `user_id` dans le nom de champ Mongo sans coercion
- **Localisation** : `services/repository.py:560` - `{"$set": {f"votes.{user_id}": choice}}`
- **Catégorie** : Sécurité (NoSQL) / Cohérence
- **Problème** : contrairement au reste du module qui passe systématiquement par `_to_int_id(...)`, `user_id` est injecté brut dans un chemin de champ. Un `user_id` contenant `.` ou `$` modifierait la structure du document (`votes.$x`...). En pratique `user_id` vient de `interaction.user.id` (snowflake int), donc non exploitable aujourd'hui, mais la signature accepte `int | str` et l'invariant n'est pas garanti localement.
- **Impact** : faible (entrée contrôlée), mais incohérent avec la discipline de coercion centralisée du fichier.
- **Correctif** : `uid = _to_int_id(user_id, field="user_id")` puis `f"votes.{uid}"`.
- **Référence** : CWE-943 (Improper Neutralization of Data within Query Logic)

### [SUGGESTION] Gating par **nom** de rôle (renommage/usurpation possible)
- **Localisation** : `cogs/queue_v2.py:67-71` (`QUEUE_ROLE_GATES`), `:416/435` (`r.name == PRO_QUALIFICATION_ROLE`) ; `cogs/match/_constants.py:38` (`ADMIN_ROLE_NAMES = ("Admin","Match Staff","Administrateur")`, comparé par nom pour les pouvoirs draft-cancel)
- **Catégorie** : Sécurité (contrôle d'accès)
- **Problème** : les gates de queue et les privilèges admin *de match* reposent sur l'égalité de nom de rôle. Quiconque peut créer/renommer un rôle avec le bon libellé contourne le gate. (À noter : l'autorisation des **slash commands** admin, elle, repose sur la permission `manage_guild` ou un rôle bypass par **ID** - c'est sain.)
- **Impact** : faible en pratique (créer un rôle exige déjà `Manage Roles`), mais fragile : un simple renommage de rôle casse les gates ou en ouvre.
- **Correctif** : configurer ces rôles par **ID** (collection de config par guild, comme `bypass`) plutôt que par nom.
- **Référence** : CWE-284 (Improper Access Control)

### [SUGGESTION] 2 `assert` en code de production (strippés sous `python -O`)
- **Localisation** : `services/repository.py:1006`, `services/team_balancer.py:95`
- **Catégorie** : Robustesse
- **Problème** : `bandit` B101. Les deux sont des gardes d'invariant légitimes (documentées), mais disparaissent si le bot tourne avec `-O`/`PYTHONOPTIMIZE`.
- **Impact** : très faible (PM2 ne lance pas `-O` par défaut), mais l'invariant deviendrait silencieux.
- **Correctif** : remplacer par un `if ... : raise RuntimeError(...)` si l'on veut la garantie en prod optimisée.
- **Référence** : Bandit B101

### [SUGGESTION] Hotspots de complexité à refactorer
- **Localisation** : `cogs/match/_cog.py:110` `on_queue_full` (CC **E=32**), `:1253` `match_replace` (CC **D=24**) ; ~22 fonctions en C (`leaderboard_refresh.build_leaderboard_payload`/`refresh_leaderboard_channel` =19, `riot_api._parse_match`=17, `elo_updater.apply_match_validation`=17, `admin.setup_bot`=18, `match/_vote._vote`=19…)
- **Catégorie** : Lisibilité / Maintenabilité
- **Problème** : `on_queue_full` orchestre fetch N+1-batché, branche Pro draft, auto-balance, création catégorie, déplacements VC et envoi d'embeds dans une seule méthode. `_cog.py` est le seul fichier en MI rang B (10.27) et dépasse 800 lignes (1533) - au-delà du plafond `coding-style.md`.
- **Impact** : surface de bug élevée, tests difficiles (cf. couverture 60 %).
- **Correctif** : extraire les sous-étapes de `on_queue_full` (préparation joueurs / branche pro / branche balance / side-effects) en méthodes ou helpers déjà amorcés par le découpage `_constants`/`_embeds`/`_vote`.
- **Référence** : `rules/common/coding-style.md` (fonctions <50 lignes, fichiers <800)

### [SUGGESTION] `/win` et `/lose` : application ELO non atomique sur 5 joueurs
- **Localisation** : `cogs/elo_admin.py:183-197` et `:251-272`
- **Catégorie** : Robustesse (idempotence)
- **Problème** : la boucle applique `$inc`/`$set` joueur par joueur, sans transaction. Un crash entre le 2e et le 3e joueur laisse un résultat partiellement appliqué, non rejouable proprement (re-lancer la commande ré-incrémenterait les 2 premiers).
- **Impact** : faible (commande admin manuelle, rare), mais pas de garantie tout-ou-rien.
- **Correctif** : si la cohérence importe, envelopper dans une transaction Mongo (replica set requis) ; sinon documenter que la commande n'est pas rejouable.
- **Référence** : -

### [SUGGESTION] `requirements.txt` : bornes hautes manquantes (sauf discord.py)
- **Localisation** : `requirements.txt:2-5` (`pymongo>=4.6`, `pillow>=10.0`, `requests>=2.31`, `python-dotenv>=1.0`)
- **Catégorie** : Dépendances
- **Problème** : seules des bornes basses. Un `pip install` futur peut tirer un major cassant (ex. pymongo 5.x, pillow 11.x). Pas de lockfile (`requirements.lock`/`uv.lock`) pour reproductibilité.
- **Impact** : faible aujourd'hui (0 CVE), mais build non déterministe dans le temps.
- **Correctif** : ajouter des bornes hautes (`<5`, `<11`…) ou figer via un lockfile généré (`uv pip compile`).
- **Référence** : PEP 440

---

## Axes sains (vérifiés, aucun problème)

- **`services/repository.py`** : couche d'accès Mongo exemplaire - CAS atomiques (`find_one_and_update` avec filtre d'état) pour join queue, votes, transitions de match, claim ELO, décisions de candidature ; coercion d'IDs centralisée (`_to_int_id`) ; `datetime.now(UTC)` partout ; index créés idempotemment. Pas d'injection NoSQL exploitable (égalités, pas de `$where`/regex sur entrée brute).
- **`cogs/queue_v2.py`** : concurrence solide - `asyncio.Lock` **par guild** borné LRU (pas de fuite mémoire), `defer()` immédiat (anti-token 3 s / 10062), `asyncio.to_thread` pour tout pymongo bloquant, refs fortes sur les tasks de fond + `done_callback` de nettoyage, side-effects Discord hors-lock, **non-leak d'exceptions à l'utilisateur (CWE-209)**.
- **`services/riot_api.py`** : client réseau robuste - `timeout` explicite, retry exponentiel **uniquement** sur transitoires (réseau, 5xx), pas de retry sur 404/429, `urllib.parse.quote(safe='')` (anti path-injection), cache TTL et `requests.Session` protégés par `threading.Lock`, troncature des corps d'erreur à 200 car.
- **`cogs/match/_cog.py`** : circuit-breaker Henrik, sémaphore d'édition de membres (anti-429 Discord), batch fetch (anti N+1), recovery au boot (cleanup orphelin, contested expirés).
- **`bot.py`** : fail-fast sur `MONGO_URL`/`DISCORD_TOKEN` absents, `serverSelectionTimeoutMS`/`connectTimeoutMS` bornés, sync slash one-shot (anti-spam Discord), logs stdout/stderr séparés.
- **Autorisation des slash commands** : cohérente - décorateur `has_permissions(manage_guild=True)` (+ handlers `.error`) ou garde runtime `_has_access` (manage_guild OU rôle bypass par **ID**) ; les actions destructives (`reset-queue`, `match-cleanup`) exigent admin/manage_guild ; décisions de candidature protégées par CAS atomique anti-double-traitement.
- **Sécurité statique** : aucun `eval`/`exec`/`pickle`/`yaml.load`/`subprocess`/`shell=True` ; aucun secret en dur (token/Mongo via env) ; 0 CVE deps ; 0 code mort.
