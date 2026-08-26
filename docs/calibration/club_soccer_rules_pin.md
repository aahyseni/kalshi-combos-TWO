# Club-soccer settlement rules pin — La Liga / MLS / UECL (scraped 2026-08-12)

Source of truth: `rules_primary`/`rules_secondary` scraped from LIVE market
objects (public `GET /markets/{ticker}`), one exemplar per series, 2026-08-12.
Raw text below, verbatim. Cross-checked against the WC-era pin (NOTES.md I8 —
operator-provided rulebook text, 2026-07-06 — and C1).

## The pin (all 12 game-level series, all three leagues)

| convention | rule text (verbatim fragment) | matches WC? |
|---|---|---|
| GAME (3-way ML incl TIE) | "…after **90 minutes plus stoppage time (does not include extra time or penalties)**" | ✅ identical to KXWC GAME (Regulation-Time ML) |
| TOTAL (over N.5 goals) | same regulation-only clause | ✅ |
| SPREAD ("X wins by more than N.5") | same regulation-only clause | ✅ |
| BTTS | same clause + "**Own goals count towards the team awarded the goal**" | ✅ |
| UECL ADVANCE | "…advance past … in the soccer **tie** in the Qualification Round 3" (close_time spans the return leg) | ✅ = KXWCADVANCE shape (ET+pens tie market). OUT OF SCOPE per operator (ML only) |

