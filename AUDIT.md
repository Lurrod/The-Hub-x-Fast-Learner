# Audit Python — 2026-05-19

> Mode `READ_ONLY` : aucune modification appliquée. Outils exécutés en
> environnement éphémère via `uvx` (pas d'installation locale).

## Synthèse

- **État général** : projet sain, bien testé (330 tests, 84 % coverage),
  patterns Mongo atomiques (CAS, indexes, idempotence), aucune vulnérabilité
  dépendance, aucun secret en dur. La dette principale est concentrée dans
  `cogs/match.py` (taille + complexité) et dans un mismatch de version Python.
- **Score** : **7.5 / 10**
- **Top 3 actions prioritaires** :
  1. Découper `cogs/match.py` (1678 lignes, `MatchCog.on_queue_full` CC=35)
     en sous-modules par responsabilité (formation, vote, vérification Henrik,
     cleanups). C'est le seul fichier rouge sur tous les axes.
  2. Aligner la version Python locale avec le `target-version` (`py311`) :
     `discord.py==2.3.2` casse sur Python 3.13 (`audioop` retiré → import
     fail). Soit pin un venv 3.11, soit upgrade `discord.py` ≥ 2.5.
  3. Ajouter des tests pour `cogs/moderation.py` (28 % de coverage — le cog
     `/warn` n'a *aucun* test).

## Métriques

| Outil | Résultat |
|-------|----------|
| ruff check | **16 erreurs** (7 auto-fixables, 5 fixables en `--unsafe-fixes`) |
| ruff format --check | **42 fichiers** à reformater |
| mypy (config laxiste du projet) | **7 erreurs** (2 fichiers) |
| bandit | **0 HIGH / 0 MEDIUM / 6 LOW** (5× B311 `random` non-crypto + 1× B101 `assert`) |
| pip-audit (requirements.txt) | **Aucune vulnérabilité connue** |
| radon CC moyen | **A (3.99)** sur 301 blocs |
| radon MI | Tous `A` sauf `cogs/match.py` = **C** |
| vulture (conf ≥ 80) | Aucun code mort |
| pytest | **330 passed** en 79 s |
| coverage totale | **84 %** (seuil 80 % ✅) |

## Hotspots de complexité (CC ≥ C)

| Fichier:ligne | Symbole | CC |
|---|---|---|
| cogs/match.py:400 | `MatchCog.on_queue_full` | **E (35)** |
| cogs/match.py:1500 | `MatchCog.match_replace` | **D (23)** |
| cogs/match.py:267 | `VoteView._vote` | C (19) |
| cogs/queue_v2.py:284 | `QueueView._join_callback` | C (18) |
| cogs/match.py:1087 | `MatchCog._handle_timeout` | C (18) |
| cogs/admin.py:48 | `AdminCog.setup_bot` | C (17) |
| services/riot_api.py:155 | `HenrikDevClient._get` | C (17) |
| services/riot_api.py:301 | `_parse_match` | C (17) |
| services/leaderboard_refresh.py:361 | `refresh_leaderboard_channel` | C (16) |
| services/team_balancer.py:49 | `balance_teams` | C (16) |
| cogs/applications.py:352 | `ApplicationReviewView.accept` | C (16) |
| services/match_verifier.py:81 | `compute_acs_multipliers` | C (14) |
| services/elo_updater.py:51 | `apply_match_validation` | C (14) |

## Coverage faible (production)

| Fichier | % |
|---|---|
| `cogs/moderation.py` | **28 %** |
| `cogs/admin.py` | 50 % |
| `cogs/applications.py` | 52 % |
| `cogs/match.py` | 59 % |
| `services/leaderboard_refresh.py` | 62 % |

(`preview_leaderboard.py` et `seed_users.py` à 0 % : outils dev, déjà exclus
par la config ruff — acceptable.)

---

## Findings

### [MAJEUR] Incompatibilité Python 3.13 ↔ discord.py 2.3.2

- **Localisation** : `pyproject.toml:6` (`target-version = "py311"`) vs runtime local Python 3.13.2
- **Catégorie** : dette technique / portabilité
- **Problème** : `discord.py 2.3.2` importe `audioop` (`voice_client.py`),
  module retiré de la stdlib en Python 3.13 (PEP 594). Tout `pytest` ou
  exécution locale échoue immédiatement avec
  `ModuleNotFoundError: No module named 'audioop'`.
- **Impact** : impossible de faire tourner le projet sur la machine de dev
  sans switch manuel vers Python 3.11. Les nouvelles installations (`pip install`
  sur 3.13) sembleront fonctionner puis crashent à l'import.
- **Correctif** :
  - Soit forcer un `venv` 3.11 documenté (et bloquer `python_requires<3.13`
    dans `pyproject.toml`).
  - Soit upgrader `discord.py>=2.5` (support Py3.13 + dépendance audio devient optionnelle).
- **Référence** : [PEP 594](https://peps.python.org/pep-0594/), [discord.py 2.5 release notes](https://github.com/Rapptz/discord.py/releases/tag/v2.5.0)

### [MAJEUR] `cogs/match.py` : monolithe 1678 lignes, MI = C

- **Localisation** : `cogs/match.py` (entier)
- **Catégorie** : maintenabilité
- **Problème** : seul fichier au-dessus de la limite des 800 lignes (cf. règle
  `coding-style.md`) et seul module à MI rang `C`. Concentre 8 des 13 hotspots
  CC du repo, dont `on_queue_full` à CC=35 (range E, très élevé).
- **Impact** : modifier le flux de formation ou de vote requiert de garder
  ~1500 lignes en tête. Coverage 59 % alors que les fichiers voisins sont >80 %.
- **Correctif** : extraire 4 sous-modules :
  - `cogs/match/formation.py` : `on_queue_full`, `_setup_roles_for`, `_move_players_*`
  - `cogs/match/vote.py` : `VoteView`, `build_match_embed*`, `_on_match_validated`
  - `cogs/match/verification.py` : Henrik circuit breaker, `_fetch_henrik_multipliers`, `_verify_match`
  - `cogs/match/cleanups.py` : `_timeout_loop`, `_process_role_cleanups_for_guild`, `_check_vote_timeouts_for_guild`
- **Référence** : `~/.claude/rules/common/coding-style.md` (800 LOC max)

### [MAJEUR] `cogs/moderation.py` : 28 % de couverture, 0 test

- **Localisation** : `cogs/moderation.py`
- **Catégorie** : testabilité
- **Problème** : cog `/warn` + `/warn-list` ajouté récemment (cf. histoire git
  du 2026-05-18), aucun fichier `tests/test_moderation.py`. Les chemins critiques
  (gate de rôle `_has_warn_access`, DM `Forbidden`, persistance, filtre `member_id`)
  ne sont pas exercés.
- **Impact** : régression silencieuse possible sur un cog *sécurité* (avertit
  des utilisateurs). Le pattern d'autorisation est dupliqué de `captain_draft._is_admin`
  → un changement de liste de rôles dans l'un ne se reflète pas dans l'autre.
- **Correctif** : ajouter `tests/test_moderation.py` couvrant au minimum :
  - refus si non-membre, refus si pas le rôle, refus si self/bot,
  - happy path (DM envoyé + persistance),
  - `Forbidden` sur DM → warn persisté + message « DM fermés »,
  - `HTTPException` sur DM → l'erreur courante avorte la persistance (cf. finding ci-dessous),
  - `warn-list` filtre `member_id`, page size 10, embed vide.
- **Référence** : `~/.claude/rules/common/testing.md` (80 % minimum)

### [MINEUR] `cogs/moderation.py:warn` perd le warn sur `HTTPException` transitoire

- **Localisation** : `cogs/moderation.py:108-127`
- **Catégorie** : robustesse
- **Problème** :
  ```python
  except discord.HTTPException:
      logger.exception("[warn] echec envoi DM a %s", member.id)
      await interaction.response.send_message(...)
      return  # ← sort AVANT le repository.add_warn
  ```
  Un blip réseau ou un 5xx transitoire de Discord sur le DM annule
  *aussi* la persistance MongoDB. Le modo voit « échec DM », mais aucune
  trace du warn — alors qu'il a légitimement décidé de l'émettre.
- **Impact** : perte d'historique de modération pour erreurs transitoires.
  Asymétrique avec `Forbidden` (DMs fermés) qui, lui, persiste correctement.
- **Correctif** : aligner sur le pattern `Forbidden` — logger l'échec DM,
  persister quand même, puis répondre `⚠️ Warn enregistré mais erreur DM`.
- **Référence** : CWE-754 (improper handling of exceptional conditions)

### [MINEUR] `cogs/moderation.py:32` faute d'orthographe utilisateur-visible

- **Localisation** : `cogs/moderation.py:32`
- **Catégorie** : lisibilité
- **Problème** : `"Vous venez de recevoir un warn, au prochain, vous serez sanctionner."`
  — « sanctionner » → « sanctionné » (participe passé). Message envoyé en DM aux warnés.
- **Correctif** : remplacer par `"…vous serez sanctionné."`.

### [MINEUR] `cogs/match.py:42` import inutilisé

- **Localisation** : `cogs/match.py:42`
- **Catégorie** : dette
- **Problème** : `_revoke_queue_role` importé depuis `cogs.queue_v2` mais jamais
  utilisé (ruff F401). Confirmé par grep manuel : seule occurrence = la ligne d'import.
- **Correctif** : `ruff check --fix` (auto-corrigible). Ou retirer la ligne.

### [MINEUR] `ruff format` : 42 fichiers non conformes

- **Localisation** : 42 fichiers sur 45
- **Catégorie** : lisibilité
- **Problème** : `ruff format --check .` propose des changements sur quasi
  tout le repo (alignement de colonnes, espaces autour de `:`, etc.). La config
  `[tool.ruff.format] quote-style = "double"` est en place mais le format
  n'a jamais été appliqué globalement.
- **Impact** : diffs bruyants lors des futurs PRs.
- **Correctif** : un commit isolé `style: apply ruff format`. À faire en
  une fois pour pouvoir blamer-skip ensuite.

### [MINEUR] mypy : 7 erreurs résiduelles

- **Localisation** :
  - `services/captain_draft.py:294, 304, 307, 317` (var-annotated, method-assign)
  - `services/captain_draft.py:377, 401` (`union-attr` sur `self.message: Any | None`)
  - `scripts/migrate_shared_collections.py:35` (var-annotated `client`)
- **Catégorie** : typage
- **Problème** : la config mypy est volontairement laxiste (commentaire dans
  `pyproject.toml`), mais ces 7 erreurs restantes sont triviales à fixer
  (annoter `select: discord.ui.Select[Any]`, idem pour `cancel_btn` et `client: MongoClient[dict]`,
  garde `if self.message is None` autour des `.edit`).
- **Correctif** : ajouter les annotations explicites + une garde sur
  `self.message` avant `edit`.

### [MINEUR] `requirements.txt` / `requirements-test.txt` : doublons & dérive

- **Localisation** : racine du repo
- **Catégorie** : dépendances
- **Problème** : `pymongo`, `pillow`, `requests`, `discord.py` apparaissent
  dans les deux fichiers (avec contraintes parfois divergentes :
  `discord.py==2.3.2` dans le runtime, `discord.py>=2.3` dans les tests).
  Un `pip install -r requirements-test.txt` peut faire upgrader `discord.py`
  vers une version non testée en prod.
- **Correctif** : faire `requirements-test.txt` inclure `-r requirements.txt`
  + uniquement les paquets *test-only* (`mongomock`, `pytest`, `pytest-asyncio`,
  `dpytest`, `faker`).

### [MINEUR] `services/team_balancer.py:95` `assert best is not None`

- **Localisation** : `services/team_balancer.py:95`
- **Catégorie** : robustesse
- **Problème** : `assert` supprimé en `-O` (B101). Si Python est lancé en
  mode optimisé, et que la boucle ne trouve aucun candidat (improbable mais
  ne se déduit pas trivialement du code), la fonction renverra `None` au
  lieu d'un `BalancedTeams`, et le type-hint `-> BalancedTeams` ment.
- **Impact** : faible (boucle de 126 combinaisons toujours non-vide pour 10 joueurs validés en amont), mais l'invariant doit être renforcé.
- **Correctif** : remplacer par `if best is None: raise RuntimeError("balancing impossible")`.
- **Référence** : bandit B101, CWE-703

### [MINEUR] `leaderboard_img.py:_fetch_avatar` : pas de garde Pillow

- **Localisation** : `leaderboard_img.py:152-170`
- **Catégorie** : sécurité (faible)
- **Problème** : `Image.open(BytesIO(resp.content))` sans `Image.MAX_IMAGE_PIXELS`
  custom ni cap de taille sur `resp.content`. Pillow accepte par défaut jusqu'à
  178 956 970 pixels. Source = URL d'avatar Discord (`member.display_avatar.url`),
  qui est CDN Discord donc *normalement* sûr, mais un Discord member control son
  upload.
- **Impact** : DoS RAM/CPU mineur via decompression bomb (PNG/GIF crafted).
- **Correctif** : `requests.get(url, timeout=5, stream=True)` + `len(resp.content) < 2_000_000` + `Image.MAX_IMAGE_PIXELS = 16_000_000` en module-level.
- **Référence** : CWE-409, [Pillow security advisory](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.MAX_IMAGE_PIXELS)

### [MINEUR] `cogs/queue_v2.py:412` expose `repr(e)` à l'utilisateur

- **Localisation** : `cogs/queue_v2.py:411-414`
- **Catégorie** : sécurité (faible)
- **Problème** :
  ```python
  user_msg = f"❌ Erreur lors de la formation du match : `{e}`. ..."
  ```
  Le texte brut d'une exception arbitraire est posté dans le salon public.
  Sur certaines exceptions (chemins d'erreur pymongo, traces internes),
  ça peut leaker noms de collections ou de hôtes.
- **Impact** : disclosure mineur.
- **Correctif** : log la stack avec `logger.exception`, n'envoyer qu'un
  message générique aux joueurs.
- **Référence** : CWE-209 (information exposure through error message)

### [MINEUR] ruff lint : 16 erreurs résiduelles

Détail :

| Fichier:ligne | Code | Note |
|---|---|---|
| `cogs/match.py:42` | F401 | unused import `_revoke_queue_role` (cf. finding dédié) |
| `scripts/migrate_shared_collections.py:38` | UP017 | `timezone.utc` → `datetime.UTC` |
| `services/captain_draft.py:19` | UP035 | `from typing import Sequence` → `collections.abc` |
| `services/captain_draft.py:75,97,136` | UP037 | quotes superflues dans annotations |
| `services/captain_draft.py:111,115` | RUF005 | concat `(*self.team_a, player)` |
| `services/captain_draft.py:332` | SIM102 | nested `if` à fusionner |
| `tests/test_captain_draft.py:70,108,197` | N802 | fonctions test `ABBAABBA` (acceptables : sémantique d'algo) |
| `tests/test_match_cog.py:580,618,708` | SIM105 | `try/except/pass` → `contextlib.suppress` |
| `tests/test_shared_collections.py:82` | UP017 | `timezone.utc` → `datetime.UTC` |

Toutes auto-fixables sauf les `N802` qui sont volontaires (notation `A`/`B`
pour les snake-draft) — ajouter une exception ciblée dans `per-file-ignores`
ou un `# noqa: N802` est plus propre que de renommer.

### [SUGGESTION] Bandit B311 (`random.choice`) — 5 occurrences

- **Localisation** : `cogs/admin.py:164,172` (coinflip, map-pick), `cogs/match.py:383` (rng MatchCog), `cogs/prefix_legacy.py:207`, `services/match_service.py:89`
- **Catégorie** : non-issue documentée
- **Problème** : `random` n'est pas crypto-secure. Bandit le flagge systématiquement.
- **Impact** : nul ici — usage purement ludique (pile/face, choix de map, RNG matchmaking).
- **Correctif** : `# nosec B311` inline si on veut faire taire bandit, ou
  laisser tel quel et ajouter `skips = ["B311"]` dans une éventuelle config bandit.

### [SUGGESTION] `cogs/match.py` : nombreuses fonctions ≥ CC 10

Au-delà du finding majeur sur le découpage, plusieurs méthodes valent un refactor isolé :

- `_handle_timeout` (C 18), `_fetch_henrik_multipliers` (C 14), `_move_players_to_match_vc` (C 13), `match_cancel` (C 13), `_process_role_cleanups_for_guild` (C 12), `_check_vote_timeouts_for_guild` (C 11), `_verify_match` (C 11).

Ce sont des candidats raisonnables à extraction en stratégie/objet d'état une
fois le découpage en sous-modules effectué (cf. finding majeur). Avant ce
découpage, leur extraction in-place crée surtout du bruit.

### [SUGGESTION] Aucune CI visible

- **Localisation** : pas de `.github/workflows`, pas de `.gitlab-ci.yml`, pas de pre-commit hook configuré (le `.pre-commit-config.yaml` n'existe pas).
- **Catégorie** : ops
- **Problème** : `ruff`, `mypy`, `pytest` ne sont pas joués automatiquement.
  Le déploiement passe par PM2 + rsync (cf. `ecosystem.config.js` et observations 2026-05-18). Les régressions ne sont attrapées que manuellement.
- **Correctif** : minimum un workflow GitHub Actions qui exécute `ruff check`,
  `ruff format --check`, `mypy`, et `pytest` sur Python 3.11.

---

## Axes sains (pour ne pas masquer le positif)

- **Sécurité dépendances** : `pip-audit` propre sur tous les paquets de prod.
- **Pas de secrets en dur** : `DISCORD_TOKEN`, `MONGO_URL`, `HENRIK_API_KEY` tous lus depuis l'env (`bot.py:38,39` ; `services/riot_api.py:139`). Fail-fast explicite si `MONGO_URL` manquant (`bot.py:50`).
- **Accès Mongo** : centralisé via `services/repository.py`, paramétré (pas de string-concat dans les filtres), atomicité préservée par `find_one_and_update` + CAS, idempotence des application d'ELO via `processed_matches`.
- **Concurrence async** : usage cohérent de `asyncio.to_thread` pour le pymongo bloquant ; `requests.Session` protégé par lock (`riot_api._session_lock`) car concurrent depuis plusieurs threads ; sémaphore `_guild_member_edit_sem(5)` pour éviter le rate-limit Discord per-guild ; `_bg_tasks` set + `done_callback` pour éviter le GC prématuré des `create_task`.
- **Circuit breaker Henrik** : implémenté correctement avec lock, threshold, fenêtre d'ouverture.
- **Logging** : configuré globalement avec split stdout/stderr selon niveau, formatter cohérent, niveau pilotable par `LOG_LEVEL`. `logger.exception` utilisé systématiquement dans les `except`.
- **Tests** : 330 cas, 84 % de couverture globale, fixtures `mongomock`/`dpytest` propres dans `conftest.py`, marquage `unit`/`integration`.
- **Code mort** : `vulture` à confiance ≥ 80 % ne signale rien.
