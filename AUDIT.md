# Audit Python — 2026-05-21 (post-fix)

> Audit initial conduit en mode `READ_ONLY` à 15:54 ; corrections appliquées à partir de 16:00.
> Audit précédent : 2026-05-19 (déprécié).

## Synthèse post-fix

- **État général :** Tous les findings CRITIQUE et MAJEUR remontés à 15:54 ont été corrigés. Le projet est désormais ruff-clean, ruff-format-clean, mypy-clean, et la suite de **382 tests passe sans pollution**. Le bug bloquant du filtre `cog_load` a été corrigé, les modérateurs sans `administrator` voient à nouveau les catégories de match, le BOM UTF-8 a été retiré, et un gate CI ruff/format/mypy/pytest+cov a été ajouté.
- **Score : 8.5/10** (avant : 6.5/10 ; gain : +2 pts).
- **Reste en backlog (MINEUR / SUGGESTION) :**
  - Coverage 74 % (cible 80 %) — concentré sur `cogs/admin.py`, `cogs/applications.py`, `cogs/match/_cog.py`, `services/leaderboard_refresh.py`.
  - Complexité D(29) sur `QueueView._join_callback` et 13 fonctions à C(12-19).
  - Dépendance discord.py : requirements >=2.5, local 2.3.2 (mismatch).
  - 6 bandit LOW + 1 nouvelle assert (B101 sur `reserve_match_number`) — false positives.

## Corrections appliquées (4 commits sur main)

| Commit    | Sujet                                                                          | Files |
|-----------|--------------------------------------------------------------------------------|-------|
| `624f5eb` | strip BOM, ruff --fix, ruff format, contextlib.suppress                        | 14    |
| `a4aa27f` | **CRITICAL** cog_load filter fix + admin role wiring + test isolation         | 4     |
| `922bf96` | CI quality gate (ruff/format/mypy) + fix-byte-order-marker pre-commit         | 2     |

## Métriques post-fix

| Outil       | Avant                  | Après                                  |
|-------------|------------------------|----------------------------------------|
| ruff check  | 16 erreurs             | **0 erreur** ✅                         |
| ruff format | 11 fichiers à reformater | **0 fichier** ✅                       |
| mypy        | 1 erreur               | **0 erreur** ✅                         |
| bandit      | 6 LOW                  | **7 LOW** (nouveau B101 attendu)        |
| pip-audit   | 0 vuln                 | 0 vuln                                 |
| pytest      | 382 passed             | **382 passed, 0 warning bruyant** ✅    |
| coverage    | 74 %                   | 74 % (inchangé — backlog)               |
| BOM files   | 2                      | **0** ✅                                |

## Findings — état détaillé

### [CRITIQUE] Filtre `cog_load` orphan-cleanup — ✅ **RÉSOLU**

- **Localisation :** `cogs/match/_cog.py` (méthode `cog_load`)
- **Correctif appliqué (commit `a4aa27f`) :**
  ```python
  _ACTIVE_MATCH_STATUSES: tuple[str, ...] = (
      "pending", "validated_a", "validated_b", "contested",
  )
  active_ids = {
      m["category_id"] for m in self.db["matches"].find(
          {"status": {"$in": list(self._ACTIVE_MATCH_STATUSES)},
           "elo_applied": {"$ne": True}},
          {"category_id": 1},
      ) if m.get("category_id")
  }
  ```
- **Test ajouté :** `tests/test_startup_cleanup.py::test_cog_load_deletes_orphan_categories` vérifie maintenant le contenu exact du filtre Mongo (`{"pending", "validated_a", "validated_b", "contested"}`) en plus du comportement.

### [MAJEUR] Régression ruff lint — ✅ **RÉSOLU**

- **Correctif appliqué (commit `624f5eb`) :**
  - 14 erreurs auto-corrigées par `uvx ruff check . --fix` (F401, RUF100, UP037, UP035, N817).
  - 1 erreur SIM105 corrigée à la main (`cogs/match/_cog.py:733` — try/except/pass → `contextlib.suppress`).
- **Vérification :** `uvx ruff check .` retourne `All checks passed!`

### [MAJEUR] BOM UTF-8 dans 2 fichiers — ✅ **RÉSOLU**

- **Correctif appliqué (commit `624f5eb`) :** strip des 3 premiers octets `EF BB BF` sur `cogs/match/_cog.py` et `tests/test_match_cog.py`.
- **Régression-proof (commit `922bf96`) :** ajout du hook `fix-byte-order-marker` dans `.pre-commit-config.yaml`.
- **Bonus :** `radon cc cogs/match/_cog.py` est désormais analysable (avant : SyntaxError sur U+FEFF).

### [MAJEUR] `ruff format --check` 11 fichiers — ✅ **RÉSOLU**

- **Correctif appliqué (commit `624f5eb`) :** `uvx ruff format .` sur 11 fichiers.
- **Vérification :** `56 files already formatted`.

### [MAJEUR] `_admin_role_ids` retourne `[]` — ✅ **RÉSOLU**

