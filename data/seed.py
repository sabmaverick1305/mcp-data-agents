import sqlite3
import random
from datetime import date, timedelta
import os
import argparse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warehouse.db")

# Determine seed mode from environment or default to demo
SEED_MODE = os.environ.get("SEED_MODE", "demo")


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create warehouse schema (shared by all modes)."""
    cur = conn.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS sales_fact;
    DROP TABLE IF EXISTS product_dim;
    DROP TABLE IF EXISTS customer_dim;
    DROP TABLE IF EXISTS region_dim;
    DROP TABLE IF EXISTS date_dim;

    CREATE TABLE region_dim (
        region_id INTEGER PRIMARY KEY,
        region_name TEXT,
        country TEXT,
        manager TEXT,
        target_revenue REAL
    );

    CREATE TABLE product_dim (
        product_id INTEGER PRIMARY KEY,
        product_name TEXT,
        category TEXT,
        subcategory TEXT,
        unit_price REAL,
        unit_cost REAL
    );

    CREATE TABLE customer_dim (
        customer_id INTEGER PRIMARY KEY,
        customer_name TEXT,
        segment TEXT,
        country TEXT
    );

    CREATE TABLE date_dim (
        date_id TEXT PRIMARY KEY,
        year INTEGER,
        quarter INTEGER,
        month INTEGER,
        month_name TEXT,
        week INTEGER
    );

    CREATE TABLE sales_fact (
        sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_id TEXT,
        product_id INTEGER,
        customer_id INTEGER,
        region_id INTEGER,
        quantity INTEGER,
        revenue REAL,
        cost REAL,
        gross_profit REAL,
        FOREIGN KEY (date_id) REFERENCES date_dim(date_id),
        FOREIGN KEY (product_id) REFERENCES product_dim(product_id),
        FOREIGN KEY (customer_id) REFERENCES customer_dim(customer_id),
        FOREIGN KEY (region_id) REFERENCES region_dim(region_id)
    );
    """)
    conn.commit()


def _seed_demo_data(conn: sqlite3.Connection) -> int:
    """Seed demo dataset (synthetic, with Q1 2024 dip)."""
    cur = conn.cursor()

    # Seed dimension tables
    regions = [
        (1, "North America", "USA", "Alice Johnson", 5000000),
        (2, "Europe", "UK", "Bob Smith", 3500000),
        (3, "Asia Pacific", "Singapore", "Carol Lee", 2800000),
        (4, "Latin America", "Brazil", "Diego Martinez", 1500000),
    ]
    cur.executemany("INSERT INTO region_dim VALUES (?,?,?,?,?)", regions)

    products = [
        (1, "Enterprise Suite", "Software", "Analytics", 4999, 500),
        (2, "Data Platform", "Software", "Data", 2999, 300),
        (3, "AI Assistant", "Software", "AI", 1999, 200),
        (4, "Pro Dashboard", "Software", "Analytics", 999, 100),
        (5, "Cloud Storage 1TB", "Infrastructure", "Storage", 499, 50),
        (6, "Compute Pack", "Infrastructure", "Compute", 799, 80),
        (7, "Security Suite", "Security", "Compliance", 1499, 150),
        (8, "Training Credits", "Services", "Education", 299, 30),
    ]
    cur.executemany("INSERT INTO product_dim VALUES (?,?,?,?,?,?)", products)

    customers = [
        (1, "TechCorp Inc", "Enterprise", "USA"),
        (2, "DataFlow Ltd", "Enterprise", "UK"),
        (3, "StartupXYZ", "SMB", "Germany"),
        (4, "MegaRetail", "Enterprise", "USA"),
        (5, "FinanceHub", "Enterprise", "Singapore"),
        (6, "HealthTech Co", "Mid-Market", "Australia"),
        (7, "EduLearn", "SMB", "Canada"),
        (8, "LogiCorp", "Mid-Market", "Brazil"),
        (9, "MediaGroup", "Enterprise", "France"),
        (10, "AutoDrive Inc", "Mid-Market", "Japan"),
    ]
    cur.executemany("INSERT INTO customer_dim VALUES (?,?,?,?)", customers)

    # Seed date dimension
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    d = date(2023, 1, 1)
    while d <= date(2024, 6, 30):
        quarter = (d.month - 1) // 3 + 1
        week = d.isocalendar()[1]
        cur.execute("INSERT OR IGNORE INTO date_dim VALUES (?,?,?,?,?,?)",
                    (d.strftime("%Y-%m-%d"), d.year, quarter, d.month, month_names[d.month - 1], week))
        d += timedelta(days=1)

    # Generate sales facts with Q1 2024 dip pattern
    random.seed(42)
    sales = []
    d = date(2023, 1, 1)
    while d <= date(2024, 6, 30):
        if d.weekday() < 5:  # Weekdays only
            # Q1 2024 intentionally has a dip to create interesting cross-source analysis
            if d.year == 2024 and d.month in (1, 2, 3):
                num_sales = random.randint(1, 4)
            else:
                num_sales = random.randint(5, 14)

            for _ in range(num_sales):
                product_id = random.randint(1, 8)
                customer_id = random.randint(1, 10)
                region_id = random.randint(1, 4)
                quantity = random.randint(1, 10)
                unit_price = products[product_id - 1][4]
                unit_cost = products[product_id - 1][5]
                price_var = random.uniform(0.88, 1.12)
                revenue = round(unit_price * quantity * price_var, 2)
                cost = round(unit_cost * quantity, 2)
                gross_profit = round(revenue - cost, 2)
                sales.append((d.strftime("%Y-%m-%d"), product_id, customer_id,
                              region_id, quantity, revenue, cost, gross_profit))
        d += timedelta(days=1)

    cur.executemany(
        "INSERT INTO sales_fact (date_id, product_id, customer_id, region_id, quantity, revenue, cost, gross_profit) "
        "VALUES (?,?,?,?,?,?,?,?)",
        sales
    )

    conn.commit()
    return len(sales)


