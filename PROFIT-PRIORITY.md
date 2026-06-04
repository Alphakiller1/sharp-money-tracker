# PROFIT-PRIORITY

Roadmap for the most profitable MLB version of the sharp tracker: identify executable cross-venue arbitrage, enter early on validated MLB line-movement opportunities, and convert those positions into locked profit when the hedge appears.

This is analysis infrastructure, not betting advice.

## Objective

Build the tracker into a profit-first MLB system that separates three opportunity types:

1. Pure arbitrage: both sides can be executed immediately for guaranteed profit.
2. Cross-venue value: one venue is materially cheaper than a trusted fair-price reference.
3. Manufactured arbitrage: enter one side before expected movement, then hedge the opposite side after the market moves.

The highest-profit operating model is likely:

1. Detect sharp-backed early entries.
2. Enter only when price is still available at a slow venue.
3. Monitor sportsbook and prediction-market hedge prices.
4. Convert to guaranteed profit when the hedge price appears.
5. If no hedge appears, hold only positions with positive CLV and validated historical edge.
6. Track every attempted, missed, hedged, and failed opportunity.

## Phase 1: Execution-Grade Data

The current tracker is strong for research but still uses some theoretical prices. The next version must store executable prices.

Required sportsbook data:

- Book key.
- Market type.
- Selection.
- Line.
- American odds.
- Decimal odds.
- Implied probability.
- Snapshot time.
- Price age.
- Whether the line is currently available.
- Book-specific limit if known.

Required prediction-market data:

- Venue.
- Ticker.
- Selection.
- Yes bid.
- Yes ask.
- No bid.
- No ask.
- Last price.
- Spread.
- Liquidity.
- Volume.
- Open interest.
- Fee estimate.
- Snapshot time.
- Price age.

Reject opportunity candidates when:

- Either side is stale.
- The game is too close to start for safe execution.
- The cross-venue gap is implausibly large.
- The line, team, or market mapping is ambiguous.
- The hedge leg has insufficient liquidity.
- Fees and slippage erase the edge.

## Phase 2: Executable Arb Calculator

Create a calculator that answers one question:

```text
Can both legs be executed now for guaranteed profit after fees and slippage?
```

Core condition:

```text
cost_side_a + cost_side_b + fees + slippage_buffer < 1
```

Output fields:

- Game.
- Market type.
- Side A selection.
- Side A venue/book.
- Side A executable cost.
- Side B selection.
- Side B venue/book.
- Side B executable cost.
- Recommended stake A.
- Recommended stake B.
- Required bankroll.
- Guaranteed profit.
- Guaranteed ROI.
- Price age.
- Liquidity warning.
- Stale-line warning.
- Execution confidence.

Minimum thresholds:

- Pure arb minimum guaranteed ROI: 1.0% after all costs.
- Thin or stale market arb minimum ROI: 2.0% to 3.0%.
- Reject anything below threshold unless used only for monitoring.

## Phase 3: Manufactured Arb Engine

Manufactured arbitrage is a staged strategy. It is not locked profit at entry.

For each candidate, store:

- Entry side.
- Entry venue/book.
- Entry price.
- Entry time.
- Reason for entry.
- Expected direction of move.
- Target hedge side.
- Target hedge price.
- Current hedge price.
- Current guaranteed ROI if hedged now.
- Current CLV.
- Max hold time.
- Stop-loss price.
- Hedge deadline.
- Unhedged EV.
- Worst-case exposure.

Entry should require multiple confirmations:

- Sharp-vs-soft divergence above threshold.
- Line moving toward the entry side.
- Prediction market agrees or lags in a useful way.
- Commercial book price is still behind the sharp/prediction-market signal.
- Historical segment has positive ROI and positive CLV.
- Liquidity is sufficient for later hedge.

Initial strongest patterns from the current tracker:

- Underdog plus steam up.
- Steam up by at least 4 probability points.
- Steam up by at least 2 probability points.

Initial entry rule:

```text
Enter if:
  side is an underdog at open probability < 0.45
  and movement toward side >= 0.02
  and sharp divergence >= 0.02
  and stale/slow venue still offers old price
  and expected hedge path exists
  and modeled downside is within stake limits
```

## Phase 4: Hedge Logic

The live system should constantly evaluate open positions.

Questions to answer:

