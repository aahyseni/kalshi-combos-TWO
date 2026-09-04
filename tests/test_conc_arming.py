"""LEVER #5 ARMING DISCIPLINE (2026-07-27 adversarial review, finding B3).

The finding: the concentration steer shipped ARMED. ``conc_profile`` was passed
unconditionally at ``rfq/lifecycle.py`` and a non-None ``conc`` REPLACED the
entire prior composition in ``risk/skew.compute_inventory_skew`` — no
``conc_armed`` flag, unlike ``pbook_armed`` and ``leg_axis_armed``, which both
shipped SHADOW first by explicit operator discipline (derive-before-arm), and
unlike the confirm-window rebuild in the same diff, which shipped two rollback
flags. Magnitude, off the build's own harness: median ``|applied_cc|`` 14.0 ->
140.0 (10x); diversifier->concentrator swing 229.1cc = 114.5% of the 200cc
margin.

The repair is the SAME seam the other two steers use:

    conc_enabled (default True)   compute + log the full decomposition
    conc_armed   (default False)  let it REPLACE the composition in skew_cc

and the load-bearing property — the one the operator's standing complaint is
about, that updates regress other things — is pinned here:

    WHILE UNARMED, EVERY PRICE-FACING FIELD IS BYTE-IDENTICAL TO THE
    COMPOSITION THAT SHIPS WITHOUT THE STEER WIRED AT ALL.

Proved over a candidate x book x params GRID rather than on one happy path, on
both branches of every arming flag, so no combination can quietly differ.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import random
import statistics
import subprocess

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.risk.concentration_steer import (
    SteerCenter,
    build_loss_event_book,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.skew import (
    ConcentrationProfile,
    InventorySkew,
    LegAxisProfile,
    PBookProfile,
    SkewLimits,
    SkewParams,
    compute_inventory_skew,
    ticket_bucket,
)

REPO = pathlib.Path(__file__).resolve().parents[1]

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
LIMITS = SkewLimits(
    max_event_delta_contracts=500.0,
    max_event_worst_case_loss_dollars=100.0,
    max_event_gross_notional_dollars=500.0,
)
GEN = 7
TICK_CC = 10
MARGIN_CC = 200            # the measured MEDIAN live margin
BANKROLL_CC = 21_800_000   # measured live equity $2,179.74
GAME_WALL_CC = 0.08 * BANKROLL_CC
ENTITY_WALL_CC = 0.04 * BANKROLL_CC
FILL_ELASTICITY = 0.22     # the measured CMH-stratified elasticity

# THE SHIPPED SHAPE: everything the live YAML arms, with the new steer in its
# SHIPPED (shadow) state. This is what the operator restart will run.
SHIPPED = SkewParams(enabled=True, pbook_armed=True, leg_axis_armed=True)
ARMED = dataclasses.replace(SHIPPED, conc_armed=True)
OFF = dataclasses.replace(SHIPPED, conc_enabled=False)

SERIES = ["KXMLBKS", "KXMLBGAME", "KXMLBHR", "KXMLBTOTAL", "KXMLBRFI"]
GAMES = [f"26JUL2716{i:02d}TEAM{i}" for i in range(12)]
PLAYERS = [f"PL{i}" for i in range(20)]


def _leg(rng: random.Random) -> LegRef:
    s = rng.choice(SERIES)
    g = rng.choice(GAMES)
    p = rng.choice(PLAYERS)
    return LegRef(
        market_ticker=f"{s}-{g}-{p}-{rng.randint(1, 9)}",
        event_ticker=f"{s}-{g}",
        side=rng.choice(["yes", "no"]),
    )


def _ticket(
    rng: random.Random, pid: str, premium_cc: int, scale: float = 1.0
) -> OpenPosition:
    n = rng.choice([2, 2, 3, 3, 3, 4])
    legs = tuple(_leg(rng) for _ in range(n))
    price = rng.randint(1_500, 8_000)
    contracts = max(100, int(premium_cc * 100 / price))
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(int(round(contracts * scale))),
        entry_price_cc=CentiCents(price),
        legs=legs,
    )


def build_world(seed: int = 17, *, scale: float = 1.0):
    """The measured live book shape: ~38 tickets / ~$411 premium, composition
    FIXED, dollar size scalable by ``scale`` (the size-invariance experiment)."""
    rng = random.Random(seed)
    book = ExposureBook(CONVENTIONS)
    for i in range(38):
        book.add_position(_ticket(rng, f"held{i}", 4_110_000 // 38, scale))
    marginals: dict[str, float] = {}
    for pos in book.positions.values():
        for leg in pos.legs:
            marginals[leg.market_ticker] = rng.uniform(0.15, 0.85)
    return book, marginals, rng


def _warm_centre(sd: float = 0.12) -> SteerCenter:
    c = SteerCenter(half_life=256.0)
    rng = random.Random(19)
    for _ in range(600):
        c.observe(rng.gauss(0.0, sd))
    return c


def profiles(book: ExposureBook, snap, centre: SteerCenter | None = None):
    """Every steering profile, built the way the lifecycle builds them."""
    raw = {k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()}
    tot = sum(raw.values()) or 1.0
    fam = {k: float(v) for k, v in snap.committed_loss_by_family_cc.items()}
    ent = {k: float(v) for k, v in snap.committed_loss_by_entity_cc.items()}
    tf = sum(fam.values()) or 1.0
    te = sum(ent.values()) or 1.0
    return {
        "pbook_profile": PBookProfile(
            input_generation=GEN,
            p_book=0.58,
            tail_share_by_game={g: v / tot for g, v in raw.items()},
            total_tail_cc=tot,
            game_budget_cc=GAME_WALL_CC,
        ),
        "pbook_book_generation": GEN,
        "leg_axis_profile": LegAxisProfile(
            shares_by_family={k: v / tf for k, v in fam.items()},
            total_family_cc=tf,
            shares_by_entity={k: v / te for k, v in ent.items()},
            total_entity_cc=te,
            family_budget_cc=GAME_WALL_CC,
            entity_budget_cc=ENTITY_WALL_CC,
            p_book=0.58,
        ),
        "conc_profile": ConcentrationProfile(
            loss_events=build_loss_event_book(
                (ticket_bucket(p.legs), float(p.max_loss_cc))
                for p in book.positions.values()
            ),
            game_dollars_cc=raw,
            game_wall_cc=GAME_WALL_CC,
            family_dollars_cc=fam,
            family_wall_cc=GAME_WALL_CC,
            entity_dollars_cc=ent,
            entity_wall_cc=ENTITY_WALL_CC,
            fill_elasticity_per_cent=FILL_ELASTICITY,
            centre=centre if centre is not None else _warm_centre(),
        ),
    }


def skew(candidate, snap, marginals, profs, params, *, wire_conc=True):
    kwargs = dict(profs)
    if not wire_conc:
        kwargs["conc_profile"] = None
    return compute_inventory_skew(
        candidate,
        snap,
        marginals.get,
        CONVENTIONS,
        LIMITS,
        params,
        margin_cc=MARGIN_CC,
        tick_cc=TICK_CC,
        observe_centre=False,      # frozen centre: comparisons are exact
        **kwargs,
    )


def warmed_profiles(book: ExposureBook, snap, marginals, cands):
    """Profiles whose ``SteerCenter`` has been warmed on the REAL score stream,
    exactly the way the running bot warms it (one observation per quote), then
    frozen. Warming on synthetic noise instead would mis-standardise the score
    and understate the armed magnitude by ~9x — the centre IS the mechanism
    that makes the steer economically real."""
    centre = SteerCenter(half_life=256.0)
    profs = profiles(book, snap, centre)
    for c in cands:
        compute_inventory_skew(
            c, snap, marginals.get, CONVENTIONS, LIMITS, ARMED,
            margin_cc=MARGIN_CC, tick_cc=TICK_CC, observe_centre=True, **profs,
        )
    return profs


def candidate_grid(book: ExposureBook, rng: random.Random, n: int = 200):
    """The realistic mix the harness uses: half land on a loss event the book
    already holds (CONCENTRATORS — the K-ladder / same-match shape), half are
    fresh AND-bound events (DIVERSIFIERS). One-sided flow would only ever
    exercise one branch of the steer."""
    held = list(book.positions.values())
    out = []
    for i in range(n):
        if i % 2 == 0:
            src = held[rng.randrange(len(held))]
            out.append(
                OpenPosition(
                    position_id=f"cand{i}",
                    combo_ticker=f"COMBO-cand{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(3_000),
                    entry_price_cc=CentiCents(4_000),
                    legs=src.legs,
                )
            )
        else:
            out.append(_ticket(rng, f"cand{i}", 120_000))
    return out


# The price-facing surface. ``conc`` and ``shadow_armed_skew_cc`` are the
# shadow record and are deliberately EXCLUDED — everything else must match.
PRICE_FIELDS = tuple(
    f.name
    for f in dataclasses.fields(InventorySkew)
    if f.name not in ("conc", "shadow_armed_skew_cc")
)


def price_view(s: InventorySkew) -> tuple:
    return (
        tuple(getattr(s, f) for f in PRICE_FIELDS)
        + (s.applied_cc, s.shadow_applied_cc)
    )


class TestShipsUnarmed:
    """The default IS the shadow. A restart with today's YAML must not price
    the steer."""

    def test_skew_params_default_is_shadow(self) -> None:
        p = SkewParams()
        assert p.conc_enabled is True      # derived + logged
        assert p.conc_armed is False       # ...and it cannot reach price

    def test_config_default_is_shadow(self) -> None:
        from combomaker.ops.config import SkewConfig

        cfg = SkewConfig()
        assert cfg.conc_enabled is True
        assert cfg.conc_armed is False

    def test_the_live_yaml_does_not_arm_it(self) -> None:
        """The armed prod YAML is gitignored, so this reads whatever the
        operator actually has on disk: if a ``conc_armed`` ever appears there,
        this test is the thing that makes it a deliberate act."""
        import yaml

        path = REPO / "config" / "prod-live-wc.local.yaml"
        if not path.exists():          # CI / a fresh clone
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sk = ((raw.get("pricing") or {}).get("skew")) or {}
        assert sk.get("conc_armed", False) is False


class TestShadowIsByteIdentical:
    """THE LOAD-BEARING PROPERTY. Unarmed, the composition is byte-identical to
    the one that ships with the steer not wired at all — over a GRID, not one
    example."""

    def test_every_price_field_matches_over_the_candidate_grid(self) -> None:
        book, marginals, rng = build_world()
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        n_nonzero_shadow = 0
        for cand in candidate_grid(book, rng):
            shadow = skew(cand, snap, marginals, profs, SHIPPED)
            unwired = skew(
                cand, snap, marginals, profs, SHIPPED, wire_conc=False
            )
            assert price_view(shadow) == price_view(unwired), cand.position_id
            # ...and the steer really WAS derived on that same call.
            assert shadow.conc is not None
            assert unwired.conc is None
            if shadow.conc.skew_cc != 0:
                n_nonzero_shadow += 1
        # Not a vacuous pass: the shadow steer is loudly non-zero throughout.
        assert n_nonzero_shadow > 150

    def test_identity_holds_on_every_combination_of_the_other_flags(
        self,
    ) -> None:
        """pbook/leg-axis arming changes which axis scores feed the steer, so
        the identity is proved on all four combinations, not just the live one."""
        book, marginals, rng = build_world(seed=23)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        cands = candidate_grid(book, rng, n=40)
        for pb in (False, True):
            for la in (False, True):
                params = SkewParams(
                    enabled=True, pbook_armed=pb, leg_axis_armed=la
                )
                for cand in cands:
                    a = skew(cand, snap, marginals, profs, params)
                    b = skew(
                        cand, snap, marginals, profs, params, wire_conc=False
                    )
                    assert price_view(a) == price_view(b), (pb, la)

    def test_disabled_does_not_even_compute_it(self) -> None:
        """``conc_enabled: false`` is the ZERO-COST rollback: no steer object at
        all, and the same price."""
        book, marginals, rng = build_world(seed=31)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        for cand in candidate_grid(book, rng, n=40):
            off = skew(cand, snap, marginals, profs, OFF)
            unwired = skew(
                cand, snap, marginals, profs, SHIPPED, wire_conc=False
            )
            assert off.conc is None
            assert off.shadow_armed_skew_cc is None
            assert price_view(off) == price_view(unwired)

    def test_arming_a_disabled_steer_still_cannot_price(self) -> None:
        """Fail-safe on the flag pair: ``conc_armed`` without ``conc_enabled``
        is not a live steer (nothing was computed to arm)."""
        book, marginals, rng = build_world(seed=37)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        params = dataclasses.replace(SHIPPED, conc_enabled=False, conc_armed=True)
        for cand in candidate_grid(book, rng, n=20):
            a = skew(cand, snap, marginals, profs, params)
            b = skew(cand, snap, marginals, profs, SHIPPED, wire_conc=False)
            assert price_view(a) == price_view(b)


class TestTheShadowReadOut:
    """What the operator reads to decide arming has to be the ARMED number, not
    a proxy for it."""

    def test_the_counterfactual_equals_what_arming_actually_produces(
        self,
    ) -> None:
        book, marginals, rng = build_world(seed=11)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        for cand in candidate_grid(book, rng, n=100):
            shadow = skew(cand, snap, marginals, profs, SHIPPED)
            armed = skew(cand, snap, marginals, profs, ARMED)
            # EXACT, to the centi-cent, on both the classifier and the pricer
            # frame — the read-out is the arming decision's ground truth.
            assert shadow.shadow_armed_skew_cc == armed.skew_cc
            assert shadow.shadow_armed_applied_cc == armed.applied_cc

    def test_armed_reports_no_counterfactual(self) -> None:
        book, marginals, rng = build_world(seed=13)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        for cand in candidate_grid(book, rng, n=20):
            armed = skew(cand, snap, marginals, profs, ARMED)
            assert armed.shadow_armed_skew_cc is None
            assert armed.shadow_armed_applied_cc is None

    def test_the_shadow_decomposition_is_fully_populated(self) -> None:
        """Everything the read-out buckets on must be present while dark."""
        book, marginals, rng = build_world(seed=17)
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        profs = profiles(book, snap)
        for cand in candidate_grid(book, rng, n=20):
            c = skew(cand, snap, marginals, profs, SHIPPED).conc
            assert c is not None
            assert c.reason
            assert c.scale is not None and c.scale.half_cc > 0
            assert -1.0 <= c.hhi.intensity <= 1.0
            assert set(c.wall_load_by_axis) == {"game", "family", "entity"}


class TestArmedMagnitude:
    """What arming BUYS and what it COSTS, measured — the operator weighs these
    against each other, so they are pinned, not described."""

    def test_arming_is_a_10x_step_in_applied_magnitude(self) -> None:
        """The review's headline number, reproduced as a property. MEASURED on
        this fixture: median |applied_cc| 14.0 (shadow) -> 141.0 (armed) =
        10.07x, against the review's 14.0 -> 140.0."""
        book, marginals, rng = build_world()
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        cands = candidate_grid(book, rng, n=400)
        profs = warmed_profiles(book, snap, marginals, cands)
        shadow = [abs(skew(c, snap, marginals, profs, SHIPPED).applied_cc)
                  for c in cands]
        armed = [abs(skew(c, snap, marginals, profs, ARMED).applied_cc)
                 for c in cands]
        m_shadow = statistics.median(shadow)
        m_armed = statistics.median(armed)
        assert m_shadow > 0
        # A LOWER bound only: this is the step the operator is being asked to
        # authorise, and the point of the test is that it cannot shrink into
        # invisibility or grow without someone re-reading this number.
        assert 5.0 <= m_armed / m_shadow <= 20.0, (m_shadow, m_armed)

    def test_the_swing_is_a_material_fraction_of_the_margin(self) -> None:
        """Diversifier -> concentrator swing vs the 200cc margin. MEASURED here:
        231.9cc = 116.0%, against the review's 229.1cc = 114.5%."""
        book, marginals, rng = build_world()
        snap = book.snapshot(marginals.get, mass_acceptance=True)
        cands = candidate_grid(book, rng, n=400)
        profs = warmed_profiles(book, snap, marginals, cands)
        rows = [skew(c, snap, marginals, profs, ARMED) for c in cands]
        div = [r.applied_cc for r in rows if r.conc and r.conc.hhi.relative > 0]
        con = [r.applied_cc for r in rows if r.conc and r.conc.hhi.relative < 0]
        assert div and con
        swing = statistics.mean(div) - statistics.mean(con)
        # Diversifiers strictly tighter (positive applied = higher no_bid), and
        # the swing is economically visible against the frozen markup.
        assert swing > 0
        assert swing / MARGIN_CC > 0.5, swing


