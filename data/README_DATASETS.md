# Datasets for MCP Data Agents

This guide explains how to use real datasets with the MCP Data Agents system.

## Overview

The system supports three data modes:

| Mode | Description | Use Case |
|------|-------------|----------|
| **demo** | Synthetic sales data (default) | Quick testing, development |
| **kaggle** | ~500 real sales records | Small dataset testing |
| **uci** | ~500K real e-commerce transactions | Production-scale testing |

All modes use the same warehouse schema (star schema with dimensions and facts).

---

## Quick Start

### Use Demo Data (Default)

```bash
# Demo mode is the default
python main.py

# Or explicitly:
SEED_MODE=demo python main.py
```

### Use Real Data

#### Option 1: Download all real datasets

```bash
# Download Kaggle and UCI datasets
python data/download_datasets.py

# Seed database with real data
SEED_MODE=real python data/seed.py

# Run evaluation with real data
python -m eval.runner --output eval_real.json

# Interactive CLI with real data
python main.py
```

#### Option 2: Use single dataset

```bash
# Download only Kaggle data
python data/download_datasets.py --dataset kaggle

# Load into warehouse
python data/seed.py --mode real

# Clear and reload (skips already-downloaded CSVs)
python data/seed.py --mode real
```

#### Option 3: One-command setup

```bash
bash scripts/setup_real_datasets.sh
```

---

## Available Datasets

### 1. Kaggle Sales Data

**URL:** https://www.kaggle.com/datasets/kyanyoga/sample-sales-data

**Size:** ~500 records

**Schema:**
```
OrderID | OrderDate | CustomerName | City | Quantity | UnitPrice
```

**Maps to:**
- `sales_fact` (OrderDate, Quantity, UnitPrice)
- `customer_dim` (CustomerName)
- `region_dim` (City)

**Use:** Quick testing, simple queries

**Access:** 
- Public dataset, no Kaggle API key needed
- Auto-downloads via `download_datasets.py`

### 2. UCI Online Retail Dataset

**URL:** https://archive.ics.uci.edu/ml/datasets/online+retail

**Size:** ~500K transactions (2010-2011)

**Schema:**
```
InvoiceNo | InvoiceDate | Quantity | UnitPrice | CustomerID | Country | Description | StockCode
```

**Maps to:**
- `sales_fact` (InvoiceDate, Quantity, UnitPrice)
- `customer_dim` (CustomerID, Country)
- `product_dim` (Description)
- `region_dim` (Country grouping)

**Use:** Stress testing, real-world patterns, time-series analysis

**Access:**
- Public dataset, no authentication needed
- Requires `pandas` and `openpyxl` for Excel conversion
- Large download (~20MB)

---

## Data Preparation

### Manual Download

If auto-download fails, manually download CSV files:

1. **Kaggle Sales:**
   - Visit https://www.kaggle.com/datasets/kyanyoga/sample-sales-data
   - Download `sample_sales_data.csv`
   - Save to: `data/raw/kaggle_sales.csv`

2. **UCI Retail:**
   - Visit https://archive.ics.uci.edu/ml/datasets/online+retail
   - Download the Excel file
   - Convert to CSV: `libreoffice --headless --convert-to csv <file.xlsx>`
   - Save to: `data/raw/uci_retail.csv`

### Schema Validation

All downloads are validated against `data/schema_mappings.json`:

```json
{
  "kaggle_sales": {
    "csv_columns": {
      "Order Date": "date_id",
      "Quantity": "quantity",
      "Unit Price": "revenue"
    }
  }
}
```

---

## Database Layout

After seeding with real data, the warehouse structure is:

```
warehouse.db
├── region_dim          (regions/countries from real data)
├── product_dim         (products from real data)
├── customer_dim        (customers from real data)
├── date_dim            (calendar 2022-2024)
└── sales_fact          (~500-500K transaction rows)
```

**Demo mode schema is identical** — only data volume differs.

---

## Evaluation with Real Data

### Run Full Evaluation

```bash
# With real data in database
SEED_MODE=real python -m eval.runner --output eval_real.json
```

