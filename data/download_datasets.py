"""
Download and prepare real datasets for evaluation.

Usage:
    python data/download_datasets.py              # Download all datasets
    python data/download_datasets.py --dataset kaggle
    python data/download_datasets.py --dataset uci
"""
import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

# Handle imports from both module and direct script contexts
try:
    from data.datasets import (
        DB_PATH,
        DATASETS_DIR,
        get_dataset,
        KaggleSalesDataset,
        UCIOnlineRetailDataset,
        SyntheticDataset,
    )
except ImportError:
    from datasets import (
        DB_PATH,
        DATASETS_DIR,
        get_dataset,
        KaggleSalesDataset,
        UCIOnlineRetailDataset,
        SyntheticDataset,
    )


def download_dataset(dataset_name: str, clear_existing: bool = False) -> dict:
    """Download and load a single dataset.

    Args:
        dataset_name: "kaggle", "uci", or "synthetic"
        clear_existing: Whether to clear sales_fact before loading

    Returns:
        Dict with load statistics
    """
    print(f"\n{'='*60}")
    print(f"Loading dataset: {dataset_name}")
    print(f"{'='*60}")

    dataset = get_dataset(dataset_name)
    print(f"Description: {dataset.description}")
    print(f"Source: {dataset.url}")

    # Download
    try:
        csv_path = dataset.download()
        print(f"✓ Downloaded to {csv_path}")
    except Exception as e:
        print(f"✗ Download failed: {e}")
        return {"dataset": dataset_name, "status": "failed", "error": str(e)}

    # Load into database
    try:
        conn = sqlite3.connect(DB_PATH)
        stats = dataset.load_into_warehouse(conn, clear_existing=clear_existing)
        conn.close()
        print(f"✓ Loaded {stats['rows_loaded']} rows into warehouse")
        stats["status"] = "success"
        return stats
    except Exception as e:
        print(f"✗ Database load failed: {e}")
        return {"dataset": dataset_name, "status": "failed", "error": str(e)}


def download_all(clear_existing: bool = False) -> list[dict]:
    """Download all configured datasets.

    Args:
        clear_existing: Whether to clear existing data before loading

    Returns:
        List of load statistics
    """
    datasets = ["kaggle", "uci"]
    results = []

    for dataset_name in datasets:
        try:
            result = download_dataset(dataset_name, clear_existing=clear_existing)
            results.append(result)
        except Exception as e:
            print(f"Error with {dataset_name}: {e}")
            results.append({
                "dataset": dataset_name,
                "status": "failed",
                "error": str(e),
            })

    return results


def print_summary(results: list[dict]) -> None:
    """Print download summary."""
    print(f"\n{'='*60}")
    print("DOWNLOAD SUMMARY")
    print(f"{'='*60}")

    for result in results:
        status = "✓" if result["status"] == "success" else "✗"
        print(f"{status} {result['dataset']:15s} - {result.get('status', 'unknown')}")
        if result["status"] == "success":
            print(f"  └─ {result['rows_loaded']} rows loaded from {result['path']}")
        else:
            print(f"  └─ Error: {result.get('error', 'unknown error')}")

    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Download and prepare real datasets"
    )
    parser.add_argument(
        "--dataset",
        choices=["kaggle", "uci", "synthetic"],
        default=None,
        help="Download specific dataset (default: all)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear existing sales_fact data before loading",
    )

    args = parser.parse_args()

    print("Dataset Downloader")
    print(f"Database: {DB_PATH}")
    print(f"Raw CSV: {DATASETS_DIR}")

    if args.dataset:
        results = [download_dataset(args.dataset, clear_existing=args.clear)]
    else:
        results = download_all(clear_existing=args.clear)

    print_summary(results)

    # Success if all succeeded
    all_success = all(r["status"] == "success" for r in results)
    exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