- Can we lock profit now?
- Can we partially hedge to remove stake risk?
- Is the position still positive CLV?
- Has the signal failed?
- Is the game close enough that we must exit or hedge?

Full hedge trigger:

```text
Hedge if guaranteed ROI >= configured threshold after fees and slippage.
```

Suggested thresholds:

- Full hedge: guaranteed ROI >= 1.5%.
- High-confidence staged arb: guaranteed ROI >= 1.0%.
- Thin market staged arb: guaranteed ROI >= 2.5%.

Partial hedge trigger:

```text
If full hedge is unavailable but risk can be reduced without killing positive EV,
hedge enough to recover stake or cap downside.
```

Position outcomes:

- Full hedge: guaranteed profit.
- Partial hedge: reduced downside with remaining upside.
- Hold: no hedge yet, but CLV and signal remain valid.
- Exit: signal invalidated, line reversed, or deadline reached.

## Phase 5: Backtest The Actual Workflow

The current backtest validates line-movement segments. The next backtest must simulate the actual staged workflow.

For every historical opportunity:

```text
At time T:
  Would the system have entered?

At time T+n:
  Did a hedge opportunity appear?

If hedge appeared:
  What was the locked profit?

If hedge did not appear:
  What was the unhedged ROI?
  What was the max adverse move?
```

Required metrics:

- Entry count.
- Hedge conversion rate.
- Average locked ROI.
- Median locked ROI.
- Unhedged ROI.
- Blended ROI.
- CLV.
- Max drawdown.
- Average hold time.
- Capital tied up.
- Profit per bankroll-hour.
- Stale-line false positive rate.
- Missed hedge rate.

Primary metric:

```text
profit per bankroll-hour
```

This matters more than raw ROI because staged arbs tie up capital while waiting for movement.

## Algo Theory

The algorithm should model each opportunity as a state machine, not a single signal.

### State Machine

```text
WATCHLIST -> ENTRY_CANDIDATE -> ENTERED -> HEDGE_AVAILABLE -> HEDGED
                                   |             |
                                   |             -> PARTIAL_HEDGED
                                   |
                                   -> HOLD
                                   -> EXIT
                                   -> EXPIRED
```

State definitions:

- WATCHLIST: game has live odds and prediction-market data.
- ENTRY_CANDIDATE: one side has a positive expected move and an exploitable entry price.
- ENTERED: system has simulated or recorded an entry.
- HEDGE_AVAILABLE: opposite side can lock profit above threshold.
- HEDGED: full arb was created.
- PARTIAL_HEDGED: downside was reduced, but outcome is not fully locked.
- HOLD: no hedge yet, but CLV and signal remain valid.
- EXIT: signal failed or stop-loss triggered.
- EXPIRED: game started or market closed before hedge.

### Opportunity Classes

Class A: Pure Arb

```text
edge = 1 - executable_cost_side_a - executable_cost_side_b - fees - slippage
```

Accept when:

```text
edge >= min_pure_arb_edge
and both legs are fresh
and liquidity supports stake size
and market mapping confidence is high
```

Class B: Cross-Venue Value

```text
value_edge = fair_probability_reference - executable_entry_cost
```

Accept when:

```text
value_edge >= min_value_edge
and reference venue is historically calibrated
and no pure hedge exists yet
```

Class C: Manufactured Arb Candidate

```text
expected_move = predicted_future_probability - executable_entry_cost
hedge_gap_needed = target_hedge_cost - current_hedge_cost
conversion_prob = probability hedge appears before deadline
```

Accept when:

```text
expected_move * conversion_prob - downside_risk > min_required_ev
```

### Signal Inputs

Use these features to estimate expected movement and hedge probability:

- Sharp-vs-soft divergence.
- Number of sharp books supporting the side.
- Current line delta.
- Rate of line movement.
- Whether movement happened across multiple books.
- Whether Kalshi leads or lags books.
- Kalshi liquidity.
- Kalshi spread.
- Book count offering stale/better price.
- Time to first pitch.
- Opening probability bucket.
- Favorite/underdog role.
- Historical ROI for matching segment.
- Historical CLV for matching segment.
- Prior hedge conversion rate for matching segment.

### Scoring Function

Start with an interpretable score before moving to a trained model.

