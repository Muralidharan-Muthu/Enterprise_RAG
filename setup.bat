@echo off
REM ============================================================
REM  Multi-Store RAG Chatbot - first-time setup after cloning / pulling main
REM  Run ONCE after `git pull`, then use run.bat to start.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo   Multi-Store RAG Chatbot - first-time setup
echo ============================================
echo.

echo [1/4] Pulling latest from main...
git pull origin main

echo.
echo [2/4] Backend: virtualenv + Python dependencies...
cd /d "%~dp0backend"
if not exist .venv (
  echo   Creating .venv ...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo [3/4] Frontend: npm install...
cd /d "%~dp0frontend"
call npm install

echo.
echo [4/4] Pulling Docker images (Redis + Neo4j)...
cd /d "%~dp0"
docker compose pull redis neo4j

echo.
echo ============================================
echo   Setup complete.
echo ============================================
echo.
echo BEFORE running, make sure these exist (NOT in git):
echo   - .env            (root)   Supabase + Gemma + Neo4j keys
echo   - backend\.env    optional override
echo.
echo Then start everything with:  run.bat
echo.
pause
