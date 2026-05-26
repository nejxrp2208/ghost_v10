@echo off
echo ==========================================
echo  Crypto Ghost Scanner - Installer
echo ==========================================
echo.

REM Install dependencies
echo Installing Python dependencies...
pip install aiohttp>=3.9.0 py-clob-client>=0.16.0 web3>=6.0.0 python-dotenv>=1.0.0 websockets>=12.0

echo.
echo ==========================================
echo  Installation complete!
echo.
echo  To start the scanner, run:
echo    start_crypto_scanner.bat
echo ==========================================
pause
