@echo off
echo ==========================================
echo  Crypto Ghost Scanner - PAPER TRADE MODE
echo  Scanner + Resolver + Redeemer + Dashboard
echo  No real money will be spent.
echo ==========================================
echo.

set SCRIPT_DIR=%~dp0
set PAPER_TRADE=true

start "CryptoGhost Scanner [PAPER]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=true&& python crypto_ghost_scanner.py"
timeout /t 3 /nobreak >nul

start "CryptoGhost Resolver [PAPER]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=true&& python crypto_ghost_resolver.py"
timeout /t 2 /nobreak >nul

start "CryptoGhost Redeemer [PAPER]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=true&& python crypto_ghost_redeemer.py"
timeout /t 2 /nobreak >nul

start "CryptoGhost Dashboard [PAPER]" /D "%SCRIPT_DIR%" cmd /k "set PAPER_TRADE=true&& python crypto_ghost_dashboard.py"

echo Paper trading started in 4 windows.
echo Trade DB: crypto_ghost_PAPER.db