- **Localisation :** `cogs/match/_cog.py:401-430` (méthode `_admin_role_ids`)
- **Correctif appliqué (commit `a4aa27f`) :** la méthode lookup maintenant chaque rôle nommé dans `ADMIN_ROLE_NAMES` (`"Admin"`, `"Match Staff"`, `"Administrateur"`) et ajoute aussi le rôle de bypass configuré via `/bypass`. Sans cette correction, les staff custom étaient structurellement exclus des catégories dynamiques.
- **Choix d'implémentation :** itération directe sur `guild.roles` (pas `discord.utils.get`) pour éviter le fallback `_aget` qui renvoie une coroutine sur les MagicMock de tests.

### [MAJEUR] `mypy` flag sur `reserve_match_number` — ✅ **RÉSOLU**

- **Localisation :** `services/repository.py:849`
- **Correctif appliqué (commit `a4aa27f`) :**
  ```python
  doc = db["guild_state"].find_one_and_update(..., upsert=True, ...)
  assert doc is not None, "find_one_and_update(upsert=True, AFTER) doit renvoyer un doc"
  return int(doc["match_counter"])
  ```
- **Vérification :** `mypy` clean sur 27 source files.

### [MAJEUR] `_before_loop` MagicMock TypeError en tests — ✅ **RÉSOLU**

- **Localisation :** `cogs/match/_cog.py:1056` (`_before_loop`) + `cog_load`
- **Correctif appliqué (commit `a4aa27f`) :** `_timeout_loop.start()` n'est invoqué que si `isinstance(self.bot, commands.Bot)`. Tests utilisant `MagicMock()` skippent silencieusement le loop start (le loop n'a de sens qu'avec un gateway Discord vivant).
- **Vérification :** plus aucun `Task exception was never retrieved: TypeError` dans la sortie pytest.

### [MAJEUR] CI sans gate ruff/mypy — ✅ **RÉSOLU**

- **Localisation :** `.github/workflows/ci.yml`
- **Correctif appliqué (commit `922bf96`) :** ajout d'un job `quality` qui run `ruff check`, `ruff format --check`, `mypy services cogs bot.py`. Le job `test` collecte maintenant aussi la couverture.
- **Effet :** la régression de classe V3.13 (lint/format/BOM) ne pourra plus passer sans avoir été visible dans la CI.

---

### [MINEUR] Coverage 74 % vs cible 80 % — **EN BACKLOG**

- Pas adressé dans cette session : demande des PR de couverture ciblées sur `cogs/admin.py` (50 %), `cogs/applications.py` (52 %), `cogs/match/_cog.py` (59 %), `services/leaderboard_refresh.py` (61 %). Effort estimé : 1-2 jours.

### [MINEUR] Complexité D(29) sur `QueueView._join_callback` — **EN BACKLOG**

- Pas adressé : refactor en 2-3 helpers (`_validate_join_preconditions`, `_finalize_join`) demande review + tests dédiés. Effort : 0.5-1 jour.

### [MINEUR] Autres fonctions à complexité C(12-19) — **TOLÉRÉ**

- 13 fonctions à surveiller mais limites acceptables pour handlers Discord.

### [MINEUR] Bandit 7 LOW — **TOLÉRÉ**

- 6 B311 random non-crypto (cosmétique : map/coinflip/draft) + 2 B101 assert (`team_balancer.py:95` historique + `repository.py:849` nouveau garde-fou typing). Tous légitimes.

### [MINEUR] Mojibake commentaires — **FAUX POSITIF**

- Vérification post-audit : les fichiers contiennent bien `─` (U+2500) en UTF-8 valide. Le `â”€` constaté pendant l'audit n'était que le rendu PowerShell CP1252. **Aucune action nécessaire.**

---

### [SUGGESTION] Tests V3.13 à statut factice — ✅ **RÉSOLU**

- **Correctif appliqué (commit `a4aa27f`) :** `tests/test_startup_cleanup.py` et `tests/test_match_cleanup_command.py` utilisent maintenant les statuts réels (`pending`, `contested`, `validated_a`) au lieu de `active`/`disputed` inexistants en production.

### [SUGGESTION] discord.py 2.5 vs install 2.3.2 — **EN BACKLOG**

- Pas adressé : nécessite revue des breaking changes discord.py 2.4/2.5. Pinner `discord.py==2.3.2` en attendant.

### [SUGGESTION] CI manquante — ✅ **RÉSOLU** (cf. ci-dessus)

## Reste à faire (backlog priorisé)

| # | Item                                              | Effort | Sévérité initiale |
|---|---------------------------------------------------|--------|-------------------|
| 1 | Coverage +6 % (applications, leaderboard, admin) | 1-2 j  | MINEUR            |
| 2 | Refactor `QueueView._join_callback` D(29) → 2-3 C | 0.5-1 j| MINEUR            |
| 3 | Décider du sort de discord.py 2.5 vs 2.3.2       | 0.5 j  | SUGGESTION        |

## Récap

Le score passe de **6.5/10 à 8.5/10**. Le bug CRITIQUE de production est neutralisé, l'hygiène lint/format/types est restaurée et protégée par un gate CI. Les éléments restants sont du backlog tolérable.

Commits de cette session :
```
922bf96 ci: add ruff/format/mypy gates + BOM-guard pre-commit hook
a4aa27f fix(match): critical cog_load filter + admin role wiring + test isolation
624f5eb style(repo): strip BOM, apply ruff --fix + format, contextlib.suppress
```
