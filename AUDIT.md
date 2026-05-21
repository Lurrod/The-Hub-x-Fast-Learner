# Audit Python — 2026-05-21

> Mode : `READ_ONLY`. Aucune modification appliquée. Cible : tout le repo (`bot.py`, `cogs/`, `services/`, `tests/`, `scripts/`, `leaderboard_img.py`).
> Audit précédent : 2026-05-19 (déprécié — remplacé par ce document).

## Synthèse

- **État général :** Le projet est globalement sain — pip-audit clean, bandit sans HIGH/MEDIUM, 382 tests verts, architecture cog/service bien découpée. Mais la livraison récente du feature « catégories dynamiques » (V3.13, commits `a38fd4c..f86f755`) a introduit **un bug bloquant** caché dans le filtre Mongo de `cog_load`, plus une régression d'hygiène (lint, format, BOM). Les tests sont passés en isolation mais avec des statuts factices qui ne reflètent pas la production.
- **Score : 6.5/10** (était ~8 avant V3.13 — le filtre cassé fait perdre 1.5 pt).
- **Top 3 actions prioritaires :**
  1. **Corriger le filtre statut du cleanup orphelin** (`cogs/match/_cog.py:1354`). Sans ça, les catégories de match en cours seront supprimées au prochain redémarrage du bot.
  2. **Retirer le BOM UTF-8** de `cogs/match/_cog.py` et `tests/test_match_cog.py` (casse `radon`, encodage non standard).
  3. **Faire passer `ruff check` et `ruff format`** — 16 erreurs lint et 11 fichiers mal formatés, dont 9 issus de la livraison V3.13. Pre-commit hook a manifestement été bypassé.

## Métriques

| Outil       | Résultat                                                                |
|-------------|-------------------------------------------------------------------------|
| ruff check  | **16 erreurs** (14 auto-fixable), 0 warning                              |
| ruff format | **11 fichiers à reformater** sur 56                                      |
| mypy        | **1 erreur** (`services/repository.py:849`) — module-level mode laxiste configuré |
| bandit      | **0 HIGH, 0 MEDIUM, 6 LOW** (toutes en contexte non-cryptographique)     |
| pip-audit   | **0 vulnérabilité connue** (requirements.txt + requirements-test.txt)   |
| radon cc    | Moyenne **C (16.4)** sur 16 blocs ≥ C ; **D (29)** sur `QueueView._join_callback` |
| radon mi    | Tous fichiers en **A** (sauf `cogs/match/_cog.py` non analysable — BOM) |
| pytest      | **382 passed, 0 failed, 0 skipped** en 74 s                              |
| coverage    | **74 %** global (cible projet 80 %) ; `services/match_category.py` **100 %** |
| vulture     | 0 finding à confiance ≥ 80 % (callbacks Discord cachés sous 60 %)        |
| LOC         | ~14 200 lignes Python sur 58 fichiers                                    |

## Findings

### [CRITIQUE] Filtre `cog_load` orphan-cleanup ne matche aucun match actif réel

- **Localisation :** `cogs/match/_cog.py:1354-1362` (méthode `cog_load`)
- **Catégorie :** correction, robustesse
- **Problème :** Le filtre Mongo `{"status": {"$in": ["active", "disputed"]}}` est utilisé pour construire le set des `active_category_ids` à protéger contre le cleanup orphelin. Or aucun de ces deux statuts n'existe en production. Les statuts réels persistés sont : `pending` (match en cours, vote ouvert), `validated_a` / `validated_b` (vote validé, ELO non encore appliqué), `contested` (timeout vote sans consensus), `cancelled`, `cleaned_up`. Vérifié par grep sur `services/repository.py:265-783` et `cogs/match/_cog.py:655-713`.
- **Impact :** Au prochain démarrage du bot, **toutes les catégories `Match #N` correspondant à des matchs en cours (`pending`) seront supprimées comme "orphelines"**, alors que les joueurs sont encore en draft / partie / vote. Idem pour les matchs `contested` que le design voulait justement préserver pour la revue admin. C'est un *production-breaking bug*.
- **Correctif :**
  ```python
  # Remplacer
  {"status": {"$in": ["active", "disputed"]}}
  # par
  {"status": {"$in": ["pending", "validated_a", "validated_b", "contested"]},
   "elo_applied": {"$ne": True}}
  # (ou s'aligner sur les statuts effectivement utilisés dans transition_match_status)
  ```
  Ajouter un test d'intégration qui sème un doc `{"status": "pending", "category_id": X}` réel et vérifie que `cog_load` NE supprime PAS `X`.
