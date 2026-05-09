# Design — Système 3 queues (Pro / Open / GC)

**Date :** 2026-05-09
**Auteur :** brainstorming session
**Status :** Spec validée, prête pour le plan d'implémentation

---

## 1. Contexte & objectif

Le bot The Hub gère aujourd'hui **une seule queue 10mans** par serveur Discord, avec un classement ELO unique. La nouvelle exigence est de scinder la queue en **trois files distinctes** avec leurs propres ELO, leaderboards, et règles d'accès :

- **Pro Queue** — réservée aux joueurs ayant le rôle `Rank S | Pro Queue`. Calcul ELO simplifié (flat ±16, pas de pondération API Henrik).
- **Open Queue** — accessible à tous (équivalent fonctionnel de la queue actuelle). Calcul ELO actuel (Henrik ACS multipliers).
- **GC Queue** — réservée aux joueurs ayant le rôle `GC`. Calcul ELO actuel (Henrik ACS multipliers).

Chaque queue est isolée : 3 ELO indépendants par joueur, 3 leaderboards, 3 historiques de matchs. **Un joueur ne peut être que dans une seule queue à la fois** (pas de présence simultanée).

---

## 2. Décisions de design

### 2.1 Storage : un champ `queue_type` partagé dans les collections

`queue_type ∈ {"pro", "open", "gc"}` est ajouté comme dimension dans les `_id` MongoDB des collections existantes (pas de nouvelles collections, pas de duplication).

| Collection | Avant | Après |
|---|---|---|
| `elo_<guild>` | `_id: "<user_id>"` | `_id: "<user_id>:<queue_type>"` + champ `queue_type` |
| `queue_<guild>` | `_id: "active"` | `_id: "active:<queue_type>"` + champ `queue_type` |
| `matches_<guild>` | doc match standard | + champ `queue_type` |
| `leaderboard_state_<guild>` | `_id: "current"` | `_id: "current:<queue_type>"` |

**Pourquoi** : compound `_id` (string) est trivial à indexer, gardable lisible, atomic pour les CAS existants, et évite de déclarer un index unique sur `(user_id, queue_type)` (MongoDB le fait gratuitement sur `_id`).

L'helper centralisateur :
```python
def _player_id(user_id: int | str, queue_type: str) -> str:
    return f"{user_id}:{queue_type}"
```

### 2.2 ELO de départ et fin du seeding `/link-riot`

- **`ELO_START` passe de 0 à 2000** dans `services/elo_calc.py`. Tout `find_one_and_update` upsert d'un joueur dans une queue initialise `elo: 2000`, `wins: 0`, `losses: 0`.
- **`/link-riot` ne seed plus l'ELO**. La fonction `seed_elo_with_riot_base` n'est plus appelée. Le rôle de `link-riot` se réduit à enregistrer le PUUID pour permettre les checks Henrik post-match.
- La constante `LINK_BASE_ELO` est supprimée. La fonction `seed_elo_with_riot_base` peut être conservée (comportement future-proof) mais devient orpheline ; à supprimer dans un commit ultérieur.

### 2.3 Salons & rôles Discord

**Salons texte** créés/mis à jour par `/setup` :
- `pro-queue` — héberge le message persistant Rejoindre/Quitter de la Pro Queue
- `open-queue` — idem Open Queue
- `gc-queue` — idem GC Queue
- `leaderboard` — héberge **3 messages auto-refreshés** (un par queue), persistés via 3 `_id` distincts dans `leaderboard_state_<guild>`
- `matchs` — inchangé (annonces de match)

