#!/bin/bash
# Start Xvfb in background
Xvfb :99 -screen 0 1280x1024x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
# Wait a moment for Xvfb to start
sleep 2
# Execute app.py
exec python app.py
