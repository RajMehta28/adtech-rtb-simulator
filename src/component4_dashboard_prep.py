import pandas as pd
import numpy as np
from guardrails import detect_budget_anomaly, flush_audit_log

print("Preparing dashboard data...")

# ── LOAD ALL DATA ────────────────────────────────────
df_auctions = pd.read_csv("data/auctions.csv")
df_users    = pd.read_csv("data/users_scored.csv")
df_segments = pd.read_csv("data/segment_analysis.csv")
df_buyers   = pd.read_csv("data/buyer_analysis.csv")
df_floors   = pd.read_csv("data/floor_price_analysis.csv")

# ── CLEAN AUCTIONS ───────────────────────────────────
df_auctions["timestamp"] = pd.to_datetime(
    df_auctions["timestamp"]
)
df_auctions["date"]  = df_auctions["timestamp"].dt.date
df_auctions["hour"]  = df_auctions["timestamp"].dt.hour
df_auctions["week"]  = df_auctions["timestamp"].dt.isocalendar().week
df_auctions["month"] = df_auctions["timestamp"].dt.month
df_auctions["dow"]   = df_auctions["timestamp"].dt.day_name()
df_auctions["filled"] = (
    df_auctions["winner"] != "no_fill"
).astype(int)

# ── TABLE 1: AUCTION DYNAMICS ────────────────────────
# Daily performance by publisher
auction_daily = df_auctions.groupby(
    ["date", "publisher", "ad_format", "device"]
).agg(
    impressions       = ("impression_id", "count"),
    filled            = ("filled", "sum"),
    revenue           = ("revenue", "sum"),
    avg_clearing_cpm  = ("clearing_price", "mean"),
    avg_winning_bid   = ("winning_bid", "mean"),
    clicks            = ("clicked", "sum"),
    conversions       = ("converted", "sum"),
    pmp_impressions   = ("is_pmp", "sum")
).reset_index()

auction_daily["fill_rate"] = (
    auction_daily["filled"] /
    auction_daily["impressions"]
)
auction_daily["ctr"] = np.where(
    auction_daily["impressions"] > 0,
    auction_daily["clicks"] /
    auction_daily["impressions"], 0
)
auction_daily["rpm"] = (
    auction_daily["revenue"] /
    auction_daily["impressions"] * 1000
)
auction_daily["cpa"] = np.where(
    auction_daily["conversions"] > 0,
    auction_daily["revenue"] /
    auction_daily["conversions"], 0
)

auction_daily.to_csv(
    "data/dashboard_auctions.csv", index=False
)
print("✅ Table 1: dashboard_auctions.csv")

# ── TABLE 2: ADVERTISER SCORECARD ────────────────────
advertiser_daily = df_auctions[
    df_auctions["winner"] != "no_fill"
].groupby(["date", "winner", "ad_format"]).agg(
    impressions      = ("impression_id", "count"),
    spend            = ("clearing_price", "sum"),
    avg_cpm          = ("clearing_price", "mean"),
    clicks           = ("clicked", "sum"),
    conversions      = ("converted", "sum"),
    pmp_impressions  = ("is_pmp", "sum")
).reset_index()

advertiser_daily.rename(
    columns={"winner": "advertiser"}, inplace=True
)

advertiser_daily["ctr"] = np.where(
    advertiser_daily["impressions"] > 0,
    advertiser_daily["clicks"] /
    advertiser_daily["impressions"], 0
)
advertiser_daily["cvr"] = np.where(
    advertiser_daily["clicks"] > 0,
    advertiser_daily["conversions"] /
    advertiser_daily["clicks"], 0
)

# Simulate revenue for ROAS calculation
# (assume 8x avg order value vs CPM spend)
advertiser_daily["simulated_revenue"] = (
    advertiser_daily["conversions"] *
    advertiser_daily["avg_cpm"] * 8
)
advertiser_daily["roas"] = np.where(
    advertiser_daily["spend"] > 0,
    advertiser_daily["simulated_revenue"] /
    advertiser_daily["spend"], 0
)

advertiser_daily.to_csv(
    "data/dashboard_advertisers.csv", index=False
)
print("✅ Table 2: dashboard_advertisers.csv")

# ── TABLE 3: PACING VIEW ─────────────────────────────
# Simulate budget pacing over 90-day flight
BUDGETS = {
    "Netflix":         150000,
    "AmericanExpress": 100000,
    "Nike":             50000,
    "Toyota":           40000,
    "Coca_Cola":        30000
}