def _seed_real_data(conn: sqlite3.Connection) -> int:
    """Seed real dataset from downloaded CSVs."""
    from data.datasets import get_dataset

    # Seed dimensions first (from demo, can be extended)
    cur = conn.cursor()
    regions = [(1, "Other", "Global", "Admin", 0)]
    cur.executemany("INSERT INTO region_dim VALUES (?,?,?,?,?)", regions)

    customers = [(1, "Unknown", "Other", "Unknown")]
    cur.executemany("INSERT INTO customer_dim VALUES (?,?,?,?)", customers)

    products = [(1, "Product", "Other", "Other", 100, 50)]
    cur.executemany("INSERT INTO product_dim VALUES (?,?,?,?,?,?)", products)

    # Seed date dimension (2 years)
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    d = date(2022, 1, 1)
    while d <= date(2024, 12, 31):
        quarter = (d.month - 1) // 3 + 1
        week = d.isocalendar()[1]
        cur.execute("INSERT OR IGNORE INTO date_dim VALUES (?,?,?,?,?,?)",
                    (d.strftime("%Y-%m-%d"), d.year, quarter, d.month, month_names[d.month - 1], week))
        d += timedelta(days=1)

    conn.commit()

    # Load real datasets
    total_rows = 0
    for dataset_name in ["kaggle", "uci"]:
        try:
            dataset = get_dataset(dataset_name)
            print(f"Loading {dataset_name}...")
            stats = dataset.load_into_warehouse(conn, clear_existing=False)
            total_rows += stats["rows_loaded"]
            print(f"  ✓ {stats['rows_loaded']} rows loaded")
        except Exception as e:
            print(f"  ✗ Failed to load {dataset_name}: {e}")

    return total_rows


def seed_database(mode: str = None):
    """Seed warehouse database.

    Args:
        mode: "demo" (synthetic) or "real" (from downloaded CSVs)
              Defaults to SEED_MODE environment variable or "demo"
    """
    if mode is None:
        mode = SEED_MODE

    print(f"Seeding warehouse in '{mode}' mode")
    print(f"Database: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    _create_schema(conn)

    if mode == "real":
        rows = _seed_real_data(conn)
    else:
        rows = _seed_demo_data(conn)

    conn.close()
    print(f"✓ Seeded {rows:,} sales records → {DB_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed warehouse database")
    parser.add_argument(
        "--mode",
        choices=["demo", "real"],
        default=None,
        help="Seed mode: demo (synthetic) or real (from CSV). "
             "Defaults to SEED_MODE env var or 'demo'",
    )
    args = parser.parse_args()

    seed_database(mode=args.mode)
