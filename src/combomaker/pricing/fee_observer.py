"""MEASURED maker-fee schedule — the fee coefficient is a layer-1 observation
from charged exchange fills, never a yaml number (2026-09-04 fee-seam repair).

WHY. Kalshi began charging our combo maker fills ``ceil(0.035 × C × P × (1−P))``
at 2026-08-20 05:07 ET. The bot's fee seam held a hand-set coefficient
(0.0175 — half the truth), a hand-set fee type, and a hand-set prefix list;
nothing watched the exchange, so every EV the bot judged stayed gross of a
fee that ate 44% of the modeled edge (547 fills / $106.41). A number a human
must move is a bug in the adaptation — so this module MEASURES it:

    exchange  the REPORTED fee on a fill is (540/540 charged fills, exact):
                fee = ceil_cc(coef · C · P · (1−P)) + (ceil_cc(C · P) − C · P)
              i.e. the exchange debits the position cost AND the fee each
              rounded UP to a centi-cent and reports the whole excess over the
              exact cost as "fee". The second term is pure cost rounding
              (< 1 cc, coefficient-free); the pin's slack absorbs it.
    fit    coef = Σ X·Y / Σ X²  (least squares through the origin) over charged
           maker fills, X = C·P·(1−P) in cc-per-unit-coefficient, Y = charged cc
    pin    EXACT, from the ceilings themselves: the exchange CEILs the fee and
           the parser CEILs the reported fee_cost (which carries the < 1 cc
           cost residue), so every charged fill i constrains
               coef · X_i ∈ (charged_i − 2, charged_i]      (``CEIL_SLACK_CC``)
           and the FEASIBLE SET is the intersection of those intervals. The
           coefficient is PINNED once exactly ONE multiple of the publication
           quantum (1e-4 — 0.07, 0.035, 0.0175 are all multiples) lies inside.
           ``pinning_count`` derives the fills needed FROM THE FILL DATA (one
           10-contract fill at 50c pins it alone; many small fills pin it
           together because their intervals interleave; no count is typed).
           An EMPTY intersection means two fee regimes are mixed — the newest
           fills that still agree form the current regime (``regime_window``).
    validate  |model − charged| ≤ 1 cc on every fill; misses are listed.
    drift  a refit that moves the pinned coefficient is an ERROR log
           (``fee_schedule_drift``) — alarm + refit, never a halt.

BOOTSTRAP (never a guessed zero, never a no-quote brick): with no charged
maker fill ever observed and no operator override, ``current()`` reports the
TAKER coefficient as the maker coefficient — the existing fail-safe convention
of ``FeeModel._pricing_coef`` (over-estimating a cost widens, never
under-prices). The first charged fill replaces it.

PERSISTENCE: ``to_json``/``from_json`` round-trip the charged observations,
the fit, the per-collection "maker fee observed" flags and the cached series
fee types, so a relight prices with the measured schedule before its first
REST poll (``data/fee_schedule_observed.json``).

FEE TYPE PER COMBO (``fee_type_for``), in precedence order:
    1. an explicit operator prefix override (FeeConfig.maker_fee_active_prefixes
       — kept only as an override; logged at install);
    2. a charged maker fee OBSERVED on the collection (sticky — exchange truth);
    3. the series' own ``fee_type`` (GET /series/{ticker}, cached here);
    4. the configured default.

This module is pure pricing-side arithmetic. It never reads a wire message —
the exchange row → ``FeeObservation`` parse lives in ``exchange/fills.py``
(conventions quarantine, tests/test_architecture.py).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from combomaker.core.money import CC_PER_DOLLAR
from combomaker.pricing.fees import FeeSchedule, FeeType

JsonDict = dict[str, Any]

# The resolution Kalshi publishes fee coefficients at (0.07, 0.0175, 0.035 are
# all whole multiples of 1e-4) — the quantum the fit is pinned to and the
# threshold a coefficient move must clear to count as drift.
COEF_QUANTUM = Fraction(1, 10_000)
# Two ceilings sit between coef·X and the parsed charged fee: the exchange
# ceils the fee itself to a centi-cent, and ``fee_cc_from_dollars_str`` ceils
# the reported fee_cost (which carries the < 1 cc position-cost residue —
# exact on 540/540 charged fills, tests/test_fee_observer.py). Each is
# strictly under one cc, so coef·X > charged − 2. A protocol fact, not a
# tolerance knob.
CEIL_SLACK_CC = 2

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FeeObservation:
    """One exchange fill as the fee model sees it: OUR contracts, OUR price,
    the fee the exchange CHARGED (cc), and whether it was a maker fill."""

    fill_id: str
    created_time: str          # ISO-8601 UTC, lexicographically ordered
    collection_prefix: str     # market ticker up to (excluding) the event segment
    contracts_centi: int
    price_cc: int
    fee_cc: int
    maker: bool

    @property
    def charged(self) -> bool:
        return self.fee_cc > 0

    def to_json(self) -> JsonDict:
        return {
            "fill_id": self.fill_id,
            "created_time": self.created_time,
            "collection_prefix": self.collection_prefix,
            "contracts_centi": self.contracts_centi,
            "price_cc": self.price_cc,
            "fee_cc": self.fee_cc,
            "maker": self.maker,
        }

    @classmethod
    def from_json(cls, raw: JsonDict) -> FeeObservation:
        return cls(
            fill_id=str(raw["fill_id"]),
            created_time=str(raw["created_time"]),
            collection_prefix=str(raw["collection_prefix"]),
            contracts_centi=int(raw["contracts_centi"]),
            price_cc=int(raw["price_cc"]),
            fee_cc=int(raw["fee_cc"]),
            maker=bool(raw["maker"]),
        )


@dataclass(frozen=True, slots=True)
class FeeMismatch:
    fill_id: str
    model_cc: int
    charged_cc: int

    @property
    def error_cc(self) -> int:
        return self.model_cc - self.charged_cc


def collection_prefix_of(market_ticker: str) -> str:
    """The collection blob a combo market ticker embeds, e.g.
    ``KXMVECROSSCATEGORY-SHARD1-S2026B8AC1F3D598-966EDD669C4`` →
    ``KXMVECROSSCATEGORY-SHARD1`` (the two trailing hyphen segments are the
    event id and the market id). A ticker with fewer segments is its own
    prefix — it never merges with another collection."""
    parts = market_ticker.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else market_ticker


def fee_base_cc(obs: FeeObservation) -> Fraction:
    """X = 10⁴ · C · P · (1−P): the charged fee in cc per unit coefficient."""
    contracts = Fraction(obs.contracts_centi, 100)
    p = Fraction(obs.price_cc, CC_PER_DOLLAR)
    return contracts * p * (1 - p) * CC_PER_DOLLAR


def model_fee_cc(coef: Fraction, obs: FeeObservation) -> int:
    """The exchange's rounding: the whole-fill fee CEILed to a centi-cent."""
    return math.ceil(coef * fee_base_cc(obs))


