#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Stop All Services
# Stops background API, Celery Worker, Frontend, and Docker Redis.
# ==============================================================================

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Autodetect and include Docker Desktop in PATH if installed in AppData or Program Files
for DOCKER_DIR in \
    "$LOCALAPPDATA/Programs/DockerDesktop/resources/bin" \
    "/c/Users/mural/AppData/Local/Programs/DockerDesktop/resources/bin" \
    "/c/Program Files/Docker/Docker/resources/bin"; do
    if [ -d "$DOCKER_DIR" ]; then
        export PATH="$DOCKER_DIR:$PATH"
    fi
done

echo -e "${CYAN}"
echo "============================================================"
echo "   🛑 Multi-Store RAG Chatbot - Stopping All Services"
echo "============================================================"
echo -e "${NC}"

# 1. Stop background API from PID file or process search
echo -e "${BLUE}[1/3] Stopping FastAPI backend...${NC}"
if [ -f "$SCRIPT_DIR/.api.pid" ]; then
    API_PID=$(cat "$SCRIPT_DIR/.api.pid" 2>/dev/null || true)
    if [ -n "$API_PID" ]; then
        kill "$API_PID" 2>/dev/null || true
    fi
    rm -f "$SCRIPT_DIR/.api.pid"
fi
pkill -f "uvicorn app.main:app" 2>/dev/null || true
echo -e "${GREEN}  ✓ FastAPI backend stopped.${NC}"

# 2. Stop Celery worker from PID file or process search
echo -e "\n${BLUE}[2/3] Stopping Celery worker...${NC}"
if [ -f "$SCRIPT_DIR/.worker.pid" ]; then
    WORKER_PID=$(cat "$SCRIPT_DIR/.worker.pid" 2>/dev/null || true)
    if [ -n "$WORKER_PID" ]; then
        kill "$WORKER_PID" 2>/dev/null || true
    fi
    rm -f "$SCRIPT_DIR/.worker.pid"
fi
pkill -f "celery -A app.core.background_tasks" 2>/dev/null || true
echo -e "${GREEN}  ✓ Celery worker stopped.${NC}"

# 3. Stop Docker Redis
echo -e "\n${BLUE}[3/3] Stopping Docker Redis container...${NC}"
if command -v docker &> /dev/null; then
    if docker compose version &> /dev/null; then
        docker compose stop redis 2>/dev/null || true
    elif command -v docker-compose &> /dev/null; then
        docker-compose stop redis 2>/dev/null || true
    fi
    echo -e "${GREEN}  ✓ Redis container stopped.${NC}"
fi

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}   ✓ All Multi-Store RAG services have been stopped.${NC}"
echo -e "${GREEN}============================================================${NC}\n"