class TestSizeInvarianceSurvivesArming:
    """The operator's rule — "we shouldn't be widening all of our bets just
    because we hit a $ amount of positions" — must hold in the ARMED
    composition, which is the one that would price."""

    def test_identical_candidate_reads_identically_at_1x_and_3x(self) -> None:
        for scale in (3.0, 10.0):
            base_book, base_marg, rng = build_world()
            snap1 = base_book.snapshot(base_marg.get, mass_acceptance=True)
            big_book, big_marg, _ = build_world(scale=scale)
            snap3 = big_book.snapshot(big_marg.get, mass_acceptance=True)
            # ONE frozen centre for both, so the comparison isolates book size.
            centre = _warm_centre()
            p1 = profiles(base_book, snap1, centre)
            p3 = profiles(big_book, snap3, centre)
            for cand in candidate_grid(base_book, rng, n=40):
                a = skew(cand, snap1, base_marg, p1, ARMED)
                b = skew(cand, snap3, big_marg, p3, ARMED)
                assert a.conc is not None and b.conc is not None
                assert a.conc.skew_cc == b.conc.skew_cc, (
                    scale, cand.position_id
                )
                assert a.peak_cc == b.peak_cc
                assert a.pbook_cc == b.pbook_cc
                assert a.family_cc == b.family_cc
                assert a.entity_cc == b.entity_cc

    def test_the_shadow_composition_is_size_invariant_too(self) -> None:
        """The one that actually ships on the restart."""
        base_book, base_marg, rng = build_world()
        snap1 = base_book.snapshot(base_marg.get, mass_acceptance=True)
        big_book, big_marg, _ = build_world(scale=3.0)
        snap3 = big_book.snapshot(big_marg.get, mass_acceptance=True)
        centre = _warm_centre()
        p1 = profiles(base_book, snap1, centre)
        p3 = profiles(big_book, snap3, centre)
        for cand in candidate_grid(base_book, rng, n=40):
            a = skew(cand, snap1, base_marg, p1, SHIPPED)
            b = skew(cand, snap3, big_marg, p3, SHIPPED)
            assert (a.peak_cc, a.pbook_cc, a.family_cc, a.entity_cc) == (
                b.peak_cc, b.pbook_cc, b.family_cc, b.entity_cc
            )


