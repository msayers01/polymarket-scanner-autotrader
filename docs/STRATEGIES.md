# Strategies — math, assumptions, and failure modes

Every strategy is split into two parts:

1. **pure math** in `src/pmtrader/quant/` (unit- and property-tested), and
2. **policy** in `src/pmtrader/strategies/<name>.py`: detection, entry planning, exits and guards.

Notation: `p` is a market price (a probability in [0,1]), `q` is our fair probability, `a`/`b` are best
ask/bid, `τ` is time to resolution (years unless noted), `Φ` is the standard normal CDF, and `C` is the number of shares.

---

## 0. Cross-cutting economics

### 0.1 Fees (venue formula)

Polymarket charges **takers** on fee-enabled markets:

```
fee(C, p) = C · rate · (p · (1 − p))^exponent        (USDC; rounded to 5 dp, min 0.00001)
```

`rate`, `exponent`, `takerOnly` and `rebateRate` come from each market's `feeSchedule`.
Observed Oct-2026 values: crypto 0.07, sports 0.05, others 0.04, geopolitics 0 (exponent 1, taker only, maker rebate 20–25%).
At p = 0.5 a crypto taker pays **1.75¢/share**, which is often larger than the edge. Consequences:

* Prefer **maker (post-only)** execution unless the edge clears the taker fee by a margin.
* Makers receive `rebateRate × taker_fee` of their counterparty. This is modelled as optional upside and never relied on.
* `quant/fees.py` is the single implementation. Never hard-code fees elsewhere.

### 0.2 Capital efficiency

Capital is locked until resolution, so opportunities are ranked by **net return per day of capital lock**:

```
ROI      = EV_net / capital_at_risk
score    = ROI · 365 / max(days_to_resolution, d_min)    (annualised, capped)
```

A 2% riskless arb that resolves in 2 days (~365% APR) beats a 5% arb that resolves in 6 months (~10% APR).
`d_min` (default 0.25 days) stops tiny τ from exploding the score.

### 0.3 Position sizing — fractional Kelly with parameter uncertainty

When buying a binary contract at all-in cost `c` (price + fee per share) with belief `q`, the growth-optimal fraction of bankroll is

```
f* = (q − c) / (1 − c)                           (buy YES;  for NO use q_no = 1 − q, c_no)
```

`q` is an *estimate*. Following Baker & McHale (2013, "Optimal betting under parameter uncertainty"), we shrink by the signal-to-noise ratio and apply a fractional multiplier λ:

```
f = λ · f* · e² / (e² + s²),        e = q − c,   s = std-error of q
```

The final size is `min(f · bankroll, risk caps, depth cap, strategy cap)`, rounded down to the market's size increment, and dropped if below `min_order_size`.
**Structural arbitrage** is not Kelly-sized (it has no variance at resolution). It is sized by depth and the per-market cap instead.

### 0.4 Depth-aware execution cost

`quant/microstructure.walk_book(levels, qty)` returns the VWAP and the marginal price of taking `qty` through the book. All edges use **marginal executable prices at the intended size**, never the top of book alone.

---

## 1. `complete_set_arb` — binary complete-set arbitrage (structural)

A YES and a NO share of the same market together always pay exactly $1, including on 50-50 resolutions. They can also be merged back to $1 USDC before resolution.

**Buy side.** Take `C` YES and `C` NO at cumulative costs `A_y(C)`, `A_n(C)` (book walks):

```
profit(C) = C − A_y(C) − A_n(C) − fee_y(C) − fee_n(C)
```

`profit` is concave in `C` (marginal ask prices are non-decreasing). We increase `C` level by level while the
*marginal* set cost `a_y(C) + a_n(C) + f'_y + f'_n < 1 − ε`, where `ε` is a safety margin (default 0.3¢) that covers price movement between the legs.

**Sell side** (`b_y + b_n − fees > 1 + ε`) means mint a set for $1 (split) and sell both legs. This needs on-chain split support, so in v0.1 the scanner **reports** it but does not execute it.

**Execution risk (leg risk).** Send the thinner leg first as FAK (fill-and-kill) at the computed limit. Then send the second leg sized to the first leg's fill. If the second leg fails, the residual is a directional position, which is handed to the exit manager with a tight stop. Paper mode simulates both legs against the live book.

**Failure modes:** stale book (guarded by a staleness check), fee schedule change (re-read each catalog refresh), and min-order-size rounding that breaks equal legs (sizes are rounded to a common feasible size).

## 2. `negrisk_basket_arb` — multi-outcome basket arbitrage (structural)

For a **mutually exclusive and exhaustive** event with N outcomes (a neg-risk event), exactly one YES pays $1:

```
YES basket:  Σᵢ a_yes,i + fees < 1 − ε      → buy 1 YES of every outcome, payoff 1
NO  basket:  Σᵢ a_no,i  + fees < N − 1 − ε  → buy 1 NO  of every outcome, payoff N − 1
```

