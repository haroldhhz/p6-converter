@echo off
REM P6 Converter — Start backend server
REM Uses Windows cert bundle instead of certifi (corporate environment)
set SSL_CERT_FILE=C:\Users\haroldhuang\Documents\cacert.pem
set SSL_CERT_DIR=C:\Users\haroldhuang\Documents\cacert.pem
cd /d "%~dp0backend"
call ..\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8002