def quantise(coef: Fraction) -> Fraction:
    return Fraction(round(coef / COEF_QUANTUM)) * COEF_QUANTUM


def charged_maker(observations: Iterable[FeeObservation]) -> list[FeeObservation]:
    return [o for o in observations if o.maker and o.charged]


class _Feasible:
    """Running intersection of the per-fill coefficient intervals
    ``(lo_i, hi_i]`` with lo_i = (charged_i − CEIL_SLACK_CC) / X_i and
    hi_i = charged_i / X_i. ``empty`` once two fills contradict each other
    (two fee regimes in one window)."""

    __slots__ = ("empty", "hi", "lo")

    def __init__(self) -> None:
        self.lo: Fraction | None = None
        self.hi: Fraction | None = None
        self.empty = False

    def add(self, obs: FeeObservation) -> None:
        x = fee_base_cc(obs)
        if x <= 0 or self.empty:
            return
        lo_i = Fraction(obs.fee_cc - CEIL_SLACK_CC) / x
        hi_i = Fraction(obs.fee_cc) / x
        self.lo = lo_i if self.lo is None else max(self.lo, lo_i)
        self.hi = hi_i if self.hi is None else min(self.hi, hi_i)
        if self.hi <= self.lo:
            self.empty = True

    def multiples(self) -> list[Fraction]:
        if self.lo is None or self.hi is None or self.empty:
            return []
        return quantum_multiples(self.lo, self.hi)