**Exhaustiveness guard (critical):** *augmented* neg-risk events can have unnamed placeholder outcomes and an "Other" whose definition changes over time.
We only trade baskets when every outcome is named, tradable and accepting orders, and when the event is not flagged as augmented with unnamed placeholders.
Otherwise the basket is not exhaustive and the "arb" can lose 100%.

## 3. `ladder_arb` — monotonicity across strikes (structural)

For markets on the **same underlying, same observation time, same resolution source**:

* Terminal "above K": `P(S_T > K)` is non-increasing in K.
* Up-barrier "reach H": `P(touch H)` is non-increasing in H.
* Down-barrier "dip to L": `P(touch L)` is non-decreasing in L.

For a pair with `K₁ < K₂` (above / reach), buy **YES(K₁) + NO(K₂)**. The payoff is ≥ 1 in every state:

| state | YES(K₁) | NO(K₂) | total |
|---|---|---|---|
| S below K₁ | 0 | 1 | 1 |
| K₁ < S ≤ K₂ | 1 | 1 | 2 |
| S above K₂ | 1 | 0 | 1 |

Arbitrage condition: `a_yes(K₁) + a_no(K₂) + fees < 1 − ε`. The payoff floor is 1, and there is a 2× upside in the middle state, so the scanner reports the EV including the middle-state probability from `crypto_fair_value`. The **guaranteed** profit is the floor.
Parsing guard: both markets must come from the same Gamma *event* (same template, date and source), and the parser must extract strikes with high confidence.

## 4. `crypto_fair_value` — model-based pricing of crypto price markets

The templates observed on Polymarket (resolution source: Binance 1-minute candles) are:

| template | example | payoff | model |
|---|---|---|---|
| terminal above | "Will the price of Bitcoin be above $74,000 on October 18?" | 1{S_T > K} | European digital |
| up/down | "Bitcoin Up or Down - October 17, 5PM ET" | 1{S_T ≥ S_open} | digital with K = open (once the window starts) |
| reach (up barrier) | "Will Bitcoin reach $150,000 in October?" | 1{max S ≥ H} | one-touch, reflection principle |
| dip (down barrier) | "Will Bitcoin dip to $60,000 by Dec 31, 2026?" | 1{min S ≤ L} | one-touch, reflection principle |
| price buckets | "Bitcoin price on October 18?" (neg-risk) | 1{K_lo ≤ S_T < K_hi} | difference of digitals |

**Dynamics.** Over the horizons involved, GBM with zero drift: `X_t = ln(S_t/S_0) = νt + σW_t`, `ν = −σ²/2`.

**European digital:**
```
q = P(S_T > K) = Φ(d₂),      d₂ = [ln(S/K) − σ²τ/2] / (σ√τ)
```

**One-touch up barrier H > S** (`b = ln(H/S) > 0`), from the reflection principle for drifted Brownian motion:
```
q = Φ((−b + ντ)/(σ√τ)) + e^{2νb/σ²} · Φ((−b − ντ)/(σ√τ)),     e^{2νb/σ²} = S/H  (for ν = −σ²/2)
```
**One-touch down barrier L < S** (`b = ln(L/S) < 0`):
```
q = Φ((b − ντ)/(σ√τ)) + e^{2νb/σ²} · Φ((b + ντ)/(σ√τ))
```
If the running max/min since the window start has already crossed the barrier, q = 1. The running extremum is computed from klines.
1-minute candle highs/lows approximate continuous monitoring well, so no Broadie–Glasserman–Kou correction is applied.

**Volatility σ (seasonal).** A flat annualised σ is wrong for short windows: crypto variance has strong
intraday and weekday/weekend periodicity (Andersen & Bollerslev 1997). Measured on BTC (Oct 2026): weekend variance
≈ 0.41× the weekly average, and 13–15 UTC (US open) ≈ 2.5×. A flat σ made a 5-hour Saturday-evening barrier look
10¢ mispriced when the market was in fact right. We therefore use:
* base σ = variance blend of the **7-day hourly Parkinson** estimate (one full weekly cycle, so already de-seasonalised)
  and Deribit **DVOL** (30-day implied, BTC/ETH), weight 0.5 each;
* a multiplicative seasonal profile `v(t) = hod[hour_of_day] × wk[weekday|weekend]` fitted from ~41 days of hourly
  Parkinson variances (normalised to mean 1, shrunk 20% toward 1);
* the σ used for a contract is `σ_base · √(mean v(t) over [now, expiry])` (`CryptoReference.vol_for_window`).

**Market blending.** The model is not the only information source; the market sees news and flows. The fair band
is shrunk toward the market mid: `q = w·q_model + (1−w)·p_mid` with `w = model_weight` (default 0.6). Edges
therefore require *strong and robust* disagreement, which cuts trades driven by model error.

**Model uncertainty band.** Crypto returns are fat-tailed, so lognormal digitals *underprice the far tails*. We compute q at `σ·m_lo` and `σ·m_hi` (default 0.8 and 1.4) and treat `[q_lo, q_hi]` as the fair range. We trade only when the executable price lies outside the band by more than costs plus margin:

```
buy YES if  a_yes + fee + margin < q_lo
buy NO  if  a_no  + fee + margin < 1 − q_hi
```

