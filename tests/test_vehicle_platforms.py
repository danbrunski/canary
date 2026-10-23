"""Vehicle platforms: data a whole generation shares (src/vehicle/platforms.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.paths import PLATFORMS_DIR
from src.vehicle.platforms import (
    PLATFORM_FILE,
    find_platform,
    load_platforms,
    vehicle_data_file,
)


def _platform(
    root: Path, pid: str, vehicles: list[dict[str, Any]], files: tuple[str, ...] = ()
) -> Path:
    directory = root / pid
    directory.mkdir(parents=True)
    (directory / PLATFORM_FILE).write_text(
        yaml.safe_dump(
            {"schema_version": 1, "name": pid.upper(), "vehicles": vehicles, "evidence": "test"}
        )
    )
    for name in files:
        (directory / name).write_text(f"{pid}\n")
    return directory


XTERRA = {"make": "Nissan", "model": "Xterra", "years": [2005, 2015]}


@pytest.mark.parametrize(
    ("make", "model", "year", "covered"),
    [
        ("Nissan", "Xterra", 2005, True),
        ("Nissan", "Xterra", 2015, True),
        (" nissan ", "XTERRA", 2013, True),
        ("Nissan", "Xterra", 2004, False),
        ("Nissan", "Xterra", 2016, False),
        ("Nissan", "Xterra", None, False),
        ("Nissan", "", 2013, False),
        ("Nissan", "Frontier", 2013, False),
    ],
)
def test_a_platform_covers_its_make_model_and_inclusive_years(
    tmp_path: Path, make: str, model: str, year: int | None, covered: bool
) -> None:
    _platform(tmp_path, "n50", [XTERRA])
    found = find_platform(make, model, year, platforms_dir=tmp_path)
    assert (found is not None and found.id == "n50") is covered


def test_the_profiles_own_file_overrides_its_platform(tmp_path: Path) -> None:
    platforms = tmp_path / "platforms"
    shared = _platform(platforms, "n50", [XTERRA], files=("rom_backup.yaml",))
    profile = tmp_path / "nissan-xterra-2013"
    profile.mkdir()
    kwargs: dict[str, Any] = {"make": "Nissan", "model": "Xterra", "year": 2013}

    found = vehicle_data_file(profile, "rom_backup.yaml", platforms_dir=platforms, **kwargs)
    assert found.path == shared / "rom_backup.yaml"
    assert found.platform is not None and found.platform.id == "n50"
    assert str(found).endswith("n50/rom_backup.yaml (platform N50)")

    (profile / "rom_backup.yaml").write_text("own\n")
    found = vehicle_data_file(profile, "rom_backup.yaml", platforms_dir=platforms, **kwargs)
    assert found.path == profile / "rom_backup.yaml" and found.platform is None


def test_no_file_anywhere_names_where_it_looked_and_what_it_matched(tmp_path: Path) -> None:
    platforms = tmp_path / "platforms"
    _platform(platforms, "n50", [XTERRA])
    profile = tmp_path / "nissan-2013"
    with pytest.raises(FileNotFoundError) as unmatched:
        vehicle_data_file(
            profile, "rom_backup.yaml", make="Nissan", model="", year=2013, platforms_dir=platforms
        )
    # The blank model is why nothing matched; the message says so.
    assert "nissan-2013/rom_backup.yaml does not exist, and no platform in" in str(unmatched.value)
    assert "make 'Nissan', model '(blank)', year 2013" in str(unmatched.value)
    with pytest.raises(FileNotFoundError, match="platform N50 has no rom_backup.yaml"):
        vehicle_data_file(
            profile,
            "rom_backup.yaml",
            make="Nissan",
            model="Xterra",
            year=2013,
            platforms_dir=platforms,
        )


def test_a_vehicle_two_platforms_cover_is_refused(tmp_path: Path) -> None:
    _platform(tmp_path, "a", [XTERRA])
    _platform(tmp_path, "b", [{**XTERRA, "years": [2013, 2013]}])
    with pytest.raises(ValueError, match="covered by more than one platform: a, b"):
        find_platform("Nissan", "Xterra", 2013, platforms_dir=tmp_path)
    assert find_platform("Nissan", "Xterra", 2012, platforms_dir=tmp_path).id == "a"  # type: ignore[union-attr]


def test_a_backwards_year_span_is_refused(tmp_path: Path) -> None:
    _platform(tmp_path, "bad", [{**XTERRA, "years": [2015, 2005]}])
    with pytest.raises(
        ValueError, match=r"(?s)bad/platform\.yaml is invalid: .*years 2015-2005 run"
    ):
        load_platforms(tmp_path)


def test_a_platform_file_that_is_not_yaml_is_named(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / PLATFORM_FILE).write_text("vehicles: [unclosed\n")
    with pytest.raises(ValueError, match=r"broken/platform\.yaml is invalid"):
        load_platforms(tmp_path)


def test_no_platforms_directory_is_no_platforms(tmp_path: Path) -> None:
    assert load_platforms(tmp_path / "absent") == []


def test_the_shipped_platforms_load_and_never_overlap() -> None:
    platforms = load_platforms(PLATFORMS_DIR)
    assert "nissan-xterra-n50" in {p.id for p in platforms}
    for platform in platforms:
        for vehicles in platform.definition.vehicles:
            for year in range(vehicles.years[0], vehicles.years[1] + 1):
                found = find_platform(vehicles.make, vehicles.model, year)
                assert found is not None and found.id == platform.id
