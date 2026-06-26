@echo off
cd /d "%~dp0"

set HTTP_PROXY=http://127.0.0.1:7890
set HTTPS_PROXY=http://127.0.0.1:7890
set ALL_PROXY=http://127.0.0.1:7890
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost

.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8010
