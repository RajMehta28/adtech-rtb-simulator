import pandas as pd
import numpy as np
import sqlite3
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # non-interactive backend for Mac
import warnings
warnings.filterwarnings('ignore')

np.random.seed(42)

# ── LOAD AUCTION DATA ────────────────────────────────
print("Loading auction data...")
df = pd.read_csv("data/auctions.csv")

# Convert timestamp
df["timestamp"] = pd.to_datetime(df["timestamp"])
df["date"]      = df["timestamp"].dt.date
df["hour"]      = df["timestamp"].dt.hour
df["dow"]       = df["timestamp"].dt.day_name()

# Separate filled vs unfilled
df_filled   = df[df["winner"] != "no_fill"].copy()
df_unfilled = df[df["winner"] == "no_fill"].copy()

print(f"✅ Loaded {len(df):,} impressions")
print(f"   Filled:   {len(df_filled):,}")
print(f"   Unfilled: {len(df_unfilled):,}")

# ── 1. FILL RATE ANALYSIS ────────────────────────────
print("\n── FILL RATE BY PUBLISHER ───────────────────────")

fill_by_publisher = df.groupby("publisher").agg(
    total_impressions = ("impression_id", "count"),
    filled            = ("winner",
                         lambda x: (x != "no_fill").sum()),
    total_revenue     = ("revenue", "sum"),
    avg_clearing_price= ("clearing_price", "mean")
).reset_index()

fill_by_publisher["fill_rate"] = (
    fill_by_publisher["filled"] /
    fill_by_publisher["total_impressions"]
)

fill_by_publisher["rpm"] = (
    fill_by_publisher["total_revenue"] /
    fill_by_publisher["total_impressions"] * 1000
)

fill_by_publisher = fill_by_publisher.sort_values(
    "rpm", ascending=False
)

print(fill_by_publisher[[
    "publisher", "total_impressions",
    "fill_rate", "avg_clearing_price", "rpm"
]].round(2).to_string(index=False))

# ── 2. FILL RATE BY AD SLOT + DEVICE ────────────────
print("\n── FILL RATE BY FORMAT + DEVICE ────────────────")

fill_by_format = df.groupby(
    ["ad_format", "device"]
).agg(
    total       = ("impression_id", "count"),
    filled      = ("winner",
                   lambda x: (x != "no_fill").sum()),
    avg_cpm     = ("clearing_price", "mean"),
    revenue     = ("revenue", "sum")
).reset_index()

fill_by_format["fill_rate"] = (
    fill_by_format["filled"] /
    fill_by_format["total"]
)

fill_by_format["rpm"] = (
    fill_by_format["revenue"] /
    fill_by_format["total"] * 1000
)

fill_by_format = fill_by_format.sort_values(
    "rpm", ascending=False
)

print(fill_by_format.round(2).to_string(index=False))

# ── 3. HOURLY PERFORMANCE ────────────────────────────
print("\n── REVENUE BY HOUR OF DAY ───────────────────────")

hourly = df.groupby("hour").agg(
    impressions = ("impression_id", "count"),
    revenue     = ("revenue", "sum"),
    filled      = ("winner",
                   lambda x: (x != "no_fill").sum()),
    avg_cpm     = ("clearing_price", "mean")
).reset_index()

hourly["fill_rate"] = (
    hourly["filled"] / hourly["impressions"]
)
hourly["rpm"] = (
    hourly["revenue"] / hourly["impressions"] * 1000
)

print(hourly[[
    "hour", "impressions", "fill_rate",
    "avg_cpm", "rpm"
]].round(2).to_string(index=False))
# ── 4. FLOOR PRICE OPTIMIZATION ──────────────────────
print("\n── FLOOR PRICE OPTIMIZATION ─────────────────────")
print("Testing 3 floor price strategies...\n")

# Load the raw bids from auction data
# We'll reconstruct bid landscape from clearing prices
# and simulate different floor price scenarios