pacing_records = []
for advertiser, total_budget in BUDGETS.items():
    daily_budget = total_budget / 90

    adv_data = advertiser_daily[
        advertiser_daily["advertiser"] == advertiser
    ].copy()

    if len(adv_data) == 0:
        continue

    adv_daily = adv_data.groupby("date").agg(
        actual_spend = ("spend", "sum")
    ).reset_index()

    adv_daily = adv_daily.sort_values("date")
    adv_daily["cumulative_spend"] = (
        adv_daily["actual_spend"].cumsum()
    )
    adv_daily["planned_cumulative"] = (
        daily_budget *
        np.arange(1, len(adv_daily) + 1)
    )
    adv_daily["pacing_index"] = (
        adv_daily["cumulative_spend"] /
        adv_daily["planned_cumulative"]
    )
    adv_daily["advertiser"]    = advertiser
    adv_daily["total_budget"]  = total_budget
    adv_daily["daily_budget"]  = daily_budget
    adv_daily["remaining_budget"] = (
        total_budget -
        adv_daily["cumulative_spend"]
    )

    pacing_records.append(adv_daily)

df_pacing = pd.concat(pacing_records, ignore_index=True)

# ── GUARDRAIL: budget pacing anomaly detection ───────
# Flags any day where an advertiser's ACTUAL daily spend blew past its
# PLANNED daily pace by 3x or more — same shape of problem as Meta's
# publicly disclosed Advantage+ budget-drain incidents (10x cost spikes).
# Detection only (fail-safe, no auto-pause) — see guardrails.py docstring.
anomaly_flags = []
anomaly_details = []
for _, row in df_pacing.iterrows():
    is_anomaly, detail = detect_budget_anomaly(
        advertiser=row["advertiser"],
        date=str(row["date"]),
        actual_daily_spend=row["actual_spend"],
        planned_daily_budget=row["daily_budget"],
        spike_multiplier=3.0,
    )
    anomaly_flags.append(is_anomaly)
    anomaly_details.append(detail)

df_pacing["budget_anomaly_flag"] = anomaly_flags
df_pacing["budget_anomaly_detail"] = anomaly_details

n_anomalies = sum(anomaly_flags)
n_logged = flush_audit_log()
print(f"   Guardrail: {n_anomalies:,} budget-pacing anomalies flagged "
      f"({n_logged:,} audit events logged)")

df_pacing.to_csv("data/dashboard_pacing.csv", index=False)
print("✅ Table 3: dashboard_pacing.csv (now includes budget_anomaly_flag)")

# ── TABLE 4: INCREMENTALITY SIMULATION ───────────────
np.random.seed(42)

n_per_group = 25000

# Control group — organic conversion rate 3.5%
control = pd.DataFrame({
    "user_id":    range(1, n_per_group + 1),
    "group":      "Control",
    "exposed":    0,
    "converted":  np.random.binomial(1, 0.035, n_per_group),
    "segment":    np.random.choice(
        ["high_value", "lapsed", "general",
         "high_intent", "affluent"],
        n_per_group
    )
})

# Exposed group — ad lifts conversion by 1.5%
exposed = pd.DataFrame({
    "user_id":    range(n_per_group + 1,
                        n_per_group * 2 + 1),
    "group":      "Exposed",
    "exposed":    1,
    "converted":  np.random.binomial(1, 0.050, n_per_group),
    "segment":    np.random.choice(
        ["high_value", "lapsed", "general",
         "high_intent", "affluent"],
        n_per_group
    )
})

df_incrementality = pd.concat(
    [control, exposed], ignore_index=True
)

# Summary by group and segment
incr_summary = df_incrementality.groupby(
    ["group", "segment"]
).agg(
    users        = ("user_id", "count"),
    conversions  = ("converted", "sum"),
    conv_rate    = ("converted", "mean")
).reset_index()

incr_summary["conv_rate_pct"] = (
    incr_summary["conv_rate"] * 100
)

df_incrementality.to_csv(
    "data/dashboard_incrementality.csv", index=False
)
incr_summary.to_csv(
    "data/dashboard_incrementality_summary.csv",
    index=False
)
print("✅ Table 4: dashboard_incrementality.csv")

# ── TABLE 5: SEGMENT PERFORMANCE ─────────────────────
df_segments["bid_premium"] = (
    df_segments["avg_ltv"] / 500
).round(2)
df_segments.to_csv(
    "data/dashboard_segments.csv", index=False
)
print("✅ Table 5: dashboard_segments.csv")

print("\n✅ All dashboard data prepared!")
print("\nFiles ready for Tableau:")
print("   → data/dashboard_auctions.csv")
print("   → data/dashboard_advertisers.csv")
print("   → data/dashboard_pacing.csv")
print("   → data/dashboard_incrementality.csv")
print("   → data/dashboard_incrementality_summary.csv")
print("   → data/dashboard_segments.csv")
print(f"\nDate range: "
      f"{df_auctions['date'].min()} to "
      f"{df_auctions['date'].max()}")
print(f"Total records: {len(df_auctions):,} auctions")