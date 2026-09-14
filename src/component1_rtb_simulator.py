import numpy as np
import pandas as pd
import random
import csv
from datetime import datetime, timedelta
from guardrails import validate_bid, flush_audit_log

# NOTE: the original file imported and instantiated Faker() here but never
# called it anywhere in the script (verified by grep) — removed as a dead,
# unused dependency while wiring in the guardrail layer below. No behavior
# change; this was never invoked.
np.random.seed(42)
random.seed(42)

# ── CONFIG ──────────────────────────────────────────
NUM_IMPRESSIONS = 100_000

PUBLISHERS = [
    "espn.com", "cnn.com", "nytimes.com",
    "weather.com", "reddit.com", "buzzfeed.com"
]

AD_FORMATS = ["display", "video", "native", "mobile", "ctv"]

FORMAT_WEIGHTS = [0.45, 0.12, 0.08, 0.30, 0.05]

DEVICES = ["desktop", "mobile", "tablet", "ctv"]
DEVICE_WEIGHTS = [0.35, 0.45, 0.10, 0.10]

GEOS = ["NYC", "LA", "Chicago", "Houston",
        "Phoenix", "Philadelphia", "San Antonio"]

# ── REALISTIC CPM FLOORS BY FORMAT ──────────────────
FORMAT_CPM_FLOOR = {
    "display": 1.0,
    "video":   15.0,
    "native":  5.0,
    "mobile":  0.5,
    "ctv":     30.0
}

# ── REALISTIC CTR BY DEVICE ─────────────────────────
DEVICE_CTR = {
    "desktop": 0.0008,
    "mobile":  0.0015,
    "tablet":  0.0010,
    "ctv":     0.0003
}

# ── TIME OF DAY DISTRIBUTION ────────────────────────
# Higher weight = more traffic at that hour
HOUR_WEIGHTS = [
    0.5, 0.3, 0.2, 0.2, 0.2, 0.3,   # 00-05 (night, low)
    0.5, 0.8, 1.2, 1.4, 1.5, 1.5,   # 06-11 (morning ramp)
    1.4, 1.3, 1.3, 1.4, 1.5, 1.6,   # 12-17 (afternoon)
    1.8, 1.9, 1.7, 1.4, 1.0, 0.7    # 18-23 (prime time)
]
# ── ADVERTISERS + BIDDING STRATEGIES ────────────────
# Each advertiser has a different DSP bidding strategy

ADVERTISERS = {
    "Nike": {
        "strategy":   "dynamic",
        "base_cpm":   3.50,
        "max_cpm":    12.00,
        "target_segments": ["sports_fan", "in_market_shoes", "high_income"],
        "pmp_publishers":  ["espn.com"]
    },
    "Coca_Cola": {
        "strategy":   "fixed",
        "base_cpm":   2.00,
        "max_cpm":    2.00,
        "target_segments": ["general_audience", "food_beverage"],
        "pmp_publishers":  []
    },
    "AmericanExpress": {
        "strategy":   "dynamic",
        "base_cpm":   5.00,
        "max_cpm":    18.00,
        "target_segments": ["high_income", "frequent_traveler", "in_market_finance"],
        "pmp_publishers":  ["nytimes.com", "cnn.com"]
    },
    "Toyota": {
        "strategy":   "floor_logic",
        "base_cpm":   3.00,
        "max_cpm":    10.00,
        "floor_cpm":  1.50,
        "target_segments": ["in_market_auto", "suburban", "high_income"],
        "pmp_publishers":  []
    },
    "Netflix": {
        "strategy":   "dynamic",
        "base_cpm":   4.00,
        "max_cpm":    25.00,
        "target_segments": ["entertainment", "streaming_user", "general_audience"],
        "pmp_publishers":  ["reddit.com"]
    }
}

# ── PMP DEALS ────────────────────────────────────────
PMP_DEALS = {
    "espn.com": {
        "floor_price":            8.00,
        "whitelisted_advertisers": ["Nike"]
    },
    "nytimes.com": {
        "floor_price":            12.00,
        "whitelisted_advertisers": ["AmericanExpress"]
    },
    "cnn.com": {
        "floor_price":            10.00,
        "whitelisted_advertisers": ["AmericanExpress"]
    },
    "reddit.com": {
        "floor_price":            6.00,
        "whitelisted_advertisers": ["Netflix"]
    }
}