### Filter by Category

```bash
# Run only "quality" tests
SEED_MODE=real python -m eval.runner --category quality

# Run multiple categories
SEED_MODE=real python -m eval.runner --category routing quality edge_case
```

### Fast Mode (No LLM Judge)

```bash
# Skip quality scoring, just check routing
SEED_MODE=real python -m eval.runner --no-judge
```

### Stress Test

```bash
# 20 concurrent planner calls with real data
SEED_MODE=real python -m eval.runner --stress 20
```

---

## Environment Variables

| Variable | Values | Default | Purpose |
|----------|--------|---------|---------|
| `SEED_MODE` | `demo`, `real` | `demo` | Which dataset to load |
| `LOAD_REAL_DATA` | `true`, `false` | `false` | Legacy flag (use SEED_MODE) |

### Example

```bash
# All commands with real data
export SEED_MODE=real

# Now all subsequent commands use real data
python main.py
python -m eval.runner
python -m pytest
```

---

## Cost & Performance Notes

### Demo Data
- **Load time:** <1 second
- **Query latency:** 50-200ms
- **API cost:** ~$0.01-0.03 per query
- **Best for:** Development, quick iteration

### Kaggle Data
- **Load time:** ~2 seconds
- **Query latency:** 100-300ms
- **API cost:** ~$0.02-0.05 per query
- **Best for:** Feature testing, small dataset validation

### UCI Data
- **Download time:** ~30 seconds
- **Load time:** ~10-30 seconds
- **Query latency:** 200-500ms
- **API cost:** ~$0.05-0.15 per query (higher complexity)
- **Best for:** Production testing, scale validation

---

## Troubleshooting

### Download Fails

**Error:** `Cannot auto-download kaggle_sales. Manual setup required.`

**Solution:** 
- Manually download from Kaggle
- Save to `data/raw/kaggle_sales.csv`
- Run `python data/seed.py --mode real` again

### Missing Dependencies

**Error:** `openpyxl required for UCI dataset`

**Solution:**
```bash
pip install openpyxl pandas
```

### Date Format Issues

**Error:** `ValueError: time data does not match format`

**Solution:** Verify CSV format matches `schema_mappings.json`
- Kaggle: `M/D/YYYY` (e.g., `1/15/2024`)
- UCI: `D/M/YYYY HH:MM` (e.g., `15/01/2024 10:30`)

### Database Already Exists

**Error:** `schema already exists` or constraint violations

**Solution:** Use `--clear` flag to reset:
```bash
python data/download_datasets.py --clear
```

---

## Adding Your Own Data

### Custom CSV Import

1. Create a `DatasetLoader` subclass in `data/datasets.py`
2. Implement `download()` and `prepare()` methods
3. Register in `get_dataset()` factory function
4. Update `data/schema_mappings.json` with column mappings

**Example:**
```python
class MyCompanyDataset(DatasetLoader):
    def __init__(self):
        super().__init__(
            name="mycompany",
            description="Our internal sales data",
            url="file:///path/to/data.csv"
        )
    
    def download(self) -> Path:
        # Copy from internal location or download
        return Path("data/raw/mycompany.csv")
    
    def prepare(self, csv_path: Path) -> tuple[dict, str]:
        # Map your columns to warehouse schema
        return (
            {
                "SaleDate": "date_id",
                "Units": "quantity",
                "Price": "revenue"
            },
            "%Y-%m-%d"
        )
```

---

## Performance Tips

1. **Index queries:** Add indexes to `date_id`, `product_id` for large datasets
2. **Batch operations:** Load data in chunks if >1M rows
3. **Cache warmup:** Run a few queries before evaluation to warm up semantic cache
4. **Parallel loading:** Use `asyncio` for faster multi-dataset loads

---

## References

- Kaggle Dataset: https://www.kaggle.com/datasets/kyanyoga/sample-sales-data
- UCI Dataset: https://archive.ics.uci.edu/ml/datasets/online+retail
- Schema Mappings: `data/schema_mappings.json`
- Download Script: `data/download_datasets.py`
- Seed Script: `data/seed.py`