class TestTheEmittedQuoteIsUnchanged:
    """The classifier identity above is the mechanism; THIS is the outcome the
    operator cares about — the bytes we send the exchange."""

    async def test_the_quote_on_the_wire_is_identical_in_shadow(
        self, tmp_path
    ) -> None:
        from tests.test_lifecycle import rfq
        from tests.test_quoting_policy import PolicyRig, _harness

        sent = {}
        for tag, params in (("off", OFF), ("shadow", SHIPPED), ("armed", ARMED)):
            h = await _harness()
            store = await Store.open(tmp_path / f"{tag}.sqlite3", h.clock)
            rig = PolicyRig(h, store, skew_params=params)
            for pos in build_world()[0].positions.values():
                rig.exposure.add_position(pos)
            # PIN CHANGED 2026-09-04 (build A item 2, risk/rebate_bound.py):
            # a LEG-AXIS rebate on a direction the book holds no mirror of no
            # longer reaches the wire (measured: the 8/12 "nobody homers"
            # ticket earned −26cc family + −8cc entity rebate on an EMPTY
            # cell). This world's ONLY price mover under OFF/SHADOW was
            # exactly that rebate (family −62 + entity −9 on M1:yes/M2:no,
            # which the 38 KXMLB* tickets never hold), so hold the mirror
            # directions: the rebate is then exposure-backed, survives, and
            # the guard below keeps testing the STEER's visibility on the
            # wire rather than the removed rebate.
            rig.exposure.add_position(
                OpenPosition(
                    position_id="mirror",
                    combo_ticker="COMBO-mirror",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(1_000),
                    entry_price_cc=CentiCents(5_000),
                    legs=(LegRef("M1", "E1", "no"), LegRef("M2", "E2", "yes")),
                )
            )
            await rig.lifecycle.handle_rfq(rfq())
            sent[tag] = dict(rig.sender.created[0])
            await store.close()
        # SHADOW == the steer not wired at all, to the byte, on the wire.
        assert sent["shadow"] == sent["off"], (sent["shadow"], sent["off"])
        # ...and arming is a real, visible change, so the identity above is not
        # a test that would pass no matter what.
        assert sent["armed"] != sent["off"], sent["armed"]

    async def test_a_cold_steer_under_the_armed_flag_still_bounds_the_leg_axis(
        self, tmp_path, monkeypatch
    ) -> None:
        """REVIEW FIX S1 (2026-09-04). skew.py composes the leg-axis (family /
        entity) rebate into the price whenever ``conc is None`` — the steer
        disabled OR its CRN profile cold — even under ``conc_armed``. The
        rebate bound must therefore key on whether the steer PRICED the
        candidate, never on the flag alone: with the fail-safe flag pair
        (conc_enabled=False, conc_armed=True) and the UN-mirrored world (the
        family −62 + entity −9 cc rebate on M1:yes/M2:no that nothing in the
        book backs), the unbacked rebate is removed exactly as under SHIPPED,
        and the wire equals OFF. Before the fix ``leg_axis_armed and not
        conc_armed`` was False here, so the rebate reached the wire."""
        import combomaker.rfq.lifecycle as lc
        from tests.test_lifecycle import rfq
        from tests.test_quoting_policy import PolicyRig, _harness

        cold_armed = dataclasses.replace(SHIPPED, conc_enabled=False, conc_armed=True)
        sent = {}
        shadow = {}
        for tag, params in (("off", OFF), ("shipped", SHIPPED), ("cold_armed", cold_armed)):
            seen: list[tuple[str, dict]] = []
            real_info = lc.log.info

            def _info(event, *a, _seen=seen, _real=real_info, **kw):
                _seen.append((event, kw))
                return _real(event, *a, **kw)

            monkeypatch.setattr(lc.log, "info", _info)
            h = await _harness()
            store = await Store.open(tmp_path / f"s1-{tag}.sqlite3", h.clock)
            rig = PolicyRig(h, store, skew_params=params)
            for pos in build_world()[0].positions.values():
                rig.exposure.add_position(pos)
            await rig.lifecycle.handle_rfq(rfq())
            sent[tag] = dict(rig.sender.created[0])
            shadow[tag] = [kw for ev, kw in seen if ev == "inventory_skew_shadow"][-1]
            await store.close()
        assert sent["cold_armed"] == sent["off"] == sent["shipped"]
        for tag in ("shipped", "cold_armed"):
            rec = shadow[tag]
            assert rec["rebate_bound_rule"] == "exposure_backed", rec
            assert rec["rebate_unbacked_cc"] > 0 and rec["applied_cc"] == 0, rec
            assert rec["applied_unbounded_cc"] > rec["applied_cc"], rec


class TestMarkupsUnchanged:
    """MARKUPS ARE FROZEN (operator standing decision). The steer reallocates
    width inside the existing margin; it must not have touched the markup
    policy or its configured values."""

    def _blob(self, rel: str) -> bytes:
        return subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=REPO, capture_output=True, check=True,
        ).stdout.replace(b"\r\n", b"\n")

    def test_markup_module_sha256_matches_head(self) -> None:
        rel = "src/combomaker/pricing/markup.py"
        head = hashlib.sha256(self._blob(rel)).hexdigest()
        work = hashlib.sha256(
            (REPO / rel).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        # HEAD 95588f7: ad3a7c7f147fe484fa6c6db03038d0ebd684942bc83bb6100c12
        # deb94ee355e5
        assert work == head, (work, head)

    def test_shipped_markup_values_match_head(self) -> None:
        import yaml

        for rel in ("config/prod.yaml", "config/demo.yaml"):
            path = REPO / rel
            if not path.exists():
                continue
            now = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            was = yaml.safe_load(self._blob(rel).decode("utf-8")) or {}
            now_m = ((now.get("pricing") or {}).get("markup")) or {}
            was_m = ((was.get("pricing") or {}).get("markup")) or {}
            assert now_m == was_m, rel