def quantum_multiples(lo: Fraction, hi: Fraction) -> list[Fraction]:
    """The publication-quantum multiples strictly inside ``(lo, hi]``."""
    first = math.floor(lo / COEF_QUANTUM) + 1
    last = math.floor(hi / COEF_QUANTUM)
    return [Fraction(k) * COEF_QUANTUM for k in range(first, last + 1)]


def feasible_bounds(observations: Iterable[FeeObservation]) -> tuple[Fraction, Fraction] | None:
    """``(lo, hi)`` with lo < coef ≤ hi over the charged maker fills; None when
    there is no charged fill or the fills contradict each other."""
    f = _Feasible()
    for obs in charged_maker(observations):
        f.add(obs)
    if f.empty or f.lo is None or f.hi is None:
        return None
    return f.lo, f.hi


def pinning_count(observations: Sequence[FeeObservation]) -> int | None:
    """The number of charged maker fills, taken in the given order, at which
    the feasible set first contains EXACTLY ONE quantum multiple — i.e. the
    count at which the ceilings alone recover the exchange's coefficient.
    None when the fills never pin it (too few / too small) or contradict."""
    f = _Feasible()
    for n, obs in enumerate(observations, start=1):
        if not (obs.maker and obs.charged):
            continue
        f.add(obs)
        if f.empty:
            return None
        if len(f.multiples()) == 1:
            return n
    return None


def regime_window(observations: Iterable[FeeObservation]) -> list[FeeObservation]:
    """The NEWEST charged maker fills that share one feasible coefficient —
    the current fee regime. Walks backwards from the newest fill and stops
    at the first fill whose interval empties the intersection (the previous
    regime's boundary). Newest first."""
    charged = sorted(charged_maker(observations), key=lambda o: o.created_time, reverse=True)
    f = _Feasible()
    window: list[FeeObservation] = []
    for obs in charged:
        probe = _Feasible()
        probe.lo, probe.hi, probe.empty = f.lo, f.hi, f.empty
        probe.add(obs)
        if probe.empty:
            break
        f = probe
        window.append(obs)
    return window


def least_squares_coefficient(observations: Iterable[FeeObservation]) -> Fraction | None:
    """Σ X·Y / Σ X² over the charged maker fills (the unquantised point
    estimate — logged next to the pin as a cross-check)."""
    num = Fraction(0)
    den = Fraction(0)
    for obs in charged_maker(observations):
        x = fee_base_cc(obs)
        num += x * obs.fee_cc
        den += x * x
    return None if den == 0 else num / den


def fit_maker_coefficient(observations: Iterable[FeeObservation]) -> Fraction | None:
    """The exchange's maker coefficient, PINNED by the ceilings of the newest
    fee regime's charged maker fills (``regime_window`` → the single quantum
    multiple inside the feasible set). A coefficient change on the exchange
    shows up as the new value — validated against the whole history by
    ``validate`` (older-regime fills then appear as mismatches). None until
    the charged fills pin the quantum, and None (never a guess) if the
    feasible set holds no quantum multiple at all (the fee formula itself
    would then be wrong — e.g. a multiplier ≠ 1 — a mismatch alarm, not a
    coefficient)."""
    window = regime_window(observations)
    if not window:
        return None
    f = _Feasible()
    for obs in window:
        f.add(obs)
    multiples = f.multiples()
    if len(multiples) != 1:
        return None
    return multiples[0]


def validate(observations: Iterable[FeeObservation], coef: Fraction) -> list[FeeMismatch]:
    """Every charged maker fill whose modeled fee at ``coef`` is off by more
    than 1 cc (the exchange's own rounding tolerance) from the charged fee."""
    out: list[FeeMismatch] = []
    for obs in charged_maker(observations):
        model = model_fee_cc(coef, obs)
        if abs(model - obs.fee_cc) > 1:
            out.append(FeeMismatch(obs.fill_id, model, obs.fee_cc))
    return out