# ── DMP: AUDIENCE SEGMENT ASSIGNMENT ─────────────────
def assign_segments(publisher, device, geo, hour, ad_format):
    segments = []

    # Sports fan — ESPN visitors on any device
    if publisher == "espn.com":
        segments.append("sports_fan")

    # High income — financial/news sites, desktop users
    if publisher in ["nytimes.com", "cnn.com"] \
            and device == "desktop":
        segments.append("high_income")

    # In market for shoes — sports fan + prime time browsing
    if "sports_fan" in segments and 18 <= hour <= 22:
        segments.append("in_market_shoes")

    # Frequent traveler — news sites + mobile
    if publisher in ["cnn.com", "nytimes.com"] \
            and device == "mobile":
        segments.append("frequent_traveler")

    # Entertainment segment — Reddit + BuzzFeed
    if publisher in ["reddit.com", "buzzfeed.com"]:
        segments.append("entertainment")
        segments.append("streaming_user")

    # In market auto — weather + suburban geos
    if publisher == "weather.com" \
            and geo in ["Houston", "Phoenix", "San Antonio"]:
        segments.append("in_market_auto")
        segments.append("suburban")

    # In market finance — any desktop user on finance/news sites
    if device == "desktop" \
            and publisher in ["nytimes.com", "cnn.com"]:
        segments.append("in_market_finance")

    # Food & beverage — broad audience
    if publisher in ["buzzfeed.com", "weather.com"]:
        segments.append("food_beverage")

    # General audience — always assigned as fallback
    segments.append("general_audience")

    return list(set(segments))

# ── DSP BIDDING ENGINE ────────────────────────────────
def calculate_bid(advertiser_name, advertiser, segments,
                  ad_format, device):

    # Check if this format's floor is too high for advertiser
    format_floor = FORMAT_CPM_FLOOR[ad_format]
    if advertiser["base_cpm"] < format_floor:
        return 0.0   # advertiser won't bid on premium formats

    # Does this advertiser care about this audience?
    segment_match = any(
        s in segments
        for s in advertiser["target_segments"]
    )

    if not segment_match:
        # 20% chance of bidding anyway (prospecting)
        if random.random() > 0.20:
            return 0.0

    strategy = advertiser["strategy"]

    if strategy == "fixed":
        bid = advertiser["base_cpm"]

    elif strategy == "dynamic":
        # Count how many target segments match
        matches = sum(
            1 for s in segments
            if s in advertiser["target_segments"]
        )
        # Each matching segment adds 40% bid multiplier
        multiplier = 1.0 + (matches * 0.40)

        # Device adjustments
        device_bonus = {
            "desktop": 1.10,
            "mobile":  1.05,
            "ctv":     1.30,
            "tablet":  1.00
        }.get(device, 1.0)

        bid = min(
            advertiser["base_cpm"] * multiplier * device_bonus,
            advertiser["max_cpm"]
        )

    elif strategy == "floor_logic":
        # Bid between floor and max based on segment match
        if segment_match:
            bid = np.random.uniform(
                advertiser["base_cpm"],
                advertiser["max_cpm"]
            )
        else:
            bid = advertiser.get("floor_cpm", 1.50)

    # Add realistic noise (±15%)
    noise = np.random.uniform(0.85, 1.15)
    bid = round(bid * noise, 4)

    return max(bid, 0.0)
# ── SECOND-PRICE AUCTION ENGINE ───────────────────────
def run_auction(bids):
    # bids = {"Nike": 5.20, "Toyota": 3.10, ...}
    if not bids:
        return None, None, 0.0

    # Sort bids highest to lowest
    sorted_bids = sorted(
        bids.items(), key=lambda x: x[1], reverse=True
    )

    winner     = sorted_bids[0][0]
    win_bid    = sorted_bids[0][1]

    if len(sorted_bids) > 1:
        # Second-price: winner pays 2nd highest + $0.01
        clearing_price = round(sorted_bids[1][1] + 0.01, 4)
    else:
        # Only one bidder — pays their own bid
        clearing_price = win_bid

    return winner, win_bid, clearing_price

# ── GENERATE IMPRESSIONS + RUN SIMULATION ─────────────
print(f"Starting RTB simulation: {NUM_IMPRESSIONS:,} impressions...")

records = []
start_date = datetime(2024, 1, 1)