```text
score =
  historical_segment_roi_weight
  + historical_clv_weight
  + sharp_divergence_weight
  + steam_weight
  + cross_venue_gap_weight
  + book_lag_weight
  + liquidity_weight
  + hedge_path_weight
  - spread_cost_penalty
  - fee_penalty
  - stale_price_penalty
  - time_decay_penalty
  - hedge_failure_penalty
```

Suggested first-pass implementation:

```text
score =
  2.0 * historical_roi
  + 1.5 * historical_clv
  + 1.0 * sharp_divergence
  + 0.75 * current_move
  + 0.75 * cross_venue_gap
  + 0.50 * book_lag
  + 0.25 * liquidity_score
  - 1.0 * spread_cost
  - 1.0 * fee_cost
  - 1.5 * stale_risk
  - 1.0 * hedge_failure_risk
```

Rank outputs:

- A: enter now, strong hedge probability.
- B: monitor, enter only if price improves.
- C: value only, no clear hedge path.
- D: reject.

### Hedge Price Math

For a binary market with two sides, the position becomes a lock when:

```text
entry_cost + hedge_cost + fees + slippage < 1
```

Target hedge cost:

```text
target_hedge_cost = 1 - entry_cost - fees - slippage - desired_profit
```

If the opposite side can be bought at or below `target_hedge_cost`, full hedge is allowed.

### Stake Sizing

For equalized payout:

```text
stake_a * payout_a = stake_b * payout_b
```

For binary contracts where payout is 1 per contract:

```text
contracts_a = target_payout
contracts_b = target_payout
cost_a = contracts_a * entry_cost
cost_b = contracts_b * hedge_cost
profit = target_payout - cost_a - cost_b - fees
```

For sportsbook decimal odds:

```text
payout = stake * decimal_odds
```

Set stakes so both outcomes return the same net profit.

### Risk Controls

Reject or downgrade candidates when:

- Price age exceeds threshold.
- Book price is unavailable on recheck.
- Market mapping confidence is low.
- Gap is above plausible max.
- Kalshi spread is too wide.
- Liquidity is too low for target stake.
- Time to first pitch is too short.
- Signal depends on only one sharp book.
- Movement has already fully occurred.
- Hedge path requires an unrealistic opposite-side price.

### Learning Loop

Every candidate should be logged whether entered or not.

Log:

- Detected time.
- Features.
- Score.
- Recommendation.
- Best entry price.
- Best hedge price over time.
- Whether hedge threshold appeared.
- Time to hedge.
- Final outcome.
- Simulated profit.
- Realized profit if actually traded.

Then update:

- Segment ROI.
- Hedge conversion probability.
- Stale false-positive rate.
- Book lag score.
- Venue calibration.
- Score weights.

## Phase 6: Live Dashboard

The dashboard should have four profit-priority panels.

Pure Arbs:

- Immediate executable locks only.
- Sorted by guaranteed ROI.
- Requires no opinion about line movement.

Manufactured Arb Candidates:

- Best early entries likely to move.
- Sorted by expected profit per bankroll-hour.
- Includes target hedge price.

Open Positions:

- Entry price.
- Current hedge price.
- Current guaranteed ROI if hedged.
- CLV.
- Time since entry.
- Hedge/hold/exit recommendation.

Postmortem:

- Did the move happen?
- Did hedge appear?
- Was entry price good?
- Was the edge real or stale?
- What would the optimal action have been?

## Scraping Prospects

Use official APIs or structured feeds first. Scrape public HTML only when permitted by the site's terms, rate limits are respected, and the data cannot be obtained through a cleaner API. Keep feeds MLB-specific unless they directly improve MLB opportunity detection.

### Tier 1: Core Price Feeds

These directly contribute to MLB arb and manufactured-arb detection.

- The Odds API MLB endpoint: current MLB sportsbook odds, market snapshots, book-level prices, historical MLB odds if paid access is enabled.
- Kalshi MLB series: game winner, first-five totals, pitcher strikeout ladders, bid/ask, liquidity, volume, ticker metadata.
- Polymarket API/subgraph: MLB markets only where coverage exists, order book, liquidity, volume, event metadata.
- Sporttrade or exchange-style MLB markets if accessible: bid/ask-style sports pricing, useful for executable hedge references.
- Pinnacle-compatible MLB feeds if legally/API accessible: sharp reference price, low-vig fair probability, market movement anchor.

Implementation priority:

1. Normalize every source into the same side/market schema.
2. Store executable bid/ask or best book price.
3. Record snapshot time and price age.
4. Preserve venue-specific IDs for later rechecks.

### Tier 2: Line Movement And Market Context

These help identify slow books, MLB steam, and whether a move is real.

- OddsPortal-style MLB historical odds pages: open, close, and book-specific movement where available.
- Covers MLB consensus pages: public-facing consensus, line movement, matchup context.
- Action Network-style MLB market pages: public betting percentages, money splits, steam indicators where available.
- VSIN or similar MLB market-report pages: qualitative sharp-move notes.
- MLB odds-comparison pages: cross-book moneyline, run line, total, first-five, and player-prop price displays.

Use these as secondary context unless the data is timestamped and reproducible.

### Tier 3: Sports And Game State Inputs

These improve MLB line-movement prediction and false-positive filtering.

- MLB Stats API: probable pitchers, starting lineups, game status, weather-neutral official game metadata.
- Baseball Savant: pitcher/batter quality, Statcast indicators, matchup features.
- Fangraphs: projected starters, team metrics, bullpen quality, player projections.
- Rotowire / RotoGrinders / FantasyLabs-style pages: lineup alerts, injury/news context, confirmed starters.
- Weather feeds: wind, temperature, precipitation, roof status where relevant.

These should not directly create arb signals. They should explain or filter movement.

### Tier 4: News And Alert Catalysts

These can explain why an MLB line is about to move.

- Beat writer RSS/X lists where accessible.
- Team transaction pages.
- MLB official probable pitcher and lineup updates.
- Injury/news aggregators.
- Weather alert feeds.

MLB-specific catalyst examples:

- Probable pitcher scratched.
- Starting lineup confirmed with star hitter resting.
- Catcher change for a pitcher-sensitive matchup.
- Bullpen exhaustion after extra-inning or high-leverage usage.
- Wind shift at Wrigley Field or another weather-sensitive park.
- Roof open/closed update.
- Late steam on first-five total after pitcher/news confirmation.

Use catalyst data for alert confidence, not standalone betting decisions.

### Scrape Quality Rules

Every scraper should emit:

- Source.
- URL or API endpoint.
- Fetch time.
- Event ID.
- Normalized game key.
- Market type.
- Selection.
- Price.
- Line.
- Price age if known.
- Parse confidence.
- Error/warning field.

Reject or quarantine rows when:

- Team mapping is uncertain.
- Market naming is ambiguous.
- Timestamp is missing for time-sensitive prices.
- The price differs too far from all other venues.
- The page structure changed.
- The source is known to lag.

## Research Papers And Theory Sources

The algorithm should be guided by betting-market literature, but implementation should stay MLB-specific. Use broader sports-betting papers for theory, then validate every claim on MLB moneyline, run line, total, first-five, and pitcher-prop data.

### Market Efficiency And Closing Lines

- "How quickly is temporary market inefficiency removed?" by Franck, Verbeek, and Nuesch. Use for the idea that internet sportsbook arbitrage exists but is removed quickly, so speed and simultaneity matter.
- "Betting market efficiency and prediction in binary choice models" by Angelini and De Angelis. Use for fixed-odds market efficiency testing and probability calibration.
- "Weak Form Efficiency in Sports Betting Markets" by Paul and Weinbach. Use for testing whether past prices/movement contain exploitable information.
- Closing-line efficiency research and CLV studies. Use to validate whether beating the close is a leading indicator of long-run profitability.

### Favorite-Longshot Bias

- "Are Sports Bettors Biased toward Longshots, Favorites, or Both? A Literature Review" by Newall and Cortis. Use for favorite-longshot bias across sports and markets.
- "A favorite-longshot bias in fixed-odds betting markets: Evidence from college basketball and college football" by Paul and Weinbach. Use for moneyline bias structure.
- "What drives biased odds in sports betting markets: Bettors' irrationality and the role of bookmakers" by Angelini, De Angelis, and Singleton. Use for separating bettor-demand effects from bookmaker pricing effects.

MLB model implication:

```text
Do not assume every underdog move is valuable.
Segment by favorite/underdog role, opening probability, starting pitcher quality, bullpen state, park, book type, and liquidity.
```

### Prediction Markets And Cross-Venue Arbitrage

