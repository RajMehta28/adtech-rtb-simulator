# AdTech RTB Simulator — Guardrail/Validation Layer Extension

*Extension to the original 4-component build (`component1_rtb_simulator.py` through `component4_dashboard_prep.py`), added to give the project the same "governance/validation" story the other sprint build artifacts (Compliance Copilot, Synthetic Edge-Case Simulation Engine) already carry — real, checkable rails, not a design exercise.*

## What was found (not hypothetical — measured against the real data)

Inspecting `component1_rtb_simulator.py`'s `calculate_bid()` surfaced a genuine validation gap: every DSP bidding strategy computes a bid that respects its own `advertiser["max_cpm"]` (and, for the `floor_logic` strategy, `advertiser["floor_cpm"]`) — but ±15% realistic noise is then applied *after* that enforcement, and nothing re-checks the bid afterward. Measured directly against the original `auctions.csv`: **14.4% of all bids exceeded their own advertiser's `max_cpm`**, and **2.3% of Toyota's `floor_logic` bids fell below its own configured floor**. Roughly 1 in 7 bids in the whole dataset violated a constraint the code's own logic claimed to enforce.

## What was built

`guardrails.py` — a new, independent validation module (not a rewrite of the bidding strategies themselves — advertiser behavior is unchanged):

- **`validate_bid()`** — an output rail: re-checks every generated bid against `max_cpm`, `floor_cpm` (floor_logic only), and the active PMP deal floor. Fail-safe: violations are **clipped** to the nearest valid boundary and logged, never silently passed through or allowed to crash the pipeline. A bid of exactly `0.0` (the original code's own "this advertiser chose not to bid" signal) is correctly treated as an abstention, not a floor violation — see the honest bug note below.
- **`detect_budget_anomaly()`** — an execution rail: flags any day where an advertiser's actual spend exceeds 3x its planned daily pace, the same shape of problem as Meta's publicly disclosed Advantage+ budget-drain incidents (10x cost spikes). Detection only, no auto-pause (stated as a next step, not built, to avoid silently cutting off legitimate demand without a human in the loop).
- **Audit logging** — every violation and anomaly is appended to `data/guardrail_audit_log.csv` (event type, advertiser, detail, context, timestamp), the same fail-closed philosophy used in the Compliance Copilot's `audit_log` table.

Wired into `component1_rtb_simulator.py` (bid validation at generation time, both the PMP and open-auction branches) and `component4_dashboard_prep.py` (budget anomaly check added to the existing pacing table as a new `budget_anomaly_flag` column).

## An honest bug in the guardrail itself, caught before shipping

The first version of `validate_bid()` didn't distinguish between a bid of `0.0` meaning "this advertiser abstained" (the original code's own signal) versus a genuine floor violation — it was clipping abstentions *up* to `floor_cpm`, which would have injected fake demand into auctions that were never supposed to have it. This inflated the simulated fill rate from the correct 78.2% to a fabricated 100% and nearly doubled Toyota's win count. Caught by reading the audit log's actual contents before trusting the row count, not by design — fixed by treating `bid == 0.0` as an explicit early-return "no bid" case. Stated here rather than hidden, the same policy used across every build artifact this sprint: show the mistake and the fix, not just the clean final number.

## Measured effect of the (corrected) guardrail on real auction outcomes

Comparing the original `auctions.csv` (100,000 impressions) against the guardrailed rerun, same random seed:

- **66 impressions (0.066%) had a different winner** after the guardrail — in every one of these cases, Toyota's noisy bid had exceeded its own $10.00 `max_cpm` (observed up to $10.57) and won; after clipping to the true max, AmericanExpress correctly outbid it instead.
- **Total revenue changed by -$1,245.94** (from $453,206.19 to $451,960.26, a 0.27% decrease) — revenue *went down*, which is the correct direction: the removed revenue was never legitimate, it came from a bidder winning at a price above what its own strategy should have allowed it to pay.
- Aggregate fill rate, publisher breakdown, and every other advertiser's win count were unaffected — the guardrail's effect is precisely scoped to the actual constraint violations, not a blanket behavior change.

## Budget-pacing anomaly detector — result and why

**0 anomalies flagged** against the real 90-day pacing data (max observed ratio: 1.84x planned pace, threshold: 3x). This is a legitimate result, not a silent failure — verified with a unit test that the detector correctly fires on an injected 5x spike and correctly ignores a 1.5x normal variance. The real reason for 0 flags: this simulation has no automated bidding feedback loop (no auto-scaling budget reallocation, no lookalike-driven bid inflation loop) that could cause a runaway spend day the way Meta's Advantage+ automated systems can — so an honest note for outreach is that the detector is proven correct, but this particular synthetic environment doesn't yet model the failure mode it's built to catch. Stated as a next step: add an optional "automated bid-scaling" mode to the simulator specifically to give the anomaly detector something real to find.

## What's built vs. honest next steps

**Built:** independent bid-constraint validation (fail-safe clip + log) wired into both PMP and open-auction bidding paths; a budget-pacing anomaly detector wired into the dashboard pacing table; a full audit trail; verified against real measured data with a quantified before/after comparison, not just unit tests in isolation.

**Not yet built, stated as next steps:**
- Auto-pause / kill-switch on repeated budget anomalies (currently detection-only by design)
- An "automated bid-scaling" simulation mode so the anomaly detector has a real failure mode to catch, rather than 0 flags on inherently smooth synthetic pacing
- Extending the same independent-verification pattern to Component 2's lookalike model (e.g., checking `recommended_bid_cpm` never exceeds a sane ceiling regardless of `lookalike_score`)
- A dead-code cleanup pass: `Faker()` was imported and instantiated but never called in both `component1` and `component2` — removed from `component1` while wiring in the guardrail (required to actually run the script in this environment, which has no PyPI access); left untouched in `component2` since it wasn't otherwise being modified this session