for i in range(NUM_IMPRESSIONS):

    # Generate impression attributes
    hour      = random.choices(
                    range(24), weights=HOUR_WEIGHTS)[0]
    publisher = random.choice(PUBLISHERS)
    ad_format = random.choices(
                    AD_FORMATS, weights=FORMAT_WEIGHTS)[0]
    device    = random.choices(
                    DEVICES, weights=DEVICE_WEIGHTS)[0]
    geo       = random.choice(GEOS)
    timestamp = start_date + timedelta(
                    days=random.randint(0, 89),
                    hours=hour,
                    minutes=random.randint(0, 59)
                )

    # Assign audience segments via DMP
    segments = assign_segments(
        publisher, device, geo, hour, ad_format
    )

    # Check if this is a PMP deal impression
    is_pmp    = False
    pmp_floor = 0.0
    if publisher in PMP_DEALS:
        deal      = PMP_DEALS[publisher]
        pmp_floor = deal["floor_price"]
        eligible  = deal["whitelisted_advertisers"]
        is_pmp    = True

        # PMP: only whitelisted advertisers can bid
        bids = {}
        for adv_name in eligible:
            adv = ADVERTISERS[adv_name]
            bid = calculate_bid(
                adv_name, adv, segments, ad_format, device
            )
            # GUARDRAIL: independently re-verify the bid against its own
            # strategy's stated constraints (max_cpm / floor_cpm / PMP
            # floor) before it's allowed into the auction — calculate_bid()
            # enforces these BEFORE noise is applied, but noise can still
            # push the final bid outside those bounds.
            bid, bid_valid, _ = validate_bid(
                adv_name, adv, bid, pmp_floor=pmp_floor,
                context=f"impression_{i+1}_pmp"
            )
            if bid >= pmp_floor:
                bids[adv_name] = bid

        # If no PMP bids → fall to open auction
        if not bids:
            is_pmp = False

    if not is_pmp:
        # Open auction — all advertisers can bid
        bids = {}
        for adv_name, adv in ADVERTISERS.items():
            bid = calculate_bid(
                adv_name, adv, segments, ad_format, device
            )
            # GUARDRAIL: same independent re-check, no PMP floor in the
            # open auction.
            bid, bid_valid, _ = validate_bid(
                adv_name, adv, bid, pmp_floor=None,
                context=f"impression_{i+1}_open"
            )
            if bid > 0:
                bids[adv_name] = bid

    # Run the auction
    winner, win_bid, clearing_price = run_auction(bids)

    # Generate CTR and click based on device
    base_ctr = DEVICE_CTR.get(device, 0.001)
    # Video and native get higher CTR
    if ad_format == "video":
        base_ctr *= 2.5
    elif ad_format == "native":
        base_ctr *= 2.0

    clicked   = 1 if random.random() < base_ctr else 0

    # Conversion: ~8% of clicks convert
    converted = 1 if clicked and random.random() < 0.08 \
                else 0

    records.append({
        "impression_id":   i + 1,
        "timestamp":       timestamp.strftime(
                               "%Y-%m-%d %H:%M:%S"),
        "publisher":       publisher,
        "ad_format":       ad_format,
        "device":          device,
        "geo":             geo,
        "hour":            hour,
        "segments":        "|".join(segments),
        "is_pmp":          is_pmp,
        "pmp_floor":       pmp_floor,
        "num_bidders":     len(bids),
        "all_bids":        str(bids),
        "winner":          winner if winner else "no_fill",
        "winning_bid":     win_bid if win_bid else 0.0,
        "clearing_price":  clearing_price,
        "clicked":         clicked,
        "converted":       converted,
        "revenue":         clearing_price if winner else 0.0
    })

    # Progress update every 10,000 rows
    if (i + 1) % 10_000 == 0:
        print(f"  {i+1:,} impressions processed...")

# ── EXPORT TO CSV ─────────────────────────────────────
df = pd.DataFrame(records)
df.to_csv("data/auctions.csv", index=False)
print(f"\n✅ Done! {len(df):,} auction records saved.")
print(f"   File: data/auctions.csv")

# ── GUARDRAIL SUMMARY ─────────────────────────────────
n_logged = flush_audit_log()
print(f"\n── GUARDRAIL LAYER ──────────────────────────────")
print(f"Bid-validation events logged: {n_logged:,}")
print(f"   File: data/guardrail_audit_log.csv")

# ── QUICK SUMMARY STATS ───────────────────────────────
print("\n── SIMULATION SUMMARY ──────────────────────────")
print(f"Total impressions:    {len(df):,}")
print(f"Filled impressions:   "
      f"{(df.winner != 'no_fill').sum():,}")
print(f"Fill rate:            "
      f"{(df.winner != 'no_fill').mean():.1%}")
print(f"PMP impressions:      {df.is_pmp.sum():,}")
print(f"Total clicks:         {df.clicked.sum():,}")
print(f"Total conversions:    {df.converted.sum():,}")
print(f"Avg clearing price:   "
      f"${df.clearing_price.mean():.2f} CPM")
print(f"Total revenue:        "
      f"${df.revenue.sum():,.2f}")
print("\n── WINNER BREAKDOWN ────────────────────────────")
print(df.winner.value_counts().to_string())