def simulate_floor_strategy(df, floor_multiplier,
                             strategy_name):
    """
    Test a floor price strategy against auction data.
    floor_multiplier: multiplier on current clearing price
    """
    results = []

    for publisher in df["publisher"].unique():
        pub_df = df[df["publisher"] == publisher].copy()

        # Estimate floor price for this strategy
        avg_clearing = pub_df[
            pub_df["winner"] != "no_fill"
        ]["clearing_price"].mean()

        if pd.isna(avg_clearing):
            continue

        floor = avg_clearing * floor_multiplier

        # Simulate: impressions where clearing >= floor = filled
        filled_mask = (
            pub_df["clearing_price"] >= floor
        ) & (pub_df["winner"] != "no_fill")

        unfilled_mask = (
            pub_df["clearing_price"] < floor
        ) | (pub_df["winner"] == "no_fill")

        filled_count   = filled_mask.sum()
        unfilled_count = unfilled_mask.sum()
        total          = len(pub_df)

        # Revenue = sum of clearing prices above floor
        revenue = pub_df[filled_mask]["clearing_price"].sum()

        fill_rate = filled_count / total if total > 0 else 0
        rpm       = (revenue / total * 1000) if total > 0 \
                    else 0

        results.append({
            "strategy":    strategy_name,
            "publisher":   publisher,
            "floor_price": round(floor, 2),
            "total_imps":  total,
            "filled":      filled_count,
            "unfilled":    unfilled_count,
            "fill_rate":   round(fill_rate, 3),
            "revenue":     round(revenue, 2),
            "rpm":         round(rpm, 2)
        })

    return pd.DataFrame(results)

# Strategy 1: Conservative (60% of avg clearing price)
strat1 = simulate_floor_strategy(
    df, 0.60, "Conservative (60%)"
)

# Strategy 2: Moderate (80% of avg clearing price)
strat2 = simulate_floor_strategy(
    df, 0.80, "Moderate (80%)"
)

# Strategy 3: Aggressive (100% of avg clearing price)
strat3 = simulate_floor_strategy(
    df, 1.00, "Aggressive (100%)"
)

# Combine all strategies
df_strategies = pd.concat(
    [strat1, strat2, strat3], ignore_index=True
)

# Summary by strategy
strategy_summary = df_strategies.groupby("strategy").agg(
    total_revenue = ("revenue", "sum"),
    avg_fill_rate = ("fill_rate", "mean"),
    avg_rpm       = ("rpm", "mean"),
    avg_floor     = ("floor_price", "mean")
).round(2).reset_index()

strategy_summary["revenue_rank"] = (
    strategy_summary["total_revenue"].rank(
        ascending=False
    ).astype(int)
)

print(strategy_summary.to_string(index=False))

# Find winning strategy
best = strategy_summary.loc[
    strategy_summary["total_revenue"].idxmax()
]
print(f"\n🏆 Best strategy: {best['strategy']}")
print(f"   Total revenue:  ${best['total_revenue']:,.2f}")
print(f"   Avg fill rate:  {best['avg_fill_rate']:.1%}")
print(f"   Avg RPM:        ${best['avg_rpm']:.2f}")

# ── 5. TOP BUYERS ANALYSIS ───────────────────────────
print("\n── TOP BUYERS (DSP/ADVERTISER) ANALYSIS ────────")

buyer_analysis = df_filled.groupby("winner").agg(
    impressions_won   = ("impression_id", "count"),
    total_spend       = ("clearing_price", "sum"),
    avg_clearing_cpm  = ("clearing_price", "mean"),
    avg_winning_bid   = ("winning_bid", "mean"),
    total_clicks      = ("clicked", "sum"),
    total_conversions = ("converted", "sum")
).reset_index()

buyer_analysis["win_rate"] = (
    buyer_analysis["impressions_won"] /
    len(df)
)

buyer_analysis["ctr"] = (
    buyer_analysis["total_clicks"] /
    buyer_analysis["impressions_won"]
)

buyer_analysis["cvr"] = np.where(
    buyer_analysis["total_clicks"] > 0,
    buyer_analysis["total_conversions"] /
    buyer_analysis["total_clicks"],
    0
)

buyer_analysis = buyer_analysis.sort_values(
    "total_spend", ascending=False
)

print(buyer_analysis[[
    "winner", "impressions_won", "win_rate",
    "avg_clearing_cpm", "total_spend",
    "ctr", "cvr"
]].round(4).to_string(index=False))

