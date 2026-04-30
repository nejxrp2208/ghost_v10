@echo off
echo ==========================================
echo  MarketGhost — 24/7 Read-only Data Collector
echo  Records every BTC/ETH/SOL/XRP Up/Down market
echo  Does NOT trade. Safe to leave running.
echo ==========================================
echo.

set SCRIPT_DIR=%~dp0
start "MarketGhost Collector" cmd /k "cd /d %SCRIPT_DIR% && python marketghost.py"
echo Collector running. Data will land in marketghost.db
echo Run 'python marketghost_stats.py' to see stats.
