import numpy as np
import pandas as pd
from faker import Faker
import random
import sqlite3
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import warnings
warnings.filterwarnings('ignore')

fake = Faker()
np.random.seed(42)
random.seed(42)

# ── CONFIG ──────────────────────────────────────────
NUM_USERS = 50_000

# ── MERCHANT CATEGORIES ─────────────────────────────
MERCHANT_CATEGORIES = [
    "athletic_footwear", "electronics", "travel",
    "grocery", "restaurants", "entertainment",
    "automotive", "luxury_goods", "home_improvement",
    "fashion", "health_wellness", "financial_services"
]

# ── GENERATE USER PROFILES ───────────────────────────
print(f"Generating {NUM_USERS:,} user profiles...")

users = []
for i in range(NUM_USERS):

    # Demographics
    age        = int(np.random.normal(38, 12))
    age        = max(18, min(75, age))
    income     = int(np.random.lognormal(10.8, 0.6))
    income     = max(20000, min(500000, income))

    # Device behavior
    device_pref = random.choices(
        ["mobile", "desktop", "tablet"],
        weights=[0.55, 0.35, 0.10]
    )[0]

    # Purchase behavior — how many transactions
    # in last 90 days (higher income = more transactions)
    income_factor      = min(income / 75000, 3.0)
    purchase_count_90d = max(0, int(
        np.random.poisson(5 * income_factor)
    ))

    # Average order value — correlated with income
    avg_order_value = round(
        max(10, np.random.normal(
            income / 800, income / 1600
        )), 2
    )

    # Days since last purchase
    days_since_purchase = random.randint(0, 180)

    # Top merchant categories this user shops
    num_categories = random.randint(1, 4)
    top_categories = random.sample(
        MERCHANT_CATEGORIES, num_categories
    )

    # Browsing signals
    site_visits_7d  = max(0, int(np.random.poisson(12)))
    email_opens_30d = max(0, int(np.random.poisson(4)))

    # Loyalty program member?
    loyalty_member  = random.random() < 0.35

    # Has the user converted from an ad before?
    # Higher income + more purchases = higher probability
    convert_prob = min(
        0.02 + (purchase_count_90d * 0.005) +
        (income / 1000000), 0.25
    )
    is_converter = random.random() < convert_prob

    users.append({
        "user_id":              i + 1,
        "age":                  age,
        "income":               income,
        "device_pref":          device_pref,
        "purchase_count_90d":   purchase_count_90d,
        "avg_order_value":      avg_order_value,
        "days_since_purchase":  days_since_purchase,
        "top_categories":       "|".join(top_categories),
        "site_visits_7d":       site_visits_7d,
        "email_opens_30d":      email_opens_30d,
        "loyalty_member":       int(loyalty_member),
        "is_converter":         int(is_converter),
        "lifetime_value":       round(
            purchase_count_90d * avg_order_value * 4, 2
        )
    })

df_users = pd.DataFrame(users)
df_users.to_csv("data/users.csv", index=False)
print(f"✅ {len(df_users):,} user profiles saved.")
print(f"   Converters: {df_users.is_converter.sum():,} "
      f"({df_users.is_converter.mean():.1%})")
print(f"   Avg income: ${df_users.income.mean():,.0f}")
print(f"   Avg LTV:    ${df_users.lifetime_value.mean():,.2f}")
# ── SQL SEGMENTATION ENGINE ──────────────────────────
print("\nRunning SQL segmentation engine...")

# Load users into SQLite database
conn = sqlite3.connect("data/adtech.db")
df_users.to_sql("users", conn,
                if_exists="replace", index=False)

# ── SEGMENTATION QUERIES ─────────────────────────────
# These mirror real CDP segmentation logic