df_strategies.to_csv(
    "data/floor_price_analysis.csv", index=False
)
buyer_analysis.to_csv(
    "data/buyer_analysis.csv", index=False
)
# ── 6. GENERATE CHARTS ───────────────────────────────
print("\nGenerating charts...")

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle(
    "SSP Yield Optimization Analysis",
    fontsize=16, fontweight="bold"
)

# Chart 1: RPM by Publisher
ax1 = axes[0, 0]
pub_sorted = fill_by_publisher.sort_values("rpm")
ax1.barh(
    pub_sorted["publisher"],
    pub_sorted["rpm"],
    color="steelblue"
)
ax1.set_xlabel("RPM ($)")
ax1.set_title("Revenue Per Mille by Publisher")
ax1.axvline(
    fill_by_publisher["rpm"].mean(),
    color="red", linestyle="--",
    label=f'Avg: ${fill_by_publisher["rpm"].mean():.2f}'
)
ax1.legend()

# Chart 2: Fill Rate by Publisher
ax2 = axes[0, 1]
colors = [
    "green" if r >= 0.80 else
    "orange" if r >= 0.70 else "red"
    for r in pub_sorted["fill_rate"]
]
ax2.barh(
    pub_sorted["publisher"],
    pub_sorted["fill_rate"],
    color=colors
)
ax2.set_xlabel("Fill Rate")
ax2.set_title("Fill Rate by Publisher")
ax2.axvline(0.80, color="green",
            linestyle="--", label="80% target")
ax2.legend()
ax2.xaxis.set_major_formatter(
    plt.FuncFormatter(lambda x, _: f"{x:.0%}")
)

# Chart 3: Floor Price Strategy Comparison
ax3 = axes[1, 0]
strategies  = strategy_summary["strategy"].tolist()
revenues    = strategy_summary["total_revenue"].tolist()
fill_rates  = strategy_summary["avg_fill_rate"].tolist()

x     = np.arange(len(strategies))
width = 0.35

bars1 = ax3.bar(
    x - width/2, revenues, width,
    label="Revenue ($)", color="steelblue"
)
ax3.set_ylabel("Total Revenue ($)")
ax3.set_title("Floor Price Strategy: Revenue vs Fill Rate")
ax3.set_xticks(x)
ax3.set_xticklabels(
    [s.split("(")[0].strip() for s in strategies],
    rotation=10
)

ax3b = ax3.twinx()
ax3b.plot(
    x, fill_rates, "ro-",
    linewidth=2, markersize=8, label="Fill Rate"
)
ax3b.set_ylabel("Fill Rate")
ax3b.yaxis.set_major_formatter(
    plt.FuncFormatter(lambda x, _: f"{x:.0%}")
)

ax3.legend(loc="upper left")
ax3b.legend(loc="upper right")

# Chart 4: Hourly RPM Heatmap
ax4 = axes[1, 1]
ax4.plot(
    hourly["hour"],
    hourly["rpm"],
    "b-o", linewidth=2, markersize=5
)
ax4.fill_between(
    hourly["hour"],
    hourly["rpm"],
    alpha=0.3
)
ax4.set_xlabel("Hour of Day")
ax4.set_ylabel("RPM ($)")
ax4.set_title("RPM by Hour of Day")
ax4.set_xticks(range(0, 24, 2))
ax4.grid(True, alpha=0.3)

# Highlight prime time
ax4.axvspan(
    18, 23, alpha=0.1,
    color="gold", label="Prime time"
)
ax4.legend()

plt.tight_layout()
plt.savefig(
    "data/ssp_yield_analysis.png",
    dpi=150, bbox_inches="tight"
)
print("✅ Chart saved: data/ssp_yield_analysis.png")

# ── FINAL SUMMARY ────────────────────────────────────
print("\n✅ Component 3 Complete!")
print("   Files saved:")
print("   → data/floor_price_analysis.csv")
print("   → data/buyer_analysis.csv")
print("   → data/ssp_yield_analysis.png")
print("\n── COMPONENT 3 SUMMARY ─────────────────────────")
print(f"   Publishers analyzed:    "
      f"{df['publisher'].nunique()}")
print(f"   Floor strategies tested: 3")
print(f"   Best strategy:          "
      f"{best['strategy']}")
print(f"   Overall fill rate:      "
      f"{len(df_filled)/len(df):.1%}")
print(f"   Total SSP revenue:      "
      f"${df_filled['revenue'].sum():,.2f}")