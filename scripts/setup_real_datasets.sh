#!/bin/bash
set -e

echo "=========================================="
echo "MCP Data Agents — Real Dataset Setup"
echo "=========================================="
echo ""

# Check for required dependencies
echo "Checking dependencies..."
if ! python -c "import pandas" 2>/dev/null; then
    echo "❌ pandas required. Install: pip install pandas"
    exit 1
fi

if ! python -c "import openpyxl" 2>/dev/null; then
    echo "⚠️  openpyxl recommended for UCI dataset. Install: pip install openpyxl"
fi

# Step 1: Download datasets
echo ""
echo "Step 1: Downloading datasets..."
python data/download_datasets.py

# Step 2: Seed database
echo ""
echo "Step 2: Seeding warehouse with real data..."
export SEED_MODE=real
python data/seed.py

# Step 3: Run evaluation
echo ""
echo "Step 3: Running evaluation with real data..."
echo "(This will take a few minutes...)"
python -m eval.runner --output eval_real_datasets.json

# Summary
echo ""
echo "=========================================="
echo "✓ Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Review evaluation results:"
echo "     cat eval_real_datasets.json"
echo "  2. Run CLI with real data:"
echo "     SEED_MODE=real python main.py"
echo "  3. Query API with real data:"
echo "     curl -X POST http://127.0.0.1:8000/query -H \"X-Tenant-ID: demo\" -d '{\"question\": \"...\"}'"
echo ""
