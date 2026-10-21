import numpy as np
import pytest

from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile

STEP_HOURS = 0.25
STEPS_PER_DAY = 96


def make_solar(rng, **overrides) -> SolarProfile:
    params = dict(
        peak_kw=5.0,
        step_hours=STEP_HOURS,
        steps_per_day=STEPS_PER_DAY,
        rng=rng,
        cloud_sigma=0.0,  # deterministic clear-sky unless a test opts in
    )
    params.update(overrides)
    return SolarProfile(**params)


def make_consumption(rng, **overrides) -> ConsumptionProfile:
    params = dict(
        step_hours=STEP_HOURS,
        steps_per_day=STEPS_PER_DAY,
        rng=rng,
        noise_sigma_kw=0.0,
    )
    params.update(overrides)
    return ConsumptionProfile(**params)


def test_solar_zero_at_night(rng):
    solar = make_solar(rng)
    midnight, three_am, ten_pm = 0, 12, 88
    for step in (midnight, three_am, ten_pm):
        assert solar.energy_at(step) == 0.0


def test_solar_positive_at_noon(rng):
    solar = make_solar(rng)
    noon = 48
    assert solar.energy_at(noon) > 0.0


def test_solar_peaks_at_solar_noon(rng):
    solar = make_solar(rng)
    energies = [solar.energy_at(step) for step in range(STEPS_PER_DAY)]
    # Sunrise 6h, sunset 20h -> solar noon at 13h = step 52
    assert energies.index(max(energies)) == 52
    # Clear-sky peak: 5 kW * 0.25 h = 1.25 kWh
    assert max(energies) == pytest.approx(5.0 * STEP_HOURS)


def test_solar_repeats_daily(rng):
    solar = make_solar(rng)
    for step in range(STEPS_PER_DAY):
        assert solar.energy_at(step) == pytest.approx(
            solar.energy_at(step + STEPS_PER_DAY)
        )


def test_solar_cloud_noise_reduces_output():
    rng = np.random.default_rng(1)
    cloudy = make_solar(rng, cloud_sigma=0.15)
    clear = make_solar(np.random.default_rng(1))
    noon = 48
    assert 0.0 < cloudy.energy_at(noon) <= clear.energy_at(noon)


def test_solar_same_seed_same_series():
    solar_a = make_solar(np.random.default_rng(7), cloud_sigma=0.2)
    solar_b = make_solar(np.random.default_rng(7), cloud_sigma=0.2)
    series_a = [solar_a.energy_at(s) for s in range(STEPS_PER_DAY)]
    series_b = [solar_b.energy_at(s) for s in range(STEPS_PER_DAY)]
    assert series_a == pytest.approx(series_b)


def test_consumption_always_positive(rng):
    profile = make_consumption(rng)
    for step in range(STEPS_PER_DAY):
        assert profile.energy_at(step) > 0.0


def test_consumption_daily_total_matches_turkish_average(rng):
    """Default household uses ~200 kWh/month, i.e. ~6.6 kWh per day."""
    profile = make_consumption(rng)
    daily_kwh = sum(profile.energy_at(s) for s in range(STEPS_PER_DAY))
    assert daily_kwh == pytest.approx(6.6, abs=0.3)


def test_consumption_evening_peak_dominates(rng):
    profile = make_consumption(rng)
    evening = profile.energy_at(80)  # 20:00
    night = profile.energy_at(12)  # 03:00
    assert evening > night


def test_consumption_scale(rng):
    small = make_consumption(rng, scale=0.7)
    large = make_consumption(rng, scale=1.3)
    step = 80
    assert large.energy_at(step) > small.energy_at(step)


def daily_kwh(profile, day: int) -> float:
    start = day * STEPS_PER_DAY
    return sum(profile.energy_at(s) for s in range(start, start + STEPS_PER_DAY))


def test_seasonal_solar_winter_below_summer(rng):
    summer = make_solar(rng, start_day_of_year=172)  # 21 June
    winter = make_solar(rng, start_day_of_year=355)  # 21 December
    assert daily_kwh(winter, 0) < 0.4 * daily_kwh(summer, 0)


def test_seasonal_solar_matches_monthly_table(rng):
    """Expected daily yield (clear sky / mean cloud) follows the Istanbul table."""
    from microgrid.profiles.solar import ISTANBUL_KWH_PER_KWP_DAY

    july = make_solar(rng, peak_kw=1.0, start_day_of_year=196)
    assert daily_kwh(july, 0) * 0.9 == pytest.approx(ISTANBUL_KWH_PER_KWP_DAY[6], rel=0.02)


def test_seasonal_solar_days_get_shorter_after_solstice(rng):
    solar = make_solar(rng, start_day_of_year=172)
    first_light = lambda day: next(
        s for s in range(day * STEPS_PER_DAY, (day + 1) * STEPS_PER_DAY)
        if solar.energy_at(s) > 0
    ) % STEPS_PER_DAY
    assert first_light(120) > first_light(0)  # October sunrise is later


def test_day_length_istanbul():
    from microgrid.profiles.solar import day_length_hours

    assert day_length_hours(172) == pytest.approx(15.0, abs=0.3)
    assert day_length_hours(355) == pytest.approx(9.1, abs=0.3)


def test_invalid_params_rejected(rng):
    with pytest.raises(ValueError):
        make_solar(rng, peak_kw=-1.0)
    with pytest.raises(ValueError):
        make_solar(rng, sunrise_hour=21.0, sunset_hour=6.0)
    with pytest.raises(ValueError):
        make_solar(rng, start_day_of_year=0)
    with pytest.raises(ValueError):
        make_consumption(rng, scale=0.0)
