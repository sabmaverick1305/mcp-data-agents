"""
Data package — warehouse seeding, schema, and dataset utilities.

seed.py         Creates and populates data/warehouse.db (SQLite star schema).
                Two modes: demo (synthetic, Q1 2024 dip) or real (CSV datasets).
                Exposes DB_PATH so other modules can locate the database file.

datasets.py     Dataset adapter classes for loading real-world CSV files into
                the warehouse schema. Each adapter normalises a different source
                (Kaggle sales, UCI retail) to the common fact/dim table layout.

download_datasets.py
                CLI helper that downloads the raw CSV files to data/raw/.
                Run once before seeding in real mode.

schema_mappings.json
                Column-name mappings from raw CSV headers to warehouse schema
                fields (date_id, product_id, revenue, cost, etc.).

Warehouse star schema:
    sales_fact          — one row per transaction
    region_dim          — 4 regions with annual revenue targets
    product_dim         — 8 SKUs across 4 product categories
    customer_dim        — 10 accounts across 3 customer segments
    date_dim            — calendar date table with year/quarter/month/week
"""
