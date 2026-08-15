@echo off
REM ============================================================
REM  Multi-Store RAG Chatbot - all services in the current terminal
REM  Redis in Docker (detached); API + Worker in background
REM  (no new windows); Frontend in foreground.
REM  Ctrl+C stops the frontend.  stop.bat kills everything.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo   Multi-Store RAG Chatbot - starting full stack
echo ============================================
echo.

REM --- 1. Infra: Redis (Docker, detached) ---
echo [1/4] Starting Redis (Docker)...
docker compose up -d redis
if errorlevel 1 (
  echo.
  echo ERROR: Docker failed. Is Docker Desktop running?
  pause
  exit /b 1
)

REM --- 2. API (FastAPI / uvicorn) - background, same terminal ---
echo [2/4] Starting API on http://localhost:8000 ...
start "" /B /D "%~dp0backend" cmd /c "call .venv\Scripts\activate.bat && uvicorn app.main:app --reload --port 8000"

REM --- 3. Celery worker - background, same terminal ---
REM  Must consume ALL staged-pipeline queues: with INGESTION_STAGED_ENABLED
REM  the pipeline hands off parse->embed->graph tasks to dedicated queues, so a
REM  worker listening only on ingestion,celery leaves every upload stuck in the
REM  parse queue forever. (solo pool on Windows serves them serially.)
echo [3/4] Starting Celery worker (all pipeline queues)...
start "" /B /D "%~dp0backend" cmd /c "call .venv\Scripts\activate.bat && celery -A app.core.background_tasks worker --loglevel=info -Q ingestion,celery,parse,embed,graph"

REM --- 4. Frontend (Next.js) - foreground, keeps terminal alive ---
echo [4/4] Starting Frontend on http://localhost:3000 ...
echo.
echo ============================================
echo   API       http://localhost:8000/docs
echo   Frontend  http://localhost:3000
echo   Worker    loading BGE model (~60s before ready)
echo ============================================
echo.
echo Ctrl+C stops the frontend.  Run stop.bat to kill everything.
echo.
cd /d "%~dp0frontend"
REM  `npm install` first so a freshly-pulled dependency (e.g. a new
REM  tailwind plugin) can't 500 the whole UI. No-op when deps are in
REM  sync, so the cost is only paid right after a pull.
npm run dev
