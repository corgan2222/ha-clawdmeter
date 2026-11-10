"""Tests for the ``limits`` array parsing.

The rules checked here are the ones that are easy to get wrong and expensive to
get wrong: index arithmetic that shifts per account, a ``kind`` field that is an
open string, and a model identity that lives only in a display label.
"""

from custom_components.clawdmeter.api import plan_from_profile
from custom_components.clawdmeter.limits import (
    KIND_SESSION,
    KIND_WEEKLY_ALL,
    allowances,
    of_kind,
    parse_breakdown,
    parse_limits,
    scoped,
    scoped_for,
)


def _row(kind: str, percent: float, model: str | None = None, **extra) -> dict:
    """Build one limits row, scoped to a model when one is named."""
    row = {"kind": kind, "group": "weekly", "percent": percent, "scope": None}
    if model is not None:
        row["scope"] = {"model": {"id": None, "display_name": model}}
    row.update(extra)
    return row


def test_rows_keep_their_position_when_one_is_dropped() -> None:
    """Test an unusable row still consumes its index.

    ``weekly_scoped_shares`` points into the raw array, so a row this module
    throws away must not shift the ones behind it.
    """
    rows = parse_limits(
        [
            "not an object",
            _row(KIND_WEEKLY_ALL, 40),
            _row("weekly_scoped", 62, "Fable"),
        ]
    )

    assert [row.index for row in rows] == [1, 2]


def test_an_unknown_kind_survives() -> None:
    """Test a meter this release has never heard of is still parsed.

    The server's own schema calls these open strings and says a new meter needs
    no client release, so dropping unknown kinds would silently hide data.
    """
    rows = parse_limits([_row("monthly_all", 12)])

    assert len(rows) == 1
    assert rows[0].kind == "monthly_all"
    assert rows[0].percent == 12.0


def test_a_row_without_a_kind_is_dropped() -> None:
    """Test a row that cannot be classified is left out rather than guessed at."""
    assert parse_limits([{"percent": 5}, {"kind": "", "percent": 5}]) == []


def test_the_model_slug_ignores_a_version_suffix() -> None:
    """Test matching survives a model being renamed by appending to it.

    The same model has shipped as both a bare name and a versioned one, so an
    exact comparison would break on the next rename.
    """
    rows = parse_limits(
        [_row("weekly_scoped", 70, "Opus 4"), _row("weekly_scoped", 3, "FABLE")]
    )

    assert scoped_for(rows, "opus").percent == 70.0
    assert scoped_for(rows, "fable").percent == 3.0


def test_a_scoped_row_without_a_label_is_not_scoped() -> None:
    """Test a nameless scoped row is skipped.

    Nothing distinguishes it from the unscoped weekly row beside it, so showing
    it would mean inventing a second bar with no way to say what it is for.
    """
    rows = parse_limits(
        [
            _row("weekly_scoped", 9),
            {"kind": "weekly_scoped", "percent": 9, "scope": {"model": {"id": "x"}}},
        ]
    )

    assert scoped(rows) == []


def test_allowances_index_the_raw_array() -> None:
    """Test the share index is resolved before any filtering.

    It counts positions in the response, so the same scoped limit sits at a
    different index on an account whose session row is absent.
    """
    with_session = parse_limits(
        [
            _row(KIND_SESSION, 50),
            _row(KIND_WEEKLY_ALL, 40),
            _row("weekly_scoped", 62, "Fable"),
        ]
    )
    without_session = parse_limits(
        [_row(KIND_WEEKLY_ALL, 40), _row("weekly_scoped", 62, "Fable")]
    )

    assert allowances(
        [{"limit_index": 2, "allowance_percent_of_weekly": 50}], with_session
    ) == {2: 50.0}
    assert allowances(
        [{"limit_index": 1, "allowance_percent_of_weekly": 50}], without_session
    ) == {1: 50.0}


