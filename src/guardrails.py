"""
Guardrail / validation layer for the AdTech RTB Simulation & Optimization Engine.
====================================================================================
Added as an extension to the original 4-component build. Found through direct
inspection of component1_rtb_simulator.py: every DSP bidding strategy computes
a bid that respects its own advertiser['max_cpm'] (and, for floor_logic,
advertiser['floor_cpm']) — but then applies +/-15% realistic noise AFTER that
enforcement (calculate_bid(), lines ~221-223 of the original file). Nothing
re-checks the bid after noise is applied, so the strategy's own stated
constraint isn't actually guaranteed.

Measured against the original 100K-impression auctions.csv: 14.4% of all
bids exceeded their own advertiser's max_cpm, and 2.3% of Toyota's
floor_logic bids fell below its own configured floor_cpm. This is a real,
substantial validation gap in the original code, not a hypothetical one —
the same class of problem as "the model was told X" vs. "the system
verified X actually happened."

Design mirrors the fail-safe philosophy used elsewhere in this sprint's
build artifacts: every rail here CLIPS AND LOGS rather than crashes or
silently allows a violation through. Nothing here changes bidding
*strategy* (advertisers still behave exactly as configured) — it only
enforces, after the fact, the constraints the strategies themselves
already claimed to have.
"""

from __future__ import annotations

import csv
from pathlib import Path
from datetime import datetime

AUDIT_LOG_PATH = Path("data/guardrail_audit_log.csv")

_audit_buffer: list[dict] = []


def log_guardrail_event(event_type: str, advertiser: str, detail: str, context: str = "") -> None:
    """Buffers an audit event; call flush_audit_log() to persist."""
    _audit_buffer.append(
        {
            "logged_at": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "advertiser": advertiser,
            "detail": detail,
            "context": context,
        }
    )


def flush_audit_log(path: Path = AUDIT_LOG_PATH) -> int:
    """Writes all buffered audit events to CSV (append mode) and clears the buffer."""
    if not _audit_buffer:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["logged_at", "event_type", "advertiser", "detail", "context"])
        if write_header:
            writer.writeheader()
        writer.writerows(_audit_buffer)
    n = len(_audit_buffer)
    _audit_buffer.clear()
    return n


# ── INPUT/OUTPUT RAIL: bid validation ────────────────────────────────────
def validate_bid(
    advertiser_name: str,
    advertiser_cfg: dict,
    bid: float,
    pmp_floor: float | None = None,
    context: str = "",
) -> tuple[float, bool, str]:
    """
    Independently re-checks a generated bid against the constraints its
    OWN strategy config claims to enforce (max_cpm, floor_cpm for
    floor_logic strategy, and any active PMP deal floor).

    Fail-safe design: on any violation, the bid is CLIPPED to the nearest
    valid boundary (not silently passed through, not thrown away as a
    crash) and the event is logged to the audit trail. A PMP-floor
    violation is treated differently — clipping up to the floor would
    invent demand that wasn't real, so it's zeroed out (equivalent to
    "this bidder doesn't qualify for this deal"), same as component1's
    own existing PMP fallback logic.

    Returns (validated_bid, was_valid, reason).
    """
    reasons = []
    validated_bid = bid

    if bid is None or bid != bid or bid < 0:  # NaN check: bid != bid is True only for NaN
        log_guardrail_event("invalid_bid_value", advertiser_name, f"bid={bid!r}", context)
        return 0.0, False, "invalid bid value (negative or NaN)"

    # A bid of exactly 0.0 is the original code's own signal for "this
    # advertiser chose not to bid" (e.g. calculate_bid()'s format-floor
    # check). That's a legitimate abstention, not a floor violation — an
    # earlier version of this guardrail incorrectly clipped it UP to
    # floor_cpm, which would have injected fake demand into auctions that
    # were never supposed to have it. Caught by inspecting the audit log
    # before shipping, not by design — worth stating honestly.
    if bid == 0.0:
        return 0.0, True, "no bid (abstained)"

    max_cpm = advertiser_cfg.get("max_cpm")
    if max_cpm is not None and bid > max_cpm:
        reasons.append(f"exceeded max_cpm ({bid:.4f} > {max_cpm:.2f})")
        validated_bid = max_cpm

    strategy = advertiser_cfg.get("strategy")
    floor_cpm = advertiser_cfg.get("floor_cpm")
    if strategy == "floor_logic" and floor_cpm is not None and bid < floor_cpm:
        reasons.append(f"fell below strategy floor_cpm ({bid:.4f} < {floor_cpm:.2f})")
        validated_bid = max(validated_bid, floor_cpm)

    if pmp_floor is not None and bid < pmp_floor:
        reasons.append(f"below PMP deal floor ({bid:.4f} < {pmp_floor:.2f}) — excluded from PMP eligibility")
        validated_bid = 0.0

    if reasons:
        log_guardrail_event("bid_constraint_violation", advertiser_name, "; ".join(reasons), context)
        return validated_bid, False, "; ".join(reasons)

    return validated_bid, True, "ok"


# ── EXECUTION RAIL: budget pacing anomaly detection ──────────────────────
def detect_budget_anomaly(
    advertiser: str,
    date: str,
    actual_daily_spend: float,
    planned_daily_budget: float,
    spike_multiplier: float = 3.0,
) -> tuple[bool, str]:
    """
    Flags a day where an advertiser's actual spend blew past its planned
    daily pace by more than `spike_multiplier`x — the same shape of
    problem as Meta's publicly disclosed Advantage+ budget-drain
    incidents (10x cost spikes). This is deliberately a simple, auditable
    ratio-based check (not a black-box anomaly model) so every flag can
    be explained in one sentence, which matters more than sophistication
    for a guardrail whose job is to be trusted.

    Fail-safe: this function only DETECTS and LOGS. It does not
    automatically pause spend — pairing detection with an automatic kill
    switch is listed as a stated next step, not built here, to avoid
    silently cutting off legitimate demand spikes without a human in the
    loop.
    """
    if planned_daily_budget <= 0:
        return False, "no planned budget to compare against"

    ratio = actual_daily_spend / planned_daily_budget
    if ratio >= spike_multiplier:
        detail = f"spend ${actual_daily_spend:,.2f} vs planned ${planned_daily_budget:,.2f} ({ratio:.1f}x pace)"
        log_guardrail_event("budget_pacing_anomaly", advertiser, detail, date)
        return True, detail

    return False, f"{ratio:.2f}x pace — within normal range"
