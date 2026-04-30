@echo off
echo ==========================================
echo  Crypto Ghost Scanner - LIVE TRADE MODE
echo  AI Brain + Scanner + Resolver + Redeemer + Dashboard
echo  !! REAL MONEY WILL BE SPENT !!
echo ==========================================
echo.
echo Are you sure you want to run with REAL MONEY?
echo Press Ctrl+C to cancel, or...
pause

set SCRIPT_DIR=%~dp0
set PAPER_TRADE=false

start "Ghost Brain [AI]" /D "%SCRIPT_DIR%brain" cmd /k "set PAPER_TRADE=false&& python ghost_brain.py"
timeout /t 4 /nobreak >nul

start "CryptoGhost Scanner [LIVE]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=false&& python crypto_ghost_scanner.py"
timeout /t 3 /nobreak >nul

start "CryptoGhost Resolver [LIVE]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=false&& python crypto_ghost_resolver.py"
timeout /t 2 /nobreak >nul

start "CryptoGhost Redeemer [LIVE]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=false&& python crypto_ghost_redeemer.py"
timeout /t 2 /nobreak >nul

start "CryptoGhost Dashboard [LIVE]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=false&& python crypto_ghost_dashboard.py"

echo Live trading started in 5 windows.
echo Brain output: brain\ghost_brain.json (refreshed every 10s)
echo Trade DB:     crypto_ghost.db