SEGMENT_QUERIES = {

    "high_value_customer": """
        SELECT user_id, 'high_value_customer' as segment
        FROM users
        WHERE purchase_count_90d >= 8
          AND avg_order_value >= 100
          AND lifetime_value >= 2000
    """,

    "lapsed_customer": """
        SELECT user_id, 'lapsed_customer' as segment
        FROM users
        WHERE days_since_purchase BETWEEN 60 AND 180
          AND purchase_count_90d >= 2
    """,

    "high_intent_no_purchase": """
        SELECT user_id, 'high_intent_no_purchase' as segment
        FROM users
        WHERE site_visits_7d >= 10
          AND purchase_count_90d = 0
    """,

    "loyalty_high_ltv": """
        SELECT user_id, 'loyalty_high_ltv' as segment
        FROM users
        WHERE loyalty_member = 1
          AND lifetime_value >= 1000
    """,

    "young_high_spender": """
        SELECT user_id, 'young_high_spender' as segment
        FROM users
        WHERE age BETWEEN 18 AND 34
          AND avg_order_value >= 75
          AND purchase_count_90d >= 3
    """,

    "price_sensitive": """
        SELECT user_id, 'price_sensitive' as segment
        FROM users
        WHERE avg_order_value < 30
          AND purchase_count_90d >= 5
    """,

    "affluent_traveler": """
        SELECT user_id, 'affluent_traveler' as segment
        FROM users
        WHERE income >= 150000
          AND top_categories LIKE '%travel%'
          AND avg_order_value >= 150
    """,

    "re_engagement": """
        SELECT user_id, 're_engagement' as segment
        FROM users
        WHERE days_since_purchase > 90
          AND lifetime_value >= 500
          AND email_opens_30d >= 2
    """
}

# Run all segment queries and collect results
all_segments = []
segment_sizes = {}

for segment_name, query in SEGMENT_QUERIES.items():
    result = pd.read_sql_query(query, conn)
    segment_sizes[segment_name] = len(result)
    all_segments.append(result)
    print(f"   {segment_name:<30} "
          f"{len(result):>6,} users")

# Combine all segments
df_segments = pd.concat(all_segments, ignore_index=True)
df_segments.to_sql("segments", conn,
                   if_exists="replace", index=False)
df_segments.to_csv("data/segments.csv", index=False)

print(f"\n✅ Segmentation complete!")
print(f"   Total segment assignments: "
      f"{len(df_segments):,}")

# ── AUDIENCE OVERLAP ANALYSIS ────────────────────────
print("\nCalculating audience overlap...")

overlap_query = """
    WITH user_segment_count AS (
        SELECT
            user_id,
            COUNT(*) as num_segments
        FROM segments
        GROUP BY user_id
    )
    SELECT
        num_segments,
        COUNT(*) as num_users,
        ROUND(COUNT(*) * 100.0 /
            (SELECT COUNT(DISTINCT user_id)
             FROM segments), 2) as pct_of_segmented
    FROM user_segment_count
    GROUP BY num_segments
    ORDER BY num_segments
"""

df_overlap = pd.read_sql_query(overlap_query, conn)
print("\n── AUDIENCE OVERLAP ────────────────────────────")
print(df_overlap.to_string(index=False))

# ── YIELD ANALYSIS BY SEGMENT ────────────────────────
yield_query = """
    SELECT
        s.segment,
        COUNT(DISTINCT s.user_id)        as segment_size,
        ROUND(AVG(u.income), 0)          as avg_income,
        ROUND(AVG(u.avg_order_value), 2) as avg_order_value,
        ROUND(AVG(u.lifetime_value), 2)  as avg_ltv,
        ROUND(AVG(u.purchase_count_90d), 1) as avg_purchases,
        SUM(u.is_converter)              as converters,
        ROUND(
            SUM(u.is_converter) * 100.0 /
            COUNT(*), 2
        )                                as conversion_rate_pct
    FROM segments s
    JOIN users u ON s.user_id = u.user_id
    GROUP BY s.segment
    ORDER BY avg_ltv DESC
"""

