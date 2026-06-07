"""Tests unitaires de services.elo_calc - logique pure."""

from services import elo_calc


# ── Constantes ────────────────────────────────────────────────────
def test_maps_list_not_empty():
    assert len(elo_calc.MAPS) >= 5
    assert "Ascent" in elo_calc.MAPS


def test_elo_start_is_2000():
    """Default starting ELO is 2000 (was 0). Players are seeded at 2000
    when they first appear in any queue."""
    from services.elo_calc import ELO_START

    assert ELO_START == 2000


# ── Pondération par Rating 2.0 (pro queue) ────────────────────────
def test_weight_constants():
    """P_MAX < base garantit la préservation du signe."""
    assert elo_calc.ELO_WEIGHT_P_MAX < elo_calc.ELO_BASE_CHANGE
    assert elo_calc.ELO_WEIGHT_C == 15
    assert elo_calc.ELO_WEIGHT_P_MAX == 6
    assert elo_calc.ELO_RATING_BASELINE == 1.0
    assert elo_calc.ELO_MIN_ROUNDS_FOR_WEIGHT == 6


def test_weighted_delta_average_player_is_flat():
    """Un joueur exactement à la baseline (1.0) prend le ±20 plat."""
    assert elo_calc.compute_weighted_delta(1.0, win=True) == 20
    assert elo_calc.compute_weighted_delta(1.0, win=False) == -20


def test_weighted_delta_carry_is_clamped():
    """Un carry (rating élevé) plafonne la perf à +6."""
    # 15 * (1.40 - 1.0) = 6.0 (au plafond)
    assert elo_calc.compute_weighted_delta(1.40, win=True) == 26
    assert elo_calc.compute_weighted_delta(1.40, win=False) == -14
    # Au-delà, toujours clampé à +6.
    assert elo_calc.compute_weighted_delta(2.00, win=True) == 26
    assert elo_calc.compute_weighted_delta(2.00, win=False) == -14


def test_weighted_delta_underperformer_is_clamped():
    """Un joueur en difficulté plafonne la perf à -6."""
    # 15 * (0.50 - 1.0) = -7.5 -> clampé à -6
    assert elo_calc.compute_weighted_delta(0.50, win=True) == 14
    assert elo_calc.compute_weighted_delta(0.50, win=False) == -26


def test_weighted_delta_mid_value_not_clamped():
    """Valeur intermédiaire : pente linéaire, sans plafond."""
    # 15 * (1.20 - 1.0) = 3.0
    assert elo_calc.compute_weighted_delta(1.20, win=True) == 23
    assert elo_calc.compute_weighted_delta(1.20, win=False) == -17


def test_weighted_delta_sign_always_preserved():
    """P_MAX < base => gagnant gagne toujours, perdant perd toujours."""
    for r in (0.0, 0.3, 0.5, 0.85, 1.0, 1.2, 1.5, 2.5):
        assert elo_calc.compute_weighted_delta(r, win=True) > 0
        assert elo_calc.compute_weighted_delta(r, win=False) < 0


def test_weighted_delta_custom_baseline():
    """La baseline est ajustable (anti-inflation si la moyenne dérive)."""
    # baseline 1.1 : un joueur à 1.1 est désormais 'moyen' -> ±20 plat.
    assert elo_calc.compute_weighted_delta(1.1, win=True, baseline=1.1) == 20
    assert elo_calc.compute_weighted_delta(1.1, win=False, baseline=1.1) == -20
