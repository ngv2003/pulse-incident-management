#!/bin/bash
# reset.sh — Run this before every demo to get a clean slate.
# Usage: bash reset.sh

echo "[RESET] Clearing database..."
rm -f pulse.db
python3 pulse_core/db.py

echo "[RESET] Recovering mock services (if running)..."
curl -s -X POST http://localhost:8101/scenario/recover 2>/dev/null && echo "[RESET] Services recovered" || echo "[RESET] mock_services.py not running — that's fine"

echo ""
echo "✓ Clean slate. Start order:"
echo "  Terminal 1:  python3 mock_services.py"
echo "  Terminal 2:  python3 pulse_watcher.py"
echo "  Terminal 3:  python3 pulse_api.py"
echo "  Terminal 4:  python3 trigger_pagerduty.py incident1"
echo ""
