#!/bin/bash

# Graceful shutdown handler
cleanup() {
    echo "🛑 Shutting down services..."
    kill $WEB_PID 2>/dev/null
    wait
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGTERM SIGINT

echo "🚀 Starting ABS-KoSync Enhanced (Integrated Mode)..."
echo ""

# Start the unified web server (includes integrated sync daemon)
echo "  🌐 Starting unified service (web + sync daemon)..."
python /app/src/web_server.py &
WEB_PID=$!

echo ""
echo "✅ Service started successfully!"
echo "   • Unified Service PID: $WEB_PID"
echo "   • Web UI available at: http://localhost:5757"
echo "   • Sync daemon running in background thread"
echo ""
echo "Press Ctrl+C to stop..."

# Wait for the process to exit
wait