- **Référence :** CWE-561 (Dead code / unreachable filter), incidence opérationnelle.

---

### [MAJEUR] Régression ruff lint : 16 erreurs sur la livraison V3.13

- **Localisation :** voir détail ci-dessous
- **Catégorie :** dette technique, hygiène
- **Problème :** Le projet était lint-clean au 2026-05-19. 16 nouvelles erreurs ont été introduites par la livraison V3.13, dont 7 directives `# noqa: BLE001` posées sans que `BLE001` ne soit dans la liste `select` de `pyproject.toml` → ruff les flague comme `RUF100` "unused noqa".

  | Erreur                                                                   | Code | Fichier                                                                      |
  |--------------------------------------------------------------------------|------|------------------------------------------------------------------------------|
  | `discord` imported but unused                                            | F401 | `cogs/leaderboard_weekly.py:22`                                              |
  | 7× unused `# noqa: BLE001`                                               | RUF100 | `cogs/match/_cog.py:231,1373` ; `services/match_category.py:72,76,159,165,193` |
  | try/except/pass → `contextlib.suppress`                                  | SIM105 | `cogs/match/_cog.py:733`                                                     |
  | Quoted type annotation inutile                                           | UP037 | `services/leaderboard_refresh.py:421`                                        |
  | Import `Iterable` depuis `typing` (deprecated, utiliser `collections.abc`) | UP035 | `services/match_category.py:13`                                              |
  | `discord` imported but unused                                            | F401 | `services/match_service.py:26`                                               |
  | `AsyncMock` aliased as acronyme `AM`                                     | N817 | `tests/test_match_category.py:417`                                           |
  | imports morts (`AsyncMock`, `MagicMock`, `UTC`)                          | F401 | `tests/test_match_category.py:417` ; `tests/test_match_service.py:4` ; `tests/test_shared_collections.py:9` |

- **Impact :** Le hook pre-commit `ruff` aurait dû bloquer ces commits. La présence de ces erreurs prouve que `--no-verify` a été utilisé ou que le hook n'est plus actif. À chaque CI ou re-installation du hook, ces 16 erreurs bloqueront tout nouveau commit.
- **Correctif :** `uvx ruff check . --fix` corrige 14 erreurs automatiquement. Les 2 restantes (SIM105 contextlib.suppress et le bloc nominatif) demandent une édition manuelle minimale.
- **Référence :** PEP 8, project rule `rules/python/coding-style.md`.

---

### [MAJEUR] BOM UTF-8 dans deux fichiers source

