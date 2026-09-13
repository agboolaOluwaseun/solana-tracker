#!/bin/bash
# Launch all Solana Tracker services:
#   - API server (port 8000)
#   - Streamlit UI (port 8501)
#   - Next.js frontend (port 3000)

set -e

PROJECT_ROOT="/Users/agboolaoluwaseun/ZCodeProject/solana_tracker_v2"
cd "$PROJECT_ROOT"

echo "═══════════════════════════════════════════════════"
echo "  Solana Tracker — Starting all services..."
echo "═══════════════════════════════════════════════════"
echo ""

# Kill any existing instances
pkill -f "uvicorn api.server" 2>/dev/null || true
pkill -f "streamlit run" 2>/dev/null || true
pkill -f "next dev" 2>/dev/null || true
sleep 1

# 1. API server
echo "▶ Starting API server on http://localhost:8000"
PYTHONPATH=. .venv/bin/python -m uvicorn api.server:app --host 127.0.0.1 --port 8000 --reload &
API_PID=$!
sleep 2

# 2. Streamlit
echo "▶ Starting Streamlit on http://localhost:8501"
.venv/bin/python -m streamlit run ui/app.py --server.port 8501 --server.headless true &
STLIT_PID=$!
sleep 2

# 3. Next.js frontend
echo "▶ Starting Next.js frontend on http://localhost:3000"
cd frontend
npm run dev &
NEXT_PID=$!
cd ..

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ All services running!"
echo ""
echo "  API:       http://localhost:8000"
echo "  Streamlit: http://localhost:8501"
echo "  Frontend:  http://localhost:3000"
echo ""
echo "  Press Ctrl+C to stop all services"
echo "═══════════════════════════════════════════════════"
echo ""

# Trap Ctrl+C to kill all children
trap "echo ''; echo 'Stopping all services...'; kill $API_PID $STLIT_PID $NEXT_PID 2>/dev/null; exit 0" INT TERM

# Wait for any child to exit
wait