The standard error used for Kelly shrinkage is `s = (q_hi − q_lo)/4`.

**Exits.** (1) **Edge decay:** take profit when the bid is at least the fair mid (`q`) minus exit_margin. (2) **Edge reversal:** exit if the model now says the position is overpriced by > reversal threshold. (3) **Time stop:** for terminal digitals, close positions or hold through expiry per config (`hold_to_expiry_if_itm`). (4) Hard stop at −X% of position cost.

**Failure modes:** a basis between Binance and other venues (we use the resolution source's own pair), a stale reference price (`ReferencePrice` age guard < 5 s for τ < 1 h), parser errors (only templates matched with confidence 1.0 are traded), and event-time jumps (macro releases); the LLM gate can veto those.

## 5. `market_maker` — reward-aware Avellaneda–Stoikov in probability space

Edge comes from three sources: spread capture, maker rebates, and Polymarket **liquidity rewards** (quadratic score `S = ((v − s)/v)²` per share within max spread `v`, sampled each minute, paid daily).

**Price dynamics.** Bounded prices are modelled as a diffusion in **logit space**, `x = ln(p/(1−p))`, `dx = σ_x dW`.
Locally `σ_p = p(1−p)·σ_x`, so volatility vanishes naturally near 0 and 1.

**Quotes** (Avellaneda & Stoikov 2008, rolling horizon `τ_h = min(H, time_to_close)`):
```
reservation   r = p_mid − I · γ · σ_p² · τ_h               (I = YES-equivalent inventory; NO counts as −YES)
total spread  δ = γ · σ_p² · τ_h + (2/γ) · ln(1 + γ/k)
bid = r − δ/2,  ask = r + δ/2      → clamp to ticks, post-only, ≥ 1 tick inside [0,1]
```
`k` (order-arrival decay) is fitted from trade prints: `λ(δ) = A·e^{−kδ}`. `γ` is the risk-aversion parameter.

**Reward awareness.** If the market pays rewards, quotes are pulled inside `max_spread` when that costs less in expected adverse selection than the reward's expected value. `quant/rewards.py` estimates our share of `Q_min` against the visible book.

**Adverse-selection controls:**
* No quoting within `pre_event_buffer` of `gameStartTime`/`endDate`, and none during live sports unless explicitly enabled.
* A jump detector: |Δmid| over 30 s > κ·σ → pull quotes and cool down.
* Flow toxicity: a signed-volume imbalance over a rolling window (VPIN-like) → widen or pull.
* Markouts: the average mid move against us 5 s/30 s/5 m after fills is tracked per market. Markets with negative net markout (after rewards) are blacklisted.
* Inventory limits: a hard |I| cap per market. Beyond the soft cap only reducing quotes are posted.
* Requote hysteresis: replace only if the target moves ≥ 1 tick, to respect rate limits and queue priority.

## 6. `longshot_fade` — calibration-based fading of overpriced longshots

The favourite–longshot bias is well documented in prediction and betting markets (Wolfers & Zitzewitz 2004; Snowberg & Wolfers 2010; Page & Clemen 2013, who find miscalibration grows with time to expiry). Cheap YES tokens tend to be overpriced.

**Calibration model** (`quant/calibration.py`):
1. Dataset: resolved markets, sampled at fixed horizons before resolution (1 d, 7 d, 30 d). Each row has price p, outcome y ∈ {0,1}, category and horizon.
2. Fit a **monotone** map `g(p)` with isotonic regression (pool-adjacent-violators), per (category, horizon bucket) when n is large enough; otherwise use the global fit.
3. Apply **Beta–binomial shrinkage** per bin toward the identity (`prior mean = p`, prior strength `κ`): `q = (y_sum + κ·p)/(n + κ)`, with standard error from the Beta posterior.

**Trade:** buy NO when `(1 − q) − (a_no + fee) > margin` and the annualised score clears the hurdle.
Typical regime: YES at 2–10¢, short τ, liquid. Tail risk is real (the longshot sometimes wins), so per-event caps, category caps and Kelly shrinkage apply, and the strategy is **disabled until a calibration file exists** (`pmtrader calibrate`).

---

## Strategy lifecycle and contract

```python
class Strategy(Protocol):
    name: str
    needs_judgment: bool                                  # route via LLM gate?
    def detect(self, ctx: ScanContext) -> list[Opportunity]: ...
    def plan_entry(self, opp: Opportunity, budget: SizingBudget) -> list[OrderIntent]: ...
    def manage(self, pos: StrategyPosition, ctx: ScanContext) -> list[OrderIntent]: ...   # exits/requotes
```

`detect` must be **pure given its inputs** (no I/O), so it runs identically in live, paper and backtest.

## Validation standard before a strategy goes live

1. Unit/property tests of the math (`tests/unit/quant`).
2. Paper trading ≥ 2 weeks or ≥ 200 trades, with markouts and P&L attribution reviewed.
3. Backtest/replay on recorded books with the same code path (v0.2).
4. Live at ≤ 10% of the target size for one week, with the kill switch tested.