- **Localisation :** `cogs/match/_cog.py:1`, `tests/test_match_cog.py:1`
- **Catégorie :** dette technique, outillage
- **Problème :** Les 3 premiers octets de ces fichiers sont `EF BB BF` (BOM UTF-8). Python tolère le BOM en byte mode mais ce n'est pas standard, et l'outil `radon cc` plante avec `SyntaxError: invalid non-printable character U+FEFF` (impossible d'évaluer la complexité ni la maintenabilité du plus gros fichier du projet).
- **Impact :** `radon`, `bandit -ll`, et certains analyseurs statiques aveugles ne peuvent pas traiter ces fichiers. Encodage non idiomatique, probablement introduit par PowerShell `Set-Content` / `Out-File` (le CLAUDE.md global mentionne explicitement ce comportement par défaut sur PS 5.1).
- **Correctif :**
  ```python
  for p in ("cogs/match/_cog.py", "tests/test_match_cog.py"):
      data = open(p, "rb").read()
      if data.startswith(b"\xef\xbb\xbf"):
          open(p, "wb").write(data[3:])
  ```
  Compléter `.pre-commit-config.yaml` par un hook `fix-byte-order-marker` (déjà fourni par `pre-commit-hooks`).
- **Référence :** PEP 263, PEP 3120 (encodage source par défaut = UTF-8 sans BOM).

---

### [MAJEUR] `ruff format --check` échoue sur 11 fichiers

- **Localisation :** `cogs/leaderboard_weekly.py`, `cogs/match/_cog.py`, `services/match_category.py`, `services/match_service.py`, `tests/test_match_category.py`, `tests/test_match_cleanup_command.py`, `tests/test_match_cog.py`, `tests/test_pagination.py`, `tests/test_queue_v2.py`, `tests/test_repository_helpers.py`, `tests/test_startup_cleanup.py`
- **Catégorie :** dette technique, hygiène
- **Problème :** Ces fichiers ne sont pas conformes à `ruff format`. 9 sur 11 ont été modifiés pendant la livraison V3.13. Le hook pre-commit `ruff-format` aurait dû corriger.
- **Impact :** Confirme que le hook pre-commit n'a pas tourné (probablement `--no-verify`). Diffusion d'un style hétérogène.
- **Correctif :** `uvx ruff format .` (auto-fix).
- **Référence :** PEP 8.

---

### [MAJEUR] `_admin_role_ids` retourne toujours `[]` — modérateurs sans `administrator` exclus

- **Localisation :** `cogs/match/_cog.py` (méthode `_admin_role_ids`)
- **Catégorie :** robustesse, régression de design V3.13
- **Problème :** La nouvelle matrice de permissions sur les catégories dynamiques pose `allow view_channel` sur chaque rôle admin retourné par `_admin_role_ids`. Cette méthode contient un `TODO` et renvoie `[]`. Conséquence : seuls les utilisateurs avec la permission Discord `administrator` (qui bypasse tous les overwrites) voient les catégories de match. Un rôle modérateur custom avec `manage_messages`/`manage_channels` mais sans `administrator` est **structurellement exclu**.
- **Impact :** Régression vs le système précédent où le rôle `Match #N` rendait les catégories visibles aux modérateurs qui l'avaient. À traiter avant que le bug ne soit signalé par les modérateurs en prod.
- **Correctif :** Câbler une lecture en BDD (collection `guild_state.admin_role_ids` par exemple) ou réutiliser le bypass existant (`repository.get_bypass_role`). À chiffrer en suivi avec PR dédiée.
- **Référence :** issue interne — à créer.

---

### [MAJEUR] `mypy` flag un index ambigu sur `reserve_match_number`

- **Localisation :** `services/repository.py:849` (`return int(doc["match_counter"])`)
- **Catégorie :** typage
- **Problème :** `find_one_and_update(..., upsert=True, return_document=ReturnDocument.AFTER)` est typé `Any | None`. Avec `upsert=True` le `None` n'est pas atteignable, mais mypy ne le sait pas.
- **Impact :** mypy de la CI échoue (1 erreur). Bloque tout durcissement `--strict` futur.
- **Correctif :**
  ```python
  doc = db["guild_state"].find_one_and_update(...)
  assert doc is not None  # upsert=True garantit un doc
  return int(doc["match_counter"])
  ```
  Ou `from typing import cast; cast(dict, doc)["match_counter"]`.
- **Référence :** pymongo typing — `find_one_and_update` overload.

---

### [MAJEUR] `_before_loop` lève `TypeError` quand `bot` est un `MagicMock` (warning silencieux en tests)

- **Localisation :** `cogs/match/_cog.py:1062`
- **Catégorie :** testabilité
- **Problème :** `_before_loop` fait `await self.bot.wait_until_ready()`. Quand les tests construisent un `MatchCog(bot=MagicMock(), ...)`, l'attribut `wait_until_ready` est un `MagicMock` non-awaitable. `tasks.loop` log alors `Task exception was never retrieved: TypeError: object MagicMock can't be used in 'await' expression` pendant la suite. Le test passe, mais le warning pollue la sortie et signale un trou de test isolation.
- **Impact :** Pollution des logs CI, indique que `cog_load` démarre le timeout loop en environnement de test. Pas de failure aujourd'hui, mais risque de masquer un vrai bug si quelqu'un ignore le bruit.
- **Correctif :** Soit changer les fixtures pour utiliser un bot mocké avec `wait_until_ready=AsyncMock()`, soit envelopper le `_timeout_loop.start()` dans `cog_load` derrière un flag `start_loops=True` (skip dans les tests).
- **Référence :** discord.py `tasks.Loop.before_loop`.

---

### [MINEUR] Coverage globale 74 % vs cible 80 %

- **Localisation :** principalement `cogs/admin.py` (50 %), `cogs/applications.py` (52 %), `cogs/match/_cog.py` (59 %), `services/leaderboard_refresh.py` (61 %), `cogs/elo_admin.py` (64 %), `bot.py` (64 %)
- **Catégorie :** testabilité
- **Problème :** Gap pré-existant aggravé par l'ajout massif de code dans `_cog.py` et l'impossibilité de tester certains chemins Discord sans gateway live.
- **Impact :** En-dessous de la cible définie par la règle utilisateur `rules/common/testing.md`. Modules les moins couverts (admin, applications, leaderboard) sont aussi les moins critiques fonctionnellement, mais ce sont des chemins exposés aux utilisateurs.
- **Correctif :** PR de couverture ciblée sur `applications.py` (welcome/report flows) et `leaderboard_refresh.py` (pagination edge cases) — viser +6 % overall.

---

### [MINEUR] Complexité cyclomatique D (29) sur `QueueView._join_callback`

- **Localisation :** `cogs/queue_v2.py:264`
- **Catégorie :** lisibilité
- **Problème :** La fonction enchaîne 8 branches majeures (defer, role gate, type check, riot lookup, multi-queue check, Qualification Pro cap, atomic insert, queue-full trigger), 5 envois ephémeraux, 1 acquisition de lock par-guild, et un `asyncio.gather` à 3 tâches. Difficile à lire en linéaire.
- **Impact :** Maintenance — toute modif (ex: ajouter une nouvelle queue gated) demande de comprendre l'entièreté du flux. Densité de bugs probable lors d'évolutions.
- **Correctif :** Extraire les pré-checks (`_validate_join_preconditions(inter) -> tuple[bool, str | None]`) et la phase post-lock (`_finalize_join(inter, queue_doc) -> None`). Tests unitaires découpés en conséquence.
- **Référence :** project rule `rules/common/coding-style.md` — fonctions < 50 lignes (l'actuelle dépasse 110).

---

### [MINEUR] Autres fonctions à complexité C (12-19)

- **Localisation :**
  - `cogs/admin.py:50` `AdminCog.setup_bot` C(18)
  - `cogs/applications.py:442` `ApplicationReviewView.accept` C(16)
  - `cogs/queue_v2.py:624` `QueueCog.close_queue` C(13)
  - `cogs/match/_embeds.py:72` `build_match_embed_from_doc` C(15) et `:141` `build_elo_changes_embed` C(12)
  - `cogs/match/_vote.py:34` `VoteView._vote` C(19)
  - `services/elo_updater.py:51` `apply_match_validation` C(17)
  - `services/leaderboard_refresh.py:441` `refresh_leaderboard_channel` C(19), `:301` `build_leaderboard_payload` C(16)
  - `services/match_verifier.py:84` `compute_acs_multipliers` C(14)
  - `services/riot_api.py:306` `_parse_match` C(17), `:155` `HenrikDevClient._get` C(17)
  - `services/team_balancer.py:49` `balance_teams` C(16)
- **Catégorie :** lisibilité
- **Problème :** Limites C de radon (rang « modérément complexe »). Acceptable pour des handlers Discord, mais à surveiller.
- **Impact :** Risque de bugs lors d'évolutions, charge cognitive élevée.
- **Correctif :** À traiter au cas par cas si le code change. Pas d'action immédiate requise — la règle projet `rules/common/code-review.md` tolère C en handlers de cog.

---

### [MINEUR] Bandit : 6 LOW (B311 random + B101 assert)

- **Localisation :**
  - `cogs/admin.py:177,190` — `random.choice` pour `/map` et `/coinflip`
  - `cogs/match/_cog.py:90` — `random.Random()` pour le draft / picks
  - `cogs/prefix_legacy.py:245` — `random.choice` pour `/map` legacy
  - `services/match_service.py:91` — `random.Random()` pour balance_teams
  - `services/team_balancer.py:95` — `assert best is not None`
- **Catégorie :** sécurité (false positives ici)
- **Problème :** Bandit signale par défaut tout usage de `random` non-crypto. Aucun de ces sites n'a besoin de CSPRNG (cosmétique : tirage carte, pile/face, picks de match). L'assert est un garde-fou logique sur une fonction interne.
- **Impact :** Aucun risque sécurité. Bruit dans le rapport bandit.
- **Correctif :** Ignorer (configurer `bandit -ll` pour ne remonter que MEDIUM+, ou ajouter `# nosec B311` ciblé). Pas d'action urgente.
- **Référence :** CWE-330, CWE-703 — non applicables ici.

---

### [MINEUR] Mojibake dans les commentaires de fichiers anciens

- **Localisation :** `bot.py`, `cogs/admin.py`, `cogs/applications.py`, `cogs/elo_admin.py`, `services/riot_api.py`, etc. (tous les fichiers avec des séparateurs commentaires)
- **Catégorie :** lisibilité
- **Problème :** Les séparateurs visuels en commentaire (`# ── ... ───`) sont stockés en `â”€â”€` (mojibake CP1252 sur UTF-8). Probablement un fichier transformé d'UTF-8 vers CP1252 puis re-sauvé en UTF-8 par un éditeur.
- **Impact :** Cosmétique — illisible en édition, mais Python s'en moque (commentaires UTF-8 valides).
- **Correctif :** PR globale `sed`-style : `â”€` → `─`. Faible priorité.

---

### [SUGGESTION] Tests V3.13 utilisent des statuts factices, pas ceux de la production

- **Localisation :** `tests/test_match_cleanup_command.py:54,75`, `tests/test_startup_cleanup.py:28-29`
- **Catégorie :** testabilité
- **Problème :** Les tests injectent `status: "active"` / `status: "disputed"` qui n'existent NULLE PART dans `services/repository.py` ni `cogs/match/_cog.py`. Les tests vérifient ainsi que le code traite *des chaînes de caractères qui ne se produiront jamais*, masquant le bug CRITIQUE #1.
- **Impact :** Faux sentiment de couverture. Le test passe mais le code de production reste cassé.
- **Correctif :** Centraliser les constantes de statut dans un seul `Final` ou enum, et faire référencer les tests sur cette source unique. Cas d'école d'AI regression testing.
- **Référence :** CWE-1006 (Bad coding practices in tests).

---

### [SUGGESTION] `requirements.txt` déclare `discord.py>=2.5,<3` mais l'install local est `2.3.2`

- **Localisation :** `requirements.txt`
- **Catégorie :** dépendances
- **Problème :** Mismatch documenté en mémoire. `pip install -r requirements.txt` reinstall en `>=2.5`, mais l'environnement courant tourne en 2.3.2 (probablement non upgrade pour éviter casse).
- **Impact :** Comportements asymétriques entre dev local et CI. Les tests peuvent passer en local sur 2.3.2 et casser en CI sur 2.5+.
- **Correctif :** Soit faire l'upgrade complet (vérifier breaking changes 2.5 vs 2.3) soit pinner précisément (`discord.py==2.3.2`).

---

### [SUGGESTION] Pas d'étape mypy / ruff en CI GitHub Actions

- **Localisation :** `.github/workflows/` (à vérifier)
- **Catégorie :** dette technique
- **Problème :** Le `.pre-commit-config.yaml` est solide mais bypass possible avec `--no-verify` (comme la livraison V3.13 le prouve). Sans gate CI, rien n'empêche la régression.
- **Impact :** Hygiène code dépend de la discipline humaine.
- **Correctif :** Ajouter un workflow GHA `python-quality.yml` qui run `ruff check`, `ruff format --check`, `mypy`, `pytest --cov`. Bloquer le merge si rouge.

---

## Ce qui va bien

- **Aucune vulnérabilité dépendances** (pip-audit clean).
- **Pas de secret en dur** ni d'usage dangereux (`eval`, `exec`, `subprocess`, `shell=True`). Tokens via env, MongoDB validation au boot.
- **Cache TTL thread-safe** pour `_TTLCache` (lock proprement utilisé).
- **Retry exponentiel avec backoff** sur Henrik API, types d'erreurs distincts (`PlayerNotFoundError`, `RateLimitedError`).
- **Idempotence MongoDB** : `find_one_and_update` avec `upsert`, indexes créés au lazy.
- **Atomicité du compteur** `reserve_match_number` via `$inc + upsert + ReturnDocument.AFTER`.
- **Logging structuré** partout (pas un seul `print()`), gestion d'erreurs avec `logger.exception`.
- **Architecture cog/service propre**, single-responsibility largement respecté.
- **dpytest + mongomock + pytest-asyncio**, suite de 382 tests verts.
- **Pre-commit hook ruff + format + file fixers** configuré (juste non respecté par la dernière PR).
- **`services/match_category.py` : 100 % de couverture** — le nouveau module est exemplaire en isolation, c'est l'intégration qui pose problème.

## Résumé chiffré

- **1** problème CRITIQUE à corriger avant le prochain redémarrage prod (filtre cog_load).
- **6** problèmes MAJEUR à traiter rapidement (lint, format, BOM, admin perms, mypy, test isolation).
- **5** problèmes MINEUR — dette tolérable, à intégrer dans le backlog.
- **3** SUGGESTION — amélioration continue.

Le fix prioritaire (#1) est un patch d'une ligne. Les fixes 2-4 (BOM, lint, format) sont mécaniques (`uvx ruff` + script BOM). Les fixes 5-7 demandent un peu de réflexion mais restent bornés. Si on traite #1 à #4 dans la journée, le projet remonte à **8.5/10**.
