@echo off
title CryptoGhost Redeemer
cd /d "%~dp0"
echo ==========================================
echo   CRYPTO GHOST AUTO-REDEEMER
echo   Mode determined by PAPER_TRADE in .env
echo ==========================================
echo.
python crypto_ghost_redeemer.py
pause