**Salons vocaux** (déjà créés manuellement par l'admin sur le serveur, pas via `/setup` — le bot vérifie juste leur présence) :
- `Waiting Room Pro`
- `Waiting Room Open`
- `Waiting Room GC`

L'auto-move à la jointure cible le vocal correspondant au `queue_type` (pas le générique `Waiting Room` actuel).

**Catégories de match** : `Match #1`, `Match #2`, `Match #3` restent **partagées** entre les 3 queues. Max 3 matchs simultanés tous types confondus. Le bot pioche la première catégorie libre indépendamment du type de queue qui a déclenché la formation.

**Rôles** :
- `Rank S | Pro Queue` (existant ou à créer côté serveur) — gate Pro Queue
- `GC` (existant ou à créer côté serveur) — gate GC Queue
- `En Queue` (existant) — reste un marqueur global "je suis dans une queue", peu importe laquelle. Une seule queue à la fois => le rôle ne se cumule pas.
- `Match #1/2/3` (existant) — restent globaux

### 2.4 Logique de jointure (`Rejoindre`)

Le handler `join_btn` exécute dans cet ordre :
1. **Compte Riot lié** — refus sinon (existant)
2. **Pas dans un match en cours** (rôle `Match #N`) — refus sinon (existant)
3. **Pas déjà dans une autre queue** [nouveau] — scan des 3 docs `queue_<guild>` `_id="active:*"` ; si l'`user_id` est présent dans `players` de l'une d'entre elles (autre que celle ciblée), refus
4. **Gate de rôle selon `queue_type`** [nouveau] :
   - `pro` → vérifier rôle `Rank S | Pro Queue` ; refus sinon
   - `gc` → vérifier rôle `GC` ; refus sinon
   - `open` → pas de check
5. **Insertion atomique** dans la bonne queue (existant, paramétrisé sur `queue_type`)
6. Si la queue atteint 10 → `_on_queue_full(inter, queue_doc, queue_type)` (signature étendue)

### 2.5 Logique ELO

**Pro Queue** : `services/elo_updater.apply_match_validation` court-circuite la pondération Henrik si `match_doc["queue_type"] == "pro"`. Application flat **+16 / -16** à tous les joueurs, sans appel à `find_henrik_custom_match` ni `compute_acs_multipliers`. Le code path Henrik (`_verify_match` dans `cogs/match.py`) skippe complètement les matchs Pro Queue (économie d'API + pas de risque de fallback bizarre sur du flat de toute façon prévu).

**Open & GC Queue** : flow existant inchangé (Henrik ACS multipliers + fallback flat à 30 min si Henrik introuvable).

**Plancher 0 ELO** : maintenu pour les 3 queues (le `clamp_loser_deltas` reste actif).

### 2.6 Auto-refresh ciblé du leaderboard

Le `#leaderboard` héberge 3 messages indépendants. Chaque modification d'ELO **connaît son `queue_type`** et déclenche le refresh **de ce seul message**.

**API** :
```python
async def refresh_leaderboard_channel(
    guild: discord.Guild, db, bot_user_id: int, queue_type: str,
) -> None
```

Le helper `_refresh_leaderboard_safe(guild, queue_type)` dans `bot.py` encapsule l'appel et le try/except. Tout caller (slash command, validation match, reset) le passe avec son `queue_type`.

**Sources de modification ELO et leur source de `queue_type`** :

| Source | D'où vient `queue_type` |
|---|---|
| Validation match (Henrik OK ou timeout) | `match_doc["queue_type"]` |
| `/win`, `/lose` | param slash `queue:` |
| `/elomodify`, `/winmodify`, `/losemodify` | param slash `queue:` |
| `/resetelo` | param slash `queue:` |
| `/reset-queue` | param slash `queue:` |

**Debounce per-(guild, queue_type)** : `_LAST_REFRESH_AT: dict[tuple[int, str], datetime]`. Un `/win` Pro qui regénère le LB Pro ne bloque pas un `/win` Open de regénérer le LB Open simultanément.

**Récupération du message id** : `repository.get_leaderboard_message_id(db, guild_id, queue_type)` lit `leaderboard_state_<guild>` avec `_id="current:<queue_type>"`. Idem `set_leaderboard_message_id`, `clear_leaderboard_message_id`.

**Pré-post au `/setup`** : à la fin de `/setup`, après création des salons et pose des 3 messages de queue, on appelle `_refresh_leaderboard_safe(guild, qt)` pour `qt ∈ {pro, open, gc}` afin que les 3 messages de leaderboard soient présents immédiatement (vides à 0 joueur, ce qui est OK : l'image n'est pas générée si aucun joueur, mais on poste un message placeholder ou on skip — choix d'implémentation à trancher au moment du build).

### 2.7 Commandes (paramètre `queue` ajouté)

Toutes les commandes ELO/leaderboard/queue prennent un paramètre `queue` choix `pro|open|gc`. `/setup` reste global.

| Commande | Signature | Notes |
|---|---|---|
| `/setup` | inchangée | Crée catégorie + salons + pose les 3 messages queue + pré-post les 3 leaderboards |
| `/setup-queue` | `queue:<pro\|open\|gc>` | Repose le message queue dans le salon courant |
| `/close-queue` | `queue:<pro\|open\|gc>` | Drop la queue active de ce type |
| `/leaderboard` | `queue:<pro\|open\|gc>` | Affiche le LB ; ephemeral hors `#leaderboard` |
| `/win` | `queue:<...>` `joueur1..5` | Applique gain selon les règles de la queue (flat ou pondéré) |
| `/lose` | `queue:<...>` `joueur1..5` | idem |
| `/elomodify` | `queue:<...>` `joueur` `action` `montant` | |
| `/winmodify` | `queue:<...>` `joueur` `action` `montant` | |
| `/losemodify` | `queue:<...>` `joueur` `action` `montant` | |
| `/resetelo` | `queue:<...>` (+`joueur` ou `all:True`) | Reset ELO d'un joueur ou de tous, **dans cette queue uniquement** |
| `/stats` | `queue:<...>` `joueur` | Affiche stats du joueur dans cette queue |
| **`/reset-queue`** [nouveau] | `queue:<pro\|open\|gc>` | Drop complet : `elo_<guild>` (queue_type=type), queue active, matches, leaderboard_state. Confirmation interactive (bouton). |

### 2.8 Reset des données existantes

**Pas de wipe automatique au déploiement.** Le `/setup` ne touche pas aux données. La nouvelle commande `/reset-queue queue:<pro|open|gc>` est utilisée par l'admin pour repartir à zéro sur une queue (ou les 3 successivement).

**Workflow `/reset-queue`** :
1. Admin lance la commande
2. Réponse éphémère : embed de confirmation + bouton "Confirmer le reset" (timeout 30s)
3. Au clic :
   - Drop tous les docs `elo_<guild>` matching `{queue_type: <type>}`
   - Delete le doc `queue_<guild>` `_id="active:<type>"` puis re-pose un nouveau message vide dans le bon salon
   - Drop tous les `matches_<guild>` matching `{queue_type: <type>}`
   - Delete `leaderboard_state_<guild>` `_id="current:<type>"`
   - Refresh le leaderboard de cette queue (qui devient vide ou placeholder)
4. Audit log : embed récapitulatif dans le salon courant (qui a reset, quand, quelle queue)

**Migration des données existantes (anciens elo_<guild>) :** elles deviennent orphelines (anciens `_id="<user_id>"` non préfixés). Au premier accès post-déploiement, le code n'est compatible qu'avec les nouveaux `_id="<user_id>:<queue_type>"`. **Recommandation : drop manuel des collections `elo_<guild>`, `queue_<guild>`, `matches_<guild>`, `leaderboard_state_<guild>` au déploiement** (script one-shot ou via mongo shell). Le `/reset-queue` ne couvre pas ce cas car il filtre sur `queue_type` (champ absent dans les anciens docs).

À documenter dans le runbook de déploiement.

---

## 3. Découpage technique

### 3.1 `services/repository.py`

Toutes les fonctions ELO/queue/match/leaderboard prennent `queue_type: str` et propagent au compound `_id` ou au champ.

Helpers ajoutés :
```python
def _player_id(user_id, queue_type) -> str: ...
def _queue_id(queue_type) -> str: ...        # "active:<type>"
def _leaderboard_state_id(queue_type) -> str: ...  # "current:<type>"
```

Fonctions touchées (signature étendue) :
- `get_or_create_player(col, user_id, queue_type, display_name, initial_elo=2000)`
- `get_active_queue(db, guild_id, queue_type)`
- `setup_active_queue(db, guild_id, queue_type, channel_id, message_id)`
- `delete_active_queue(db, guild_id, queue_type) -> bool`
- `close_active_queue(db, guild_id, queue_type)`
- `add_player_to_queue(db, guild_id, queue_type, user_id, *, max_size=10)`
- `remove_player_from_queue(db, guild_id, queue_type, user_id)`
- **Nouveau** `find_player_in_any_queue(db, guild_id, user_id) -> str | None` — renvoie le `queue_type` où le joueur est présent, ou None
- `create_match(...)` reçoit `queue_type` et le persiste dans le doc
- `get_match`, `get_match_by_message`, `add_match_vote`, etc. : pas changés (lecture par `_id` ObjectId), mais `find_validated_unverified` filtre **côté caller** (skip Pro Queue dans le scanner Henrik)
- `get_leaderboard_message_id(db, guild_id, queue_type)`, `set_leaderboard_message_id`, `clear_leaderboard_message_id`
- `seed_elo_with_riot_base` — non appelée, kept-for-now ou supprimée

Fonctions inchangées : `get_elo_col`, `get_queue_col`, `get_matches_col`, `get_leaderboard_state_col` (collections par-guild, type non encodé dans le nom de collection).

### 3.2 `cogs/queue_v2.py`

`QueueCog.__init__` instancie **3 `QueueView`**, une par `queue_type`, avec custom_ids distincts :
- `queue_v2:join:pro` / `queue_v2:leave:pro`
- `queue_v2:join:open` / `queue_v2:leave:open`
- `queue_v2:join:gc` / `queue_v2:leave:gc`

`QueueView.__init__` accepte `queue_type` et le mémorise. Tous les checks (rôle, single-queue, repository calls) utilisent ce champ.

`build_queue_embed(queue_doc, guild, queue_type)` ajoute le type dans le titre (`"🎯 Pro Queue 10mans — 3/10"`).

`_grant_queue_role` reste générique (`En Queue`), car le rôle est unique global.

`_move_to_waiting_room(member, queue_type)` cible le bon vocal :
```python
WAITING_ROOM_NAMES = {
    "pro":  "Waiting Room Pro",
    "open": "Waiting Room Open",
    "gc":   "Waiting Room GC",
}
```

Commandes :
```python
@app_commands.command(name="setup-queue")
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
async def setup_queue(self, interaction, queue: str): ...
```

`on_member_remove` retire le joueur de **toutes** les queues actives où il pourrait être (boucle 3 types).

### 3.3 `cogs/match.py`

`on_queue_full(self, inter, queue_doc, queue_type)` propage `queue_type` à `create_match`. Le `match_doc` est tagué.

`build_match_embed`, `build_match_embed_from_doc` ajoutent le type dans le footer ou le titre (`"🎯 Match Pro Queue trouvé !"`).

`_verify_match` (le scanner Henrik) **skip les matchs Pro Queue** : if `match_doc.get("queue_type") == "pro"`, applique directement `apply_match_validation` flat sans appel Henrik.

Le post-validation `refresh_leaderboard_channel(guild, db, bot_user_id, queue_type)` cible le bon LB.

Le re-post de la queue après formation cible le **salon de la bonne queue** (`pro-queue`/`open-queue`/`gc-queue`).

### 3.4 `services/elo_updater.py`

`apply_match_validation(db, guild_id, match_doc, multipliers=None)` lit `match_doc.get("queue_type", "open")` et :
- Si `pro` : force `multipliers = None` ET `base_gain = base_loss = 16`. `weighted = False`.
- Sinon : flow actuel.

Le compound `_id` dans `_apply_player` devient `f"{user_id}:{queue_type}"`. La fonction reçoit `queue_type` en paramètre.

### 3.5 `services/leaderboard_refresh.py`

Signature : `refresh_leaderboard_channel(guild, db, bot_user_id, queue_type)`.

`build_leaderboard_payload(guild, db, queue_type, ...)` filtre les docs par `queue_type` :
```python
docs = list(col.find({"queue_type": queue_type}).sort([...]))
```

Le titre du leaderboard image inclut le type (`"Leaderboard Pro Queue"`).

`_LAST_REFRESH_AT: OrderedDict[tuple[int, str], datetime]`.

### 3.6 `bot.py`

- `ELO_START = 2000` (importé depuis elo_calc)
- Toutes les commandes ELO/leaderboard reçoivent un param `queue` avec choix
- `get_elo_col` reste, mais les caller doivent passer le compound `_id` aux find/update
- Helper `get_player_v3(col, member, queue_type)` pour upsert au lieu de `get_player(col, member)`
- `_match_elo_for_member(guild_id, user_id, queue_type)` étendu
- `/setup` : crée les 3 salons queue + le salon leaderboard + pose les 3 messages queue + pré-post les 3 LB
- `/reset-queue` : nouvelle commande
- `_refresh_leaderboard_safe(guild, queue_type)` : signature étendue

`/welcome` : déjà à jour, pas de changement.

### 3.7 `cogs/riot_link.py`

Suppression du seeding ELO. La fonction de link enregistre uniquement la metadata Riot pour les vérifications Henrik. La constante `LINK_BASE_ELO` supprimée. Toute référence à `seed_elo_with_riot_base` dans le code est retirée.

### 3.8 Tests

- **`test_queue_v2.py`** : ajout de tests pour les 3 queues simultanées, le gate de rôle Pro/GC, le check single-queue, les custom_ids distincts.
- **`test_elo_updater.py`** : ajout d'un test pour la branche flat Pro Queue (multipliers ignorés, ±16 systématique).
- **`test_match_cog.py`** : `queue_type` propagé dans les match docs ; le scanner Henrik skip les Pro Queue.
- **`test_match_service.py`** : `build_players` reçoit `queue_type` pour aller chercher le bon ELO.
- **`test_pagination.py`** (leaderboard) : adapter aux 3 leaderboards isolés.
- Le coverage doit rester ≥ 80%.

---

## 4. Risques & edge cases

### 4.1 Migration des données existantes
Les anciens docs `elo_<guild>` ont `_id="<user_id>"` (sans suffixe `queue_type`). Après déploiement, ils sont **invisibles** au nouveau code (filtre par compound `_id`). À documenter dans le runbook : drop manuel des collections concernées avant ou immédiatement après le déploiement.

### 4.2 Rôles `Rank S | Pro Queue` et `GC` absents du serveur
Le check de rôle dans `join_btn` doit gérer le cas où le rôle n'existe pas (admin ne l'a pas créé). Comportement : refuser la jointure avec un message clair "le rôle X est introuvable, contactez un admin", **pas** un crash.

### 4.3 Race entre 2 jointures simultanées dans 2 queues
Le check single-queue (3 lookups dans `queue_<guild>`) n'est pas atomique. Un joueur pourrait théoriquement cliquer Rejoindre dans 2 queues quasi-simultanément. Mitigation : le `_lock(guild_id)` per-guild dans `QueueView` sérialise les jointures par guild — comme il y a une seule guild en pratique et que les 3 views partagent le même cog, le lock est suffisant si **les 3 views partagent le même `_locks` dict**. À implémenter : le `_locks` est un attribut du cog, pas de la view.

### 4.4 Catégories Match #N partagées : conflit de leader
Si Pro Queue et Open Queue forment chacune un match en parallèle, ils piochent les catégories `Match #1` et `Match #2` indépendamment. Le `find_free_match_category` doit rester atomique (race possible : deux formations simultanées scannent les catégories libres au même instant et choisissent la même). Le code actuel utilise un lock applicatif qui doit couvrir les 3 queue cogs — vérifier que `cogs/match.py` synchronise sur le bon scope.

### 4.5 `/win`, `/lose`, `/elomodify` etc. avec mauvais `queue_type`
Si un admin se trompe de queue dans `/elomodify`, l'ELO est appliqué dans la mauvaise queue. Pas de garde-fou côté bot (commande admin, on fait confiance). Ajout d'un message de confirmation dans la réponse (`"+50 ELO Pro Queue à @joueur"`) pour visibilité.

### 4.6 `/stats` sans param queue
Décision : `queue` est obligatoire (pas de default). Si l'utilisateur veut voir ses 3 ELO d'un coup, c'est un autre design qu'on ne couvre pas ici (potentiel ajout futur : `/stats-all` qui affiche les 3).

### 4.7 Leaderboard pré-post au `/setup` : message vide
Au tout premier `/setup` post-reset, aucun joueur n'est encore enregistré. `build_leaderboard_payload` retourne `(None, None)`. **Décision** : on ne pré-poste rien dans ce cas ; le premier message LB apparaît au premier événement modificateur d'ELO. Alternative à valider : poster un placeholder texte ("Leaderboard Pro Queue — aucun joueur") ; à trancher au build.

---

## 5. Plan de migration / rollout

1. **Pré-déploiement** : annoncer aux joueurs que les ELO sont reset
2. **Déploiement** :
   - Drop manuel via mongo shell : `db.elo_<guild>.drop()`, `db.queue_<guild>.drop()`, `db.matches_<guild>.drop()`, `db.leaderboard_state_<guild>.drop()`
   - Push du nouveau code, restart du bot
   - Admin lance `/setup` : crée les 3 salons queue, le salon LB, pose les 3 messages queue, pré-post les 3 LB (vides)
3. **Post-déploiement** : admin teste manuellement chaque queue (jointure, formation match, vote, validation, leaderboard refresh) avec un compte de test
4. **Communication** : mise à jour du `/welcome` embed (déjà à jour suite à V1.21) et annonce dans `#general`

---

## 6. Hors scope

- Cross-queue stats (un joueur veut voir ses 3 ELO d'un coup) → pas dans cette spec
- Tournament / saison reset programmé → pas dans cette spec
- Migration des historiques de match anciens → ils sont droppés au reset
- Rôles dynamiques (auto-attribution `Rank S | Pro Queue` selon performance) → out of scope, les rôles sont gérés manuellement par les admins
