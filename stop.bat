@echo off
REM ============================================================
REM  Multi-Store RAG Chatbot - stop everything started by run.bat
REM ============================================================
setlocal
cd /d "%~dp0"

echo Stopping native processes (API, Worker, Frontend)...
REM uvicorn + celery run under python.exe; Next.js dev runs under node.exe
taskkill /F /IM python.exe  >nul 2>&1
taskkill /F /IM node.exe    >nul 2>&1
echo   Native processes killed.

echo Stopping Docker infra (Redis + Neo4j)...
docker compose stop redis neo4j

echo.
echo Done. (Redis/Neo4j data is kept in Docker volumes.)
echo WARNING: this kills ALL python.exe and node.exe on this machine,
echo          not only Multi-Store RAG Chatbot. Close other Python/Node apps first if needed.
pause