- "Arbitrage Analysis in Polymarket NBA Markets" by Chen et al. Use for prediction-market microstructure, latency, and algorithmic arb framing.
- Wolfers and Zitzewitz prediction-market work. Use for information aggregation and when real-money markets act as probability references.
- Hanson/Oprea/Porter information aggregation and manipulation work. Use for the idea that attempted manipulation can create incentives for informed correction.

MLB model implication:

```text
Prediction-market price is useful only when spread, liquidity, and recency are strong.
Mid price is research data; MLB yes/no ask price is execution data.
```

### Backtesting And Multiple Testing

Use research discipline from financial-market testing, but evaluate MLB separately by market:

- Avoid cherry-picking segments.
- Apply false-discovery control when scanning many conditions.
- Track out-of-sample performance separately.
- Measure profit at executable entry price, not only closing-line agreement.
- Report confidence intervals and drawdown, not only ROI.
- Do not combine MLB moneyline, run line, totals, first-five, and pitcher props into one performance bucket.

## Alert System

The alert system should tell the user when to enter, hedge, hold, or exit MLB positions. Alerts should be sparse, high-confidence, and actionable.

### Alert Types

Pure Arb Alert:

```text
Trigger when both sides can be executed now and guaranteed ROI clears threshold.
```

Entry Alert:

```text
Trigger when a manufactured-arb candidate reaches A-grade score and entry price is still available.
```

Hedge Alert:

```text
Trigger when an open position can be fully hedged for guaranteed profit.
```

Partial Hedge Alert:

```text
Trigger when downside can be reduced while preserving positive expected value.
```

Exit Alert:

```text
Trigger when line reverses, signal invalidates, stale risk rises, or hedge deadline arrives.
```

Watch Alert:

```text
Trigger when a B-grade candidate is close to entry but needs one more condition.
```

### Alert Channels

Start with low-complexity channels:

- Local console output for dev.
- Dashboard panel.
- Discord webhook.
- Email.
- SMS/push notification only for A-grade entry, pure arb, and hedge alerts.

Suggested message format:

```text
[ENTRY A] ARI ML
Buy: ARI @ Kalshi 0.407 ask
Reason: dog + steam up, sharp div +6.4%, book lag +5.1%
Target hedge: LAD <= 0.575
Max hold: 42 min
Risk: $100 entry, no lock yet
Action: buy entry side, then monitor hedge
```

Hedge message format:

```text
[HEDGE] ARI/LAD
Entry: ARI 0.407
Hedge: LAD 0.568
Locked ROI: +2.1% after fees
Action: hedge now
```

Pure arb message format:

```text
[PURE ARB] KCR/CIN
Leg 1: KCR Kalshi ask 0.405
Leg 2: CIN sportsbook +155
Locked ROI: +1.8% after fees
Price age: 18s / 22s
Action: execute both legs immediately
```

### Alert Throttling

Avoid noisy alerts:

- One entry alert per game/side unless score improves materially.
- Hedge alerts repeat only if locked ROI improves or deadline approaches.
- Watch alerts go only to dashboard, not SMS.
- Suppress alerts after game start or market close.
- Suppress candidates with stale or unconfirmed prices.

### Alert Severity

- Critical: pure arb or hedge available now.
- High: A-grade manufactured entry.
- Medium: B-grade watch candidate.
- Low: dashboard-only context or postmortem note.

### Alert Decision Contract

Every alert must include:

- Exact action.
- Exact venue/book.
- Exact price.
- Stake suggestion.
- Target hedge price.
- Time sensitivity.
- Why the alert fired.
- Why it could be wrong.

## Build Priority

1. Executable arb math.
2. Kalshi yes/no ask handling and fee model.
3. Best sportsbook executable price by side.
4. Stale-line and mapping-confidence checks.
5. Position tracker for staged entries.
6. Hedge monitor.
7. Manufactured-arb backtest.
8. Live dashboard panels.
9. Alerting.
10. Automation only after profitable paper-trading results.

## Definition Of Done

The system is ready for serious paper trading when it can:

- Distinguish pure arb from value from manufactured arb.
- Use executable prices instead of midpoints or medians.
- Include fees, spreads, and stale-line checks.
- Produce stake sizes for equalized payout.
- Track open staged positions.
- Identify hedge availability in real time.
- Backtest the full staged workflow.
- Report profit per bankroll-hour.
- Produce a postmortem for every candidate.
