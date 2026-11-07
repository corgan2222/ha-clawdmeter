"""Parsing for the usage endpoint's ``limits`` list.

The usage API used to expose one object per window at the top level
(``five_hour``, ``seven_day``, ``seven_day_sonnet``, ``seven_day_opus``). Those
keys still exist but are increasingly ``null``, and the same figures now arrive
as rows in a ``limits`` array that carries its own ``kind`` and ``scope``.

Three properties of that array drive the design here:

* ``kind`` and ``group`` are open strings, not enums. The server's own schema
  says a new meter needs no client release, so an unknown ``kind`` is data to
  pass along, never a parse error.
* ``is_active`` is the server's pick of the row a single-value display should
  show. It is not "this limit applies to the account", it is not guaranteed to
  be set on exactly one row, and it has been observed absent. Nothing here
  depends on it, so it is not parsed.
* ``weekly_scoped_shares[].limit_index`` indexes the **raw** array, including
  rows this module does not understand. Indices are therefore resolved before
  any filtering, which is why :attr:`LimitRow.index` exists.

The ``seven_day_breakdown`` object, which splits the week's usage by surface,
is parsed here too: it is the same foreign JSON and needs the same care.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.util import dt as dt_util

KIND_SESSION = "session"
KIND_WEEKLY_ALL = "weekly_all"
KIND_WEEKLY_SCOPED = "weekly_scoped"


@dataclass(frozen=True, slots=True)
class LimitRow:
    """One row of the ``limits`` array, kept close to the wire format."""

    index: int
    kind: str
    percent: float | None
    resets_at: datetime | None
    model: str | None
    surface: str | None

    @property
    def label(self) -> str | None:
        """Return the server's display label for a scoped row."""
        return self.model or self.surface

    @property
    def slug(self) -> str | None:
        """Return a stable key for the scoped row's model or surface.

        Only the first word is used and the case is dropped, because the server
        has shipped both bare and versioned names for the same model (``Fable``
        and ``Fable 5``). Matching the whole string would break on the next
        rename that only appends to it.
        """
        label = self.label
        if not label:
            return None
        first = label.split()[0].strip().lower()
        return first or None


def as_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` if it is not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_datetime(value: Any) -> datetime | None:
    """Parse an ISO instant into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _display_name(scope: Any, key: str) -> str | None:
    """Return ``scope[key].display_name`` when it is a usable string.

    ``scope.model.id`` is not read on purpose: it is absent from the client
    schema the server publishes and has been ``null`` in every payload seen so
    far, so the display name is the only identity a row actually carries.
    """
    if not isinstance(scope, dict):
        return None
    inner = scope.get(key)
    if not isinstance(inner, dict):
        return None
    name = inner.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def parse_limits(raw: Any) -> list[LimitRow]:
    """Return the ``limits`` array as rows, keeping their original positions.

    Rows that are not objects, or that carry no ``kind``, are dropped because
    nothing can be done with them, but they still consume their index so that
    ``limit_index`` keeps pointing at the right row.
    """
    rows: list[LimitRow] = []
    if not isinstance(raw, list):
        return rows
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        scope = item.get("scope")
        rows.append(
            LimitRow(
                index=index,
                kind=kind,
                percent=as_float(item.get("percent")),
                resets_at=as_datetime(item.get("resets_at")),
                model=_display_name(scope, "model"),
                surface=_display_name(scope, "surface"),
            )
        )
    return rows


def of_kind(rows: list[LimitRow], kind: str) -> LimitRow | None:
    """Return the first row of ``kind``, or ``None``."""
    return next((row for row in rows if row.kind == kind), None)


def scoped(rows: list[LimitRow]) -> list[LimitRow]:
    """Return the scoped rows that name what they are scoped to.

    A scoped row without a model or surface name is indistinguishable from the
    unscoped weekly row sitting next to it, so it is left out rather than shown
    as a nameless second bar.
    """
    return [row for row in rows if row.kind == KIND_WEEKLY_SCOPED and row.slug]


def scoped_for(rows: list[LimitRow], slug: str) -> LimitRow | None:
    """Return the scoped row whose model or surface matches ``slug``."""
    return next((row for row in scoped(rows) if row.slug == slug), None)


@dataclass(frozen=True, slots=True)
class Breakdown:
    """The week's usage split by surface, as shares that add up to 100."""

    shares: dict[str, float]
    window_started_at: datetime | None


def parse_breakdown(raw: Any) -> Breakdown | None:
    """Return ``seven_day_breakdown`` as a surface key to share map.

    ``key`` is an open string like ``kind`` above (``other`` is already a
    catch-all), so every key is passed along as it is. Rows without a key or a
    numeric ``percent`` are dropped. ``None`` means the account reported no
    usable breakdown at all.
    """
    if not isinstance(raw, dict):
        return None
    rows = raw.get("rows")
    if not isinstance(rows, list):
        return None
    shares: dict[str, float] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        percent = as_float(item.get("percent"))
        if isinstance(key, str) and key and percent is not None:
            shares[key] = percent
    if not shares:
        return None
    return Breakdown(
        shares=shares, window_started_at=as_datetime(raw.get("window_started_at"))
    )


def allowances(raw: Any, rows: list[LimitRow]) -> dict[int, float]:
    """Map a row index to the share of the weekly limit that row may use.

    ``limit_index`` counts positions in the raw array, so it shifts by one on an
    account whose ``session`` row is absent. Indices that miss, or that land on
    a row this module did not keep, are discarded rather than guessed at.
    """
    shares: dict[int, float] = {}
    if not isinstance(raw, list):
        return shares
    known = {row.index for row in rows}
    for item in raw:
        if not isinstance(item, dict):
            continue
        index = item.get("limit_index")
        allowance = as_float(item.get("allowance_percent_of_weekly"))
        if isinstance(index, int) and not isinstance(index, bool):
            if index in known and allowance is not None:
                shares[index] = allowance
    return shares
