#!/bin/bash
# Stop all Solana Tracker services

echo "Stopping Solana Tracker services..."

pkill -f "uvicorn api.server" 2>/dev/null && echo "  ✓ API server stopped" || echo "  - API server not running"
pkill -f "streamlit run" 2>/dev/null && echo "  ✓ Streamlit stopped" || echo "  - Streamlit not running"
pkill -f "next dev" 2>/dev/null && echo "  ✓ Next.js stopped" || echo "  - Next.js not running"

echo ""
echo "All services stopped."
