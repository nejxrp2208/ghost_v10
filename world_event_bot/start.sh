#!/bin/bash
cd /root/ghost_v10/world_event_bot
exec /root/ghost_v10/venv/bin/streamlit run app.py \
  --server.port 8502 \
  --server.headless true \
  --server.address 0.0.0.0