df_yield = pd.read_sql_query(yield_query, conn)
df_yield.to_csv("data/segment_analysis.csv", index=False)
print("\n── SEGMENT PERFORMANCE ─────────────────────────")
print(df_yield.to_string(index=False))
# ── LOOKALIKE MODEL (Scikit-learn) ───────────────────
print("\n\nBuilding lookalike model...")

# Features for the ML model
FEATURES = [
    "age", "income", "purchase_count_90d",
    "avg_order_value", "days_since_purchase",
    "site_visits_7d", "email_opens_30d",
    "loyalty_member", "lifetime_value"
]

TARGET = "is_converter"

X = df_users[FEATURES]
y = df_users[TARGET]

# Split into train and test sets
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42,
    stratify=y
)

# Scale features — important for logistic regression
scaler  = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled  = scaler.transform(X_test)

# Train logistic regression lookalike model
model = LogisticRegression(
    class_weight="balanced",
    max_iter=1000,
    random_state=42
)
model.fit(X_train_scaled, y_train)

# Evaluate model
y_pred = model.predict(X_test_scaled)
print("\n── MODEL PERFORMANCE ───────────────────────────")
print(classification_report(y_test, y_pred))

# Score ALL users — assign lookalike probability
X_all_scaled       = scaler.transform(df_users[FEATURES])
df_users["lookalike_score"] = model.predict_proba(
    X_all_scaled
)[:, 1]

# ── FEATURE IMPORTANCE ───────────────────────────────
print("── FEATURE IMPORTANCE ──────────────────────────")
importance = pd.DataFrame({
    "feature":     FEATURES,
    "coefficient": model.coef_[0]
}).sort_values("coefficient", ascending=False)
print(importance.to_string(index=False))

# ── SIMULATE BID PRICE BASED ON LOOKALIKE SCORE ──────
print("\n── BID PRICE BY LOOKALIKE SCORE ────────────────")

def score_to_bid(score, base_bid=2.00, max_bid=15.00):
    """
    Convert lookalike score to DSP bid price.
    Higher score = higher bid.
    This is how DSPs use CDP data to bid smarter.
    """
    multiplier = 1.0 + (score * 8.0)
    return round(min(base_bid * multiplier, max_bid), 2)

df_users["recommended_bid_cpm"] = \
    df_users["lookalike_score"].apply(score_to_bid)

# Show bid distribution by score tier
df_users["score_tier"] = pd.cut(
    df_users["lookalike_score"],
    bins=[0, 0.1, 0.25, 0.5, 0.75, 1.0],
    labels=["Very Low", "Low", "Medium", "High", "Very High"]
)

bid_analysis = df_users.groupby(
    "score_tier", observed=True
).agg(
    num_users        = ("user_id", "count"),
    avg_score        = ("lookalike_score", "mean"),
    avg_bid_cpm      = ("recommended_bid_cpm", "mean"),
    avg_income       = ("income", "mean"),
    avg_ltv          = ("lifetime_value", "mean"),
    converter_rate   = ("is_converter", "mean")
).round(2)

print(bid_analysis.to_string())

# Save enriched user profiles
df_users.to_csv("data/users_scored.csv", index=False)
conn.close()

print("\n✅ Component 2 Complete!")
print("   Files saved:")
print("   → data/users.csv")
print("   → data/users_scored.csv")
print("   → data/segments.csv")
print("   → data/segment_analysis.csv")
print("   → data/adtech.db  (SQLite database)")
print("\n── COMPONENT 2 SUMMARY ─────────────────────────")
print(f"   Users profiled:      {len(df_users):,}")
print(f"   Segments created:    {len(SEGMENT_QUERIES)}")
print(f"   Converters found:    "
      f"{df_users.is_converter.sum():,}")
print(f"   Top lookalike score: "
      f"{df_users.lookalike_score.max():.4f}")
print(f"   Avg recommended bid: "
      f"${df_users.recommended_bid_cpm.mean():.2f} CPM")