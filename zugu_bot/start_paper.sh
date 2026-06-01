#!/bin/bash
cd /root/zugubotv3

SESSION="zugubotv3"

tmux kill-session -t $SESSION 2>/dev/null

tmux new-session -d -s $SESSION -n scanner
tmux send-keys -t $SESSION:scanner "python3 scanner.py" Enter

tmux new-window -t $SESSION -n resolver
tmux send-keys -t $SESSION:resolver "python3 crypto_ghost_resolver.py" Enter

tmux new-window -t $SESSION -n redeemer
tmux send-keys -t $SESSION:redeemer "python3 crypto_ghost_redeemer.py" Enter

tmux new-window -t $SESSION -n dashboard
tmux send-keys -t $SESSION:dashboard "python3 crypto_ghost_dashboard.py" Enter

tmux new-window -t $SESSION -n monitor
tmux send-keys -t $SESSION:monitor "python3 monitor.py" Enter

echo "zugubotv3 zagnan v paper modu!"
echo "Povezi se z: tmux attach -t $SESSION"