def test_an_allowance_pointing_nowhere_is_discarded() -> None:
    """Test a share whose index misses is dropped instead of being reassigned."""
    rows = parse_limits([_row(KIND_WEEKLY_ALL, 40)])

    assert (
        allowances([{"limit_index": 7, "allowance_percent_of_weekly": 50}], rows) == {}
    )


def test_of_kind_finds_the_named_meter() -> None:
    """Test the session and weekly rows are found by kind, not by position."""
    rows = parse_limits([_row(KIND_WEEKLY_ALL, 40), _row(KIND_SESSION, 50)])

    assert of_kind(rows, KIND_SESSION).percent == 50.0
    assert of_kind(rows, "nope") is None


def test_a_malformed_limits_value_is_not_an_error() -> None:
    """Test anything that is not a list of rows yields no rows."""
    assert parse_limits(None) == []
    assert parse_limits({"kind": "session"}) == []
    assert allowances(None, []) == {}


def test_the_breakdown_keeps_every_surface_key() -> None:
    """Test an unknown surface key is passed along like an unknown kind.

    ``other`` is already a catch-all, so the set of keys is expected to grow.
    """
    breakdown = parse_breakdown(
        {
            "window_started_at": "2026-11-05T14:00:00.125277+00:00",
            "rows": [
                {"key": "claude_code", "percent": 40},
                {"key": "cowork", "percent": 55.5},
                {"key": "glasses", "percent": "4.5"},
            ],
        }
    )

    assert breakdown.shares == {"claude_code": 40.0, "cowork": 55.5, "glasses": 4.5}
    assert breakdown.window_started_at.isoformat().startswith("2026-11-05T14:00:00")


def test_unusable_breakdown_rows_are_dropped() -> None:
    """Test a row without a key or a numeric share is left out."""
    breakdown = parse_breakdown(
        {
            "rows": [
                "not an object",
                {"percent": 10},
                {"key": "", "percent": 10},
                {"key": "chat", "percent": None},
                {"key": "chat_bool", "percent": True},
                {"key": "claude_code", "percent": 90},
            ]
        }
    )

    assert breakdown.shares == {"claude_code": 90.0}
    assert breakdown.window_started_at is None


def test_a_missing_breakdown_is_none() -> None:
    """Test an account without a usable breakdown reports none at all."""
    assert parse_breakdown(None) is None
    assert parse_breakdown({"rows": None}) is None
    assert parse_breakdown({"rows": [{"key": "chat", "percent": None}]}) is None
    assert parse_breakdown([{"key": "chat", "percent": 5}]) is None


def test_the_plan_display_name_wins_over_the_account_flags() -> None:
    """Test a renamed tier is reported under its current name.

    A plan that replaced an older tier keeps reporting the old flag, so the
    flags alone would show the superseded name indefinitely.
    """
    plan = plan_from_profile(
        {
            "account": {"has_claude_max": True},
            "organization": {"plan_display_name": "Ultra"},
        }
    )

    assert plan == "Ultra"


def test_the_account_flags_still_name_personal_plans() -> None:
    """Test nothing changes for a profile that only carries the flags."""
    assert plan_from_profile({"account": {"has_claude_max": True}}) == "Max"
    assert plan_from_profile({"account": {"has_claude_pro": True}}) == "Pro"


def test_an_organisation_type_names_a_seat_with_no_flag() -> None:
    """Test Team and Enterprise seats are recognised.

    Both leave every account flag false, so the organisation type is the only
    thing left to read the plan from.
    """
    body = {"account": {}, "organization": {"organization_type": "claude_team"}}

    assert plan_from_profile(body) == "Team"


def test_an_unrecognised_organisation_type_is_still_shown() -> None:
    """Test an unknown type is made readable rather than dropped."""
    body = {"organization": {"organization_type": "claude_foo_bar"}}

    assert plan_from_profile(body) == "Foo Bar"


def test_a_profile_with_nothing_to_go_on_yields_no_plan() -> None:
    """Test an empty profile does not invent a plan name."""
    assert plan_from_profile({}) is None
    assert plan_from_profile({"organization": None, "account": None}) is None
