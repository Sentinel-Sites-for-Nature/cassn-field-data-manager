"""Tests for canonical deployment ID construction.

The ID must carry the *round's* last retrieval date, not the individual device's.
Every other identifier the app produces — the deployment folder name and every
renamed file — already uses the round's date, and deriving this one differently
silently disagreed with them for 22% of placements.
"""

from __future__ import annotations

import pytest

from cassn.lookups import (
    LookupSchemaError,
    build_deployment_rounds,
    canonical_deployment_id,
    canonical_deployment_ids,
    placement_key,
)


def placement(plot, device_type, end, *, site="Bodega", start="2026-01-05"):
    return {
        "site_short_name": site,
        "plot_number": str(plot),
        "device_type": device_type,
        "device_id": f"{device_type}{plot}",
        "device_record_id": f"rec{plot}{device_type}",
        "deployment_start_date": start,
        "deployment_end_date": end,
        "deployment_event_id": f"UC_{site}_{end.replace('-', '')}" if end else "",
        "deployment_id": "",
    }


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

def test_one_day_round_dates_every_device_the_same():
    rows = [placement(1, "ML", "2026-03-03"), placement(2, "BD", "2026-03-03")]
    ids = canonical_deployment_ids(rows)
    assert set(ids.values()) == {
        "UC_Bodega_plot1_ML_20260303",
        "UC_Bodega_plot2_BD_20260303",
    }


def test_devices_pulled_across_two_days_share_the_round_end_date():
    """The regression: plot 1 came back a day early, but it is the same visit."""
    early = placement(1, "BD", "2026-03-02")
    late = placement(2, "BD", "2026-03-03")
    ids = canonical_deployment_ids([early, late])

    assert ids[placement_key(early)] == "UC_Bodega_plot1_BD_20260303"
    assert ids[placement_key(late)] == "UC_Bodega_plot2_BD_20260303"


def test_the_id_date_always_equals_the_event_folder_date():
    """Both derive from the round's max end date, so they cannot drift apart."""
    rows = [
        placement(1, "ML", "2026-03-02"),
        placement(2, "ML", "2026-03-03"),
        placement(3, "BD", "2026-03-02"),
    ]
    events_by_site, _ = build_deployment_rounds(rows)
    event_date = events_by_site["Bodega"][0]["deployment_end"].replace("-", "")

    for deployment_id in canonical_deployment_ids(rows).values():
        assert deployment_id.endswith("_" + event_date)


def test_separate_visits_keep_separate_dates():
    """A later round is a different deployment and must not absorb the earlier."""
    march = placement(1, "BD", "2026-03-03")
    june = placement(1, "BD", "2026-06-18", start="2026-03-03")
    ids = canonical_deployment_ids([march, june])

    assert ids[placement_key(march)] == "UC_Bodega_plot1_BD_20260303"
    assert ids[placement_key(june)] == "UC_Bodega_plot1_BD_20260618"


def test_open_placements_get_no_id():
    open_row = placement(1, "BD", "")
    assert placement_key(open_row) not in canonical_deployment_ids([open_row])


def test_sites_are_dated_independently():
    """One reserve's late pickup must not redate another reserve's round."""
    bodega = placement(1, "BD", "2026-03-02", site="Bodega")
    quail = placement(1, "BD", "2026-03-05", site="QuailRidge")
    ids = canonical_deployment_ids([bodega, quail])

    assert ids[placement_key(bodega)] == "UC_Bodega_plot1_BD_20260302"
    assert ids[placement_key(quail)] == "UC_QuailRidge_plot1_BD_20260305"


# ---------------------------------------------------------------------------
# The single-row builder
# ---------------------------------------------------------------------------

def test_builder_uses_the_date_it_is_given_not_the_row_date():
    """The row was retrieved on the 13th; the round it belongs to ended on the 14th."""
    row = placement(2, "BD", "2026-07-13")
    assert canonical_deployment_id(row, "2026-07-14") == "UC_Bodega_plot2_BD_20260714"


def test_builder_returns_blank_without_a_date():
    assert canonical_deployment_id(placement(1, "BD", ""), "") == ""


def test_builder_rejects_an_invalid_plot_number():
    row = placement(1, "BD", "2026-03-03")
    row["plot_number"] = "not-a-number"
    with pytest.raises(LookupSchemaError):
        canonical_deployment_id(row, "2026-03-03")


def test_placement_key_ignores_the_assigned_id():
    a = placement(1, "BD", "2026-03-03")
    b = dict(a, deployment_id="anything-at-all")
    assert placement_key(a) == placement_key(b)