@dataclass(frozen=True, slots=True)
class FeeRefit:
    """What one ``ingest`` did: the coefficient before/after, whether it
    DRIFTED (moved by ≥ one quantum), how many new fills landed, and the
    mismatches the new coefficient leaves on the whole history."""

    previous: Fraction | None
    fitted: Fraction | None
    drifted: bool
    new_fills: int
    new_charged: int
    mismatches: tuple[FeeMismatch, ...]
    collections_newly_active: tuple[str, ...]
    # The unquantised least-squares point estimate over the current regime's
    # fills — the log-line cross-check of the pinned value (None = no fit).
    least_squares: Fraction | None = None


class ObservedFeeSchedule:
    """The live schedule every FeeModel reads (``FeeScheduleSource``).

    Mutable by design — one instance is shared by the pricer, the fill ledger
    and the waiver, and the observer refits it in place on the slow loop; the
    quote path only ever reads three attributes."""

    def __init__(
        self,
        *,
        taker_coef: Fraction,
        maker_coef_override: Fraction | None = None,
        override_prefixes: tuple[str, ...] = (),
    ) -> None:
        self._taker_coef = taker_coef
        self._override = maker_coef_override
        self._override_prefixes = tuple(override_prefixes)
        self._fitted: Fraction | None = None
        self._fitted_at: str | None = None
        self._observations: dict[str, FeeObservation] = {}
        self._last_fill_time: str | None = None
        self._collections_active: set[str] = set()
        self._series_fee_types: dict[str, str] = {}
        self._collection_series: dict[str, str] = {}
        self._mismatches: tuple[FeeMismatch, ...] = ()
        # Bumped on every change a reader might cache against.
        self.generation = 0

    # ------------------------------------------------------------ schedule
    def current(self) -> FeeSchedule:
        return FeeSchedule(taker_coef=self._taker_coef, maker_coef=self.maker_coef)

    @property
    def taker_coef(self) -> Fraction:
        return self._taker_coef

    @property
    def maker_coef(self) -> Fraction:
        """Fitted → override → TAKER (the fail-safe bootstrap)."""
        if self._fitted is not None:
            return self._fitted
        if self._override is not None:
            return self._override
        return self._taker_coef

    @property
    def maker_coef_source(self) -> str:
        if self._fitted is not None:
            return "observed"
        if self._override is not None:
            return "override"
        return "taker_fallback"

    @property
    def fitted(self) -> Fraction | None:
        return self._fitted

    @property
    def fitted_at(self) -> str | None:
        return self._fitted_at

    @property
    def n_fills(self) -> int:
        return len(self._observations)

    @property
    def n_charged(self) -> int:
        return len(charged_maker(self._observations.values()))

    @property
    def last_fill_time(self) -> str | None:
        return self._last_fill_time

    @property
    def mismatches(self) -> tuple[FeeMismatch, ...]:
        return self._mismatches

    @property
    def collections_active(self) -> frozenset[str]:
        return frozenset(self._collections_active)

    @property
    def override_prefixes(self) -> tuple[str, ...]:
        return self._override_prefixes

    def observations(self) -> list[FeeObservation]:
        return sorted(self._observations.values(), key=lambda o: o.created_time)

    # ------------------------------------------------------------- fee type
    def series_fee_type(self, series_ticker: str) -> str | None:
        return self._series_fee_types.get(series_ticker)

    def set_series_fee_type(self, series_ticker: str, fee_type: str) -> None:
        if self._series_fee_types.get(series_ticker) != fee_type:
            self._series_fee_types[series_ticker] = fee_type
            self.generation += 1

    def set_collection_series(self, collection: str, series_ticker: str) -> None:
        if self._collection_series.get(collection) != series_ticker:
            self._collection_series[collection] = series_ticker
            self.generation += 1

    def series_for(self, collection: str | None) -> str | None:
        return None if collection is None else self._collection_series.get(collection)

    def collections_needing_series(self, collections: Iterable[str]) -> list[str]:
        """Collections whose series fee type is not cached yet (the slow
        loop fetches these; the quote path never does)."""
        out: list[str] = []
        for coll in collections:
            series = self._collection_series.get(coll)
            if series is None or series not in self._series_fee_types:
                out.append(coll)
        return out

    def observed_active(self, combo_ticker: str | None, collection: str | None) -> bool:
        for prefix in self._collections_active:
            if combo_ticker and combo_ticker.startswith(prefix):
                return True
            if collection and collection.startswith(prefix):
                return True
        return False

    def override_active(self, combo_ticker: str | None, collection: str | None) -> bool:
        for prefix in self._override_prefixes:
            if combo_ticker and combo_ticker.startswith(prefix):
                return True
            if collection and collection.startswith(prefix):
                return True
        return False

    def fee_type_for(
        self,
        *,
        combo_ticker: str | None,
        collection: str | None,
        default: FeeType,
    ) -> FeeType:
        """The fee type OUR fill on this combo is charged under. See the
        module docstring for the precedence. A non-quadratic configured
        default (FLAT/UNKNOWN) passes through untouched — the FeeModel keeps
        raising FeeUnknownError on it (fail-closed, never a guess)."""
        if default is not FeeType.QUADRATIC and not default.charges_maker:
            return default
        if self.override_active(combo_ticker, collection):
            return FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
        if self.observed_active(combo_ticker, collection):
            return FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
        series = self.series_for(collection)
        if series is not None:
            raw = self._series_fee_types.get(series)
            if raw is not None:
                parsed = FeeType.parse(raw)
                if parsed is not FeeType.UNKNOWN:
                    return parsed
                # An unparseable series string is exchange truth we cannot
                # price: fail closed (the model raises on UNKNOWN).
                return FeeType.UNKNOWN
        return default

    # --------------------------------------------------------------- ingest
    def ingest(self, observations: Iterable[FeeObservation]) -> FeeRefit:
        """Absorb exchange fills (deduplicated by fill id), refit, and report
        the drift verdict. Pure bookkeeping — logging/persistence is the
        caller's (lifecycle slow loop)."""
        new_fills = 0
        new_charged = 0
        newly_active: list[str] = []
        for obs in observations:
            if obs.fill_id in self._observations:
                continue
            self._observations[obs.fill_id] = obs
            new_fills += 1
            if self._last_fill_time is None or obs.created_time > self._last_fill_time:
                self._last_fill_time = obs.created_time
            if obs.maker and obs.charged:
                new_charged += 1
                if obs.collection_prefix not in self._collections_active:
                    self._collections_active.add(obs.collection_prefix)
                    newly_active.append(obs.collection_prefix)
        previous = self._fitted
        fitted = fit_maker_coefficient(self._observations.values())
        least_squares = (
            least_squares_coefficient(regime_window(self._observations.values()))
            if fitted is not None
            else None
        )
        drifted = False
        if fitted is not None:
            drifted = previous is not None and abs(fitted - previous) >= COEF_QUANTUM
            if fitted != previous:
                self._fitted = fitted
                self._fitted_at = self._last_fill_time
            self._mismatches = tuple(validate(self._observations.values(), fitted))
        if new_fills or drifted or newly_active:
            self.generation += 1
        return FeeRefit(
            previous=previous,
            fitted=fitted,
            drifted=drifted,
            new_fills=new_fills,
            new_charged=new_charged,
            mismatches=self._mismatches,
            collections_newly_active=tuple(newly_active),
            least_squares=least_squares,
        )

    # ---------------------------------------------------------- persistence
    def to_json(self) -> JsonDict:
        return {
            "version": SCHEMA_VERSION,
            "taker_coef": str(Decimal(self._taker_coef.numerator) / self._taker_coef.denominator),
            "maker_coef_fitted": None if self._fitted is None else _frac_str(self._fitted),
            "maker_coef_source": self.maker_coef_source,
            "fitted_at": self._fitted_at,
            "n_fills": len(self._observations),
            "n_charged": self.n_charged,
            "last_fill_time": self._last_fill_time,
            "collections_active": sorted(self._collections_active),
            "series_fee_types": dict(sorted(self._series_fee_types.items())),
            "collection_series": dict(sorted(self._collection_series.items())),
            # Only the fills the fit needs (charged maker) are persisted; the
            # uncharged history is re-derivable and would only bloat the file.
            "observations": [o.to_json() for o in self.observations() if o.maker and o.charged],
        }

    @classmethod
    def from_json(
        cls,
        raw: JsonDict,
        *,
        taker_coef: Fraction,
        maker_coef_override: Fraction | None = None,
        override_prefixes: tuple[str, ...] = (),
    ) -> ObservedFeeSchedule:
        """Rebuild from ``to_json`` output. The taker coefficient and the
        operator overrides come from the LIVE config, never from the file —
        the file carries observations, not policy."""
        sched = cls(
            taker_coef=taker_coef,
            maker_coef_override=maker_coef_override,
            override_prefixes=override_prefixes,
        )
        for item in raw.get("observations") or []:
            obs = FeeObservation.from_json(item)
            sched._observations[obs.fill_id] = obs
        for coll in raw.get("collections_active") or []:
            sched._collections_active.add(str(coll))
        for series, fee_type in (raw.get("series_fee_types") or {}).items():
            sched._series_fee_types[str(series)] = str(fee_type)
        for coll, series in (raw.get("collection_series") or {}).items():
            sched._collection_series[str(coll)] = str(series)
        last = raw.get("last_fill_time")
        sched._last_fill_time = str(last) if last else None
        fitted = raw.get("maker_coef_fitted")
        if fitted:
            sched._fitted = Fraction(Decimal(str(fitted)))
            at = raw.get("fitted_at")
            sched._fitted_at = str(at) if at else None
        # Re-derive from the persisted observations so a hand-edited file
        # cannot carry a coefficient its own fills do not support.
        refit = fit_maker_coefficient(sched._observations.values())
        if refit is not None:
            sched._fitted = refit
            sched._mismatches = tuple(validate(sched._observations.values(), refit))
        return sched

    def save(self, path: str | os.PathLike[str]) -> None:
        """Atomic write (tmp + replace). Plain file I/O on plain data — safe
        under ``asyncio.to_thread``."""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=1, sort_keys=True)
        os.replace(tmp, path)

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        taker_coef: Fraction,
        maker_coef_override: Fraction | None = None,
        override_prefixes: tuple[str, ...] = (),
    ) -> ObservedFeeSchedule:
        """Load from disk; a missing or corrupt file is a COLD schedule
        (taker-conservative bootstrap), never an error at boot."""
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("fee schedule file root must be an object")
            return cls.from_json(
                raw,
                taker_coef=taker_coef,
                maker_coef_override=maker_coef_override,
                override_prefixes=override_prefixes,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return cls(
                taker_coef=taker_coef,
                maker_coef_override=maker_coef_override,
                override_prefixes=override_prefixes,
            )


def _frac_str(value: Fraction) -> str:
    """Decimal text of a coefficient at the publication quantum's precision
    (``0.0350``): the places are derived from ``COEF_QUANTUM`` itself."""
    places = len(str(COEF_QUANTUM.denominator)) - 1
    return format(Decimal(value.numerator) / Decimal(value.denominator), f".{places}f")


@dataclass(frozen=True, slots=True)
class FeeScheduleSummary:
    """Log-line view of the schedule (quote_app boot / observer refit)."""

    maker_coef: str
    source: str
    n_fills: int
    n_charged: int
    fitted_at: str | None
    collections_active: tuple[str, ...] = field(default_factory=tuple)
    mismatches: int = 0

    @classmethod
    def of(cls, sched: ObservedFeeSchedule) -> FeeScheduleSummary:
        return cls(
            maker_coef=_frac_str(sched.maker_coef),
            source=sched.maker_coef_source,
            n_fills=sched.n_fills,
            n_charged=sched.n_charged,
            fitted_at=sched.fitted_at,
            collections_active=tuple(sorted(sched.collections_active)),
            mismatches=len(sched.mismatches),
        )
