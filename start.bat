@echo off
title MT5 Basket Bot - localhost dashboard
cd /d "%~dp0"
echo Menjalankan dashboard di http://127.0.0.1:8895/
start "" http://127.0.0.1:8895/
python server.py
pause
