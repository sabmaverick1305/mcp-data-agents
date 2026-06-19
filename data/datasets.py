"""
Dataset loading utilities for both demo and real datasets.

Supports multiple data sources with schema mapping and normalization.
"""
import os
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.request import urlopen
from io import BytesIO
import zipfile
import csv

import pandas as pd


DATASETS_DIR = Path(__file__).parent / "raw"
DB_PATH = Path(__file__).parent / "warehouse.db"


def load_csv_data(
    csv_path: str,
    table_name: str,
    schema_map: dict,
    conn: sqlite3.Connection,
    date_format: str = "%Y-%m-%d",
) -> int:
    """Load CSV with column mapping to warehouse schema.

    Args:
        csv_path: Path to CSV file
        table_name: Target table (sales_fact, product_dim, etc.)
        schema_map: Dict mapping CSV columns -> table columns
        conn: SQLite connection
        date_format: Date format in CSV

    Returns:
        Number of rows inserted
    """
    if not Path(csv_path).exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    # Try multiple encodings
    df = None
    for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
        try:
            df = pd.read_csv(csv_path, encoding=encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if df is None:
        raise ValueError(f"Cannot decode {csv_path} with any standard encoding")

    # Validate schema
    missing_cols = set(schema_map.keys()) - set(df.columns)
    if missing_cols:
        raise ValueError(f"CSV missing columns: {missing_cols}")

    # Rename columns to match table schema
    df = df.rename(columns=schema_map)
    df = df[list(schema_map.values())]

    # Insert into database
    df.to_sql(table_name, conn, if_exists="append", index=False)
    return len(df)



def normalize_dates(dates_series, input_format: str, output_format: str = "%Y-%m-%d"):
    """Convert date format across a series.

    Args:
        dates_series: Pandas Series of date strings
        input_format: Input date format (e.g., "%m/%d/%Y")
        output_format: Output format (default ISO)

    Returns:
        Series of normalized dates as strings
    """
    return pd.to_datetime(dates_series, format=input_format).dt.strftime(output_format)


def validate_schema_match(df: pd.DataFrame, required_columns: list[str]) -> bool:
    """Verify CSV has required columns.

    Args:
        df: DataFrame to validate
        required_columns: List of required column names

    Returns:
        True if all required columns present

    Raises:
        ValueError if any columns missing
    """
    missing = set(required_columns) - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    return True


class DatasetLoader(ABC):
    """Base class for dataset sources."""

    def __init__(self, name: str, description: str, url: str):
        self.name = name
        self.description = description
        self.url = url
        self.local_path: Optional[Path] = None

    @abstractmethod
    def download(self) -> Path:
        """Download dataset and return path to CSV."""
        pass

    @abstractmethod
    def prepare(self, csv_path: Path) -> tuple[dict, str]:
        """Prepare dataset. Returns (schema_map, date_format)."""
        pass

    def load_into_warehouse(
        self,
        conn: sqlite3.Connection,
        clear_existing: bool = False,
    ) -> dict:
        """Load dataset into warehouse.

        Args:
            conn: SQLite connection
            clear_existing: Whether to clear existing sales_fact data

        Returns:
            Dict with load statistics
        """
        if not self.local_path:
            self.download()

        if clear_existing:
            cur = conn.cursor()
            cur.execute("DELETE FROM sales_fact")
            conn.commit()

        schema_map, date_format = self.prepare(self.local_path)
        rows_loaded = load_csv_data(
            str(self.local_path),
            "sales_fact",
            schema_map,
            conn,
            date_format,
        )

        return {
            "dataset": self.name,
            "rows_loaded": rows_loaded,
            "path": str(self.local_path),
        }


class KaggleSalesDataset(DatasetLoader):
    """Kaggle Sample Sales Data.

    Small, clean dataset for initial testing.
    URL: https://www.kaggle.com/datasets/kyanyoga/sample-sales-data
    """

    def __init__(self):
        super().__init__(
            name="kaggle_sales",
            description="Kaggle Sample Sales Dataset (~500 records)",
            url="https://www.kaggle.com/datasets/kyanyoga/sample-sales-data",
        )

    def download(self) -> Path:
        """Download Kaggle sales data CSV."""
        DATASETS_DIR.mkdir(exist_ok=True)

        csv_path = DATASETS_DIR / "kaggle_sales.csv"

        # Direct download via Kaggle's public URL (no API key needed)
        download_url = "https://www.kaggle.com/api/v1/datasets/download/kyanyoga/sample-sales-data"

        try:
            print(f"Downloading {self.name}...")
            with urlopen(download_url) as response:
                zip_data = BytesIO(response.read())

            with zipfile.ZipFile(zip_data) as z:
                files = z.namelist()
                csv_file = next((f for f in files if f.endswith('.csv')), None)
                if csv_file:
                    z.extract(csv_file, DATASETS_DIR)
                    extracted = DATASETS_DIR / csv_file
                    extracted.rename(csv_path)
                    print(f"Downloaded to {csv_path}")
        except Exception as e:
            print(f"Failed to download via URL: {e}")
            # Fallback: provide instructions for manual download
            raise RuntimeError(
                f"Cannot auto-download {self.name}. Manual setup required. "
                f"Visit: {self.url}\n"
                f"Download the CSV and place at: {csv_path}"
            )

        self.local_path = csv_path
        return csv_path

    def prepare(self, csv_path: Path) -> tuple[dict, str]:
        """Map Kaggle schema to warehouse schema."""
        return (
            {
                "ORDERDATE": "date_id",
                "QUANTITYORDERED": "quantity",
                "PRICEEACH": "revenue",
            },
            "%m/%d/%Y",  # Kaggle date format
        )


class UCIOnlineRetailDataset(DatasetLoader):
    """UCI Online Retail Dataset.

    Real e-commerce data, ~500K transactions.
    URL: https://archive.ics.uci.edu/ml/datasets/online+retail
    """

    def __init__(self):
        super().__init__(
            name="uci_retail",
            description="UCI Online Retail Dataset (~500K records)",
            url="https://archive.ics.uci.edu/ml/datasets/online+retail",
        )

    def download(self) -> Path:
        """Download UCI Online Retail dataset."""
        DATASETS_DIR.mkdir(exist_ok=True)

        csv_path = DATASETS_DIR / "uci_retail.csv"

        # Direct download from UCI repository
        download_url = (
            "https://archive.ics.uci.edu/ml/machine-learning-databases/"
            "online_retail/Online_Retail.xlsx"
        )

        try:
            print(f"Downloading {self.name}...")
            # UCI provides XLSX, need to convert to CSV
            import openpyxl

            with urlopen(download_url) as response:
                xlsx_data = BytesIO(response.read())

            # Read Excel and convert to CSV
            df = pd.read_excel(xlsx_data)
            df.to_csv(csv_path, index=False)
            print(f"Downloaded and converted to {csv_path}")
        except ImportError:
            raise RuntimeError(
                "openpyxl required for UCI dataset. "
                "Install: pip install openpyxl"
            )
        except Exception as e:
            print(f"Failed to download: {e}")
            raise RuntimeError(
                f"Cannot auto-download {self.name}. Manual setup required. "
                f"Visit: {self.url}\n"
                f"Download the CSV and place at: {csv_path}"
            )

        self.local_path = csv_path
        return csv_path

    def prepare(self, csv_path: Path) -> tuple[dict, str]:
        """Map UCI schema to warehouse schema."""
        return (
            {
                "InvoiceDate": "date_id",
                "Quantity": "quantity",
                "UnitPrice": "revenue",
            },
            "%d/%m/%Y %H:%M",  # UCI date format with time
        )


class SyntheticDataset(DatasetLoader):
    """Generate synthetic data with realistic patterns."""

    def __init__(self):
        super().__init__(
            name="synthetic",
            description="Programmatically generated synthetic data",
            url="local",
        )

    def download(self) -> Path:
        """Generate synthetic data (no download needed)."""
        DATASETS_DIR.mkdir(exist_ok=True)
        csv_path = DATASETS_DIR / "synthetic.csv"

        # Generate using faker if available
        try:
            from faker import Faker
            import random

            fake = Faker()
            records = []

            for _ in range(1000):
                records.append({
                    "date_id": fake.date_between(start_date="-2y").strftime("%Y-%m-%d"),
                    "quantity": random.randint(1, 20),
                    "revenue": round(random.uniform(10, 1000), 2),
                })

            df = pd.DataFrame(records)
            df.to_csv(csv_path, index=False)
            print(f"Generated synthetic data to {csv_path}")
        except ImportError:
            raise RuntimeError(
                "faker required for synthetic dataset. "
                "Install: pip install faker"
            )

        self.local_path = csv_path
        return csv_path

    def prepare(self, csv_path: Path) -> tuple[dict, str]:
        """Return schema for synthetic data."""
        return (
            {
                "date_id": "date_id",
                "quantity": "quantity",
                "revenue": "revenue",
            },
            "%Y-%m-%d",
        )


def get_dataset(name: str) -> DatasetLoader:
    """Factory to get dataset loader by name."""
    datasets = {
        "kaggle": KaggleSalesDataset(),
        "uci": UCIOnlineRetailDataset(),
        "synthetic": SyntheticDataset(),
    }

    if name not in datasets:
        raise ValueError(f"Unknown dataset: {name}. Available: {list(datasets.keys())}")

    return datasets[name]