**Consequences for wiring (all machinery exists):** `include_et=False` on every
priced family; the GAME/ADVANCE coexistence maps exactly as the WC adapter
already does (GAME = 90'-3-way, ADVANCE = tie); containment windows C1 stay
exact (FT families share one settlement window; 1H nests inside it).

## ⚠ NEW CATCH — the 48-hour reschedule SCALAR rule applies to club soccer

GAME rules_secondary (La Liga, MLS, UECL alike): "**If the game is cancelled or
rescheduled to over 48 hours away, the market will resolve to a fair price in
accordance with the rules.**"

This is the same scalar-settlement surface as MLB's 48h rain rule (trap B8.4).
The WC never exercised it; club leagues (weather, fixture congestion) can.
Therefore:

1. **`farmable=False` for ALL club-soccer impossibility cells** — club soccer
   must NOT inherit WC's `farmable=True` patterns; the airtight-tautology bar
   fails the same way MLB's did.
2. Scalar-aware accounting already handles the settlement side (NO pays 1−V,
   promoted convention); combo scalar products multiply per the DNP forensics
   rulebook reading.
3. Postponement ⇒ whole-game scalar is game-scoped (all families at once) —
   same class as the MLB rain analysis (deferred-ledger item 13), now with a
   second sport attached.

## Timing anchors (observed on the exemplars)

- `expected_expiration_time` ≈ kickoff + ~2h for club GAME/TOTAL/SPREAD/BTTS
  (e.g. MLS 8/16 SEAVAN: exp 8/17 06:30Z; UECL 8/12 SCRPAI: exp 22:00Z) — the
  WC-era soccer offset (4.5h default) needs a per-league re-derivation at
  wiring; club game codes carry NO kickoff time (`26AUG26RMARSO`), so the
  pregame anchor = min(close_time, expected_expiration − offset) path.
- `close_time` = ~2–3 days after the game (the 48h reschedule window) — NEVER
  a start anchor (same lesson as MLB close_time = game+3d).
- ADVANCE close_time spans the return leg (26AUG13TOBPAR → close 9/10).

## Home/away frame — STILL TO PIN (blocking DC home-advantage)

Titles read "Real Madrid vs Real Sociedad" with game code `26AUG26RMARSO`
(first token = first-named team). MLB codes are AWAY-first; soccer fixture
convention is usually HOME-first. DO NOT ASSUME — pin per league from an
official schedule + one settled market before Dixon-Coles home-advantage is
armed (L1 frame-trap protocol; the WC was neutral-venue so this never
mattered before).

## Raw scrape (verbatim rules_primary per series exemplar)

- `KXLALIGAGAME-26AUG26RMARSO-TIE`: "If Tie is the result of the Real Madrid
  vs Real Sociedad professional La Liga soccer game originally scheduled for
  Aug 26, 2026 after 90 minutes plus stoppage time (does not include extra
  time or penalties), then the market resolves to Yes."
- `KXLALIGATOTAL-26AUG17DEPELC-6`: "If over 5.5 goals are scored in the
  Deportivo De La Coruna vs Elche professional La Liga soccer game … after 90
  minutes plus stoppage time (does not include extra time or penalties) …"
- `KXLALIGASPREAD-26AUG17DEPELC-ELC3`: "If Elche wins by more than 2.5 goals
  … after 90 minutes plus stoppage time (does not include extra time or
  penalties) …"
- `KXLALIGABTTS-26AUG17DEPELC-BTTS`: "If Deportivo De La Coruna and Elche
  both score a goal … after 90 minutes plus stoppage time … " + own-goals
  clause.
- `KXMLSGAME-26AUG23ATLSKC-TIE`, `KXMLSTOTAL-26AUG16SEAVAN-6`,
  `KXMLSSPREAD-26AUG16SEAVAN-VAN3`, `KXMLSBTTS-26AUG16SEAVAN-BTTS`: identical
  clause structure, "professional MLS soccer game".
- `KXUECLGAME-26AUG13TOBPAR-TOB`, `KXUECLTOTAL-26AUG12SCRPAI-7`,
  `KXUECLSPREAD-26AUG12SCRPAI-SCR4`, `KXUECLBTTS-26AUG13TOBPAR-BTTS`:
  identical clause structure, "professional Conference League soccer game".
- `KXUECLADVANCE-26AUG13TOBPAR-TOB`: "If Tobol advance past Partizan Belgrade
  in the Tobol vs Partizan Belgrade soccer tie in the Qualification Round 3
  of the Conference League…"

## 2026-08-26 extension — Bundesliga + Saudi Pro League (scraped 2026-08-26)

Same method (live market objects, one exemplar per series, authed recon fleet
2026-08-26). Both leagues are **verbatim-identical** to the 12-series pin
above: 90 minutes + stoppage, explicitly excluding extra time and penalties;
BTTS own-goals clause; GAME `rules_secondary` carries the 48-hour
cancel/reschedule fair-price scalar clause ⇒ **`farmable=False` inherits**
(the series-scoped `_farm_certain` [KXWC] mechanism needs no change).

- `KXBUNDESLIGAGAME-26SEP06SGEFCA-TIE`: "If Tie is the result of the
  Frankfurt vs Augsburg professional Bundesliga soccer game originally
  scheduled for Sep 6, 2026 after 90 minutes plus stoppage time (does not
  include extra time or penalties), then the market resolves to Yes." +
  48h clause in rules_secondary.
- `KXBUNDESLIGABTTS-26AUG30FCASCH-BTTS`: 90-min clause + "Own goals count
  towards the team awarded the goal." TOTAL/SPREAD same 90-min convention.
- `KXSAUDIPLGAME-26SEP01HILAAS-TIE`: "If Tie is the result of the Al Hilal
  vs Al Ahli Saudi professional Saudi Pro League soccer game originally
  scheduled for Sep 1, 2026 after 90 minutes plus stoppage time (does not
  include extra time or penalties), then the market resolves to Yes." +
  48h clause.
- `KXSAUDIPLBTTS-26AUG28KHAHIL-BTTS`: 90-min clause + own-goals clause.
  TOTAL/SPREAD same.

Timing anchors (measured, same-fixture deltas): GAME = kickoff+3h,
TOTAL/SPREAD/BTTS = kickoff+4h — exactly the wired-league pattern
(Bundesliga 26AUG30FCASCH GAME exp 18:30Z vs others 19:30Z, kickoff 15:30Z;
Saudi 21:00-AST kickoffs: GAME 21:00Z, others 22:00Z). Config rows:
`KX{BUNDESLIGA,SAUDIPL}GAME: 3.0167`, catch-all `4.0167`, 48.0h max-pregame.

Naming/trap notes: Kalshi's ONLY Saudi soccer base is `KXSAUDIPL` (Saudi
Pro League; SAUDIPRO/SPL/ROSHN/SAUDI/SAUDIPREM all 404). `KXBUNDESLIGA2*`
= **Bundesliga 2**, a different league sharing the character prefix (the
exact-family-with-dash allowlist style keeps it out; test-pinned).
`KXBBLGAME` = Bundesliga **basketball** (no string collision). No Bundesliga
ADVANCE series exists; `KXSAUDIPLADVANCE` is listed-but-empty — both leagues
wire the four families only. Saudi events reverse-probe EMPTY on
`associated_event_ticker` yet a live open RFQ carries a KXSAUDIPLGAME leg
inside `KXMVECROSSCATEGORY-SHARD1-R` — membership is tape-proven; the
association appears to lag ~a week before fixtures (Bundesliga's Sep 6 event
also probed empty while its Aug 30 event returned all three collections).
Home/away frame: still TO PIN for both (ticker-first = home consistent with
all 8 markets read, incl. Frankfurt home on 9/6 — but that is one fixture,
not a pin; L1 protocol before DC home-advantage arms).
