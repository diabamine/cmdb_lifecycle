#!/bin/bash
# Verify minimal offline setup for CMDB matching

echo "=== CMDB Lifecycle Matcher - Offline Readiness Check ==="
echo

echo "✓ REQUIRED files (MUST be present):"
for f in snapshot/endoflife_full.json snapshot/redhat_lifecycle.json; do
    if [ -f "$f" ]; then
        size=$(du -h "$f" | cut -f1)
        echo "  ✅ $f ($size)"
    else
        echo "  ❌ MISSING: $f"
    fi
done
echo

echo "🔄 RECOMMENDED files (enhances matching):"
for f in snapshot/vendor_lifecycle.json snapshot/cisa_kev.json snapshot/epss_scores.csv.gz; do
    if [ -f "$f" ]; then
        size=$(du -h "$f" | cut -f1)
        echo "  ✅ $f ($size)"
    else
        echo "  ⚠️  optional: $f"
    fi
done
echo

echo "📊 OPTIONAL files (for version-level CVEs):"
for f in snapshot/nvd.sqlite; do
    if [ -f "$f" ]; then
        size=$(du -h "$f" | cut -f1)
        echo "  ✅ $f ($size)"
    else
        echo "  ⚠️  optional: $f (skipping saves ~302MB)"
    fi
done
echo

# Total size
if [ -d snapshot ]; then
    total=$(du -sh snapshot | cut -f1)
    echo "Total snapshot/ size: $total"
    echo
fi

# Test core functionality
if [ -f snapshot/endoflife_full.json ] && [ -f snapshot/redhat_lifecycle.json ]; then
    echo "Testing core matching engine..."
    python3 << 'PYEOF'
import sys
try:
    import cmdb_enrich as ce
    from pathlib import Path
    ref = ce.Ref(Path("snapshot"))
    print(f"✅ Reference loaded: {len(ref.prods)} products, {len(ref.index)} name keys")
    print(f"   - endoflife.date: {sum(1 for p in ref.prods.values() if p['source']=='endoflife.date')} products")
    print(f"   - Red Hat: {sum(1 for p in ref.prods.values() if p['source']=='redhat')} products")
    print(f"   - Vendors: {sum(1 for p in ref.prods.values() if p['source'] not in ('endoflife.date','redhat'))} products")
    if any(p['kev'] for p in ref.prods.values()):
        print(f"   - With KEV data: {sum(1 for p in ref.prods.values() if p['kev'])} products")
    if ref.nvd:
        print(f"   - NVD database: connected")
except Exception as e:
    print(f"❌ Failed: {e}")
    sys.exit(1)
PYEOF
else
    echo "❌ REQUIRED files missing - cannot test"
    exit 1
fi
echo

echo "🎯 For offline use:"
echo "  1. Ensure endoflife_full.json and redhat_lifecycle.json are present"
echo "  2. (Optional) Add vendor_lifecycle.json for vendor coverage"
echo "  3. (Optional) Add cisa_kev.json + epss_scores.csv.gz for exploit risk"
echo "  4. (Optional) Add nvd.sqlite for version-level CVEs"
echo
echo "To run on your CMDB:"
echo "  - UI: streamlit run app.py  (then upload your CSV)"
echo "  - CLI: python3 -I cmdb_enrich.py --snapshot snapshot --cmdb your_data.csv --out output"
