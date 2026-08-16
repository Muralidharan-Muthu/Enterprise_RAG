#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Run Full Stack
# Redis (Docker), FastAPI Backend (Background), Celery Worker (Background),
# and Next.js Frontend (Foreground).
# Press Ctrl+C or run ./stop.sh to stop all services.
# ==============================================================================

set -e

# ANSI Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${CYAN}"
echo "============================================================"
echo "   🚀 Multi-Store RAG Chatbot - Starting Full Stack"
echo "============================================================"
echo -e "${NC}"

# Cleanup handler on exit / Ctrl+C
cleanup() {
    echo -e "\n${YELLOW}Stopping background services...${NC}"
    if [ -f "$SCRIPT_DIR/.api.pid" ]; then
        API_PID=$(cat "$SCRIPT_DIR/.api.pid" 2>/dev/null || true)
        if [ -n "$API_PID" ]; then
            kill "$API_PID" 2>/dev/null || true
        fi
        rm -f "$SCRIPT_DIR/.api.pid"
    fi

    if [ -f "$SCRIPT_DIR/.worker.pid" ]; then
        WORKER_PID=$(cat "$SCRIPT_DIR/.worker.pid" 2>/dev/null || true)
        if [ -n "$WORKER_PID" ]; then
            kill "$WORKER_PID" 2>/dev/null || true
        fi
        rm -f "$SCRIPT_DIR/.worker.pid"
    fi
    echo -e "${GREEN}Services stopped.${NC}"
}
trap cleanup EXIT INT TERM

# 1. Start Redis in Docker
echo -e "${BLUE}[1/4] Starting Redis (Docker)...${NC}"
if command -v docker &> /dev/null; then
    if docker compose version &> /dev/null; then
        docker compose up -d redis
    elif command -v docker-compose &> /dev/null; then
        docker-compose up -d redis
    fi
    echo -e "${GREEN}  ✓ Redis running on localhost:6379${NC}"
else
    echo -e "${YELLOW}  [!] Docker not found. Assuming external/cloud Redis is configured in .env${NC}"
fi

# Activate virtual environment for backend
cd "$SCRIPT_DIR/backend"
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
else
    echo -e "${RED}[ERROR] backend/.venv not found. Please run ./setup.sh first.${NC}"
    exit 1
fi

# 2. Start FastAPI API Server in Background
echo -e "\n${BLUE}[2/4] Starting FastAPI Backend on http://localhost:8000 ...${NC}"
uvicorn app.main:app --reload --port 8000 &
API_PID=$!
echo $API_PID > "$SCRIPT_DIR/.api.pid"
echo -e "${GREEN}  ✓ FastAPI started (PID: $API_PID)${NC}"

# 3. Start Celery Ingestion Worker in Background
echo -e "\n${BLUE}[3/4] Starting Celery Worker (Queues: ingestion, celery, parse, embed, graph)...${NC}"
celery -A app.core.background_tasks worker --loglevel=info -Q ingestion,celery,parse,embed,graph &
WORKER_PID=$!
echo $WORKER_PID > "$SCRIPT_DIR/.worker.pid"
echo -e "${GREEN}  ✓ Celery worker started (PID: $WORKER_PID)${NC}"

# 4. Start Next.js Frontend in Foreground
echo -e "\n${BLUE}[4/4] Starting Next.js Frontend on http://localhost:3000 ...${NC}"
echo -e "${CYAN}============================================================${NC}"
echo -e "  🌐 ${YELLOW}Frontend UI:${NC}        http://localhost:3000"
echo -e "  ⚙️  ${YELLOW}Backend Docs:${NC}       http://localhost:8000/api/docs"
echo -e "  🩺 ${YELLOW}Health Check:${NC}       http://localhost:8000/api/v1/health"
echo -e "  📦 ${YELLOW}Redis:${NC}              localhost:6379"
echo -e "  ⚡ ${YELLOW}Celery Worker:${NC}      Active"
echo -e "${CYAN}============================================================${NC}"
echo -e "${YELLOW}Press Ctrl+C or run ./stop.sh to stop all services.${NC}\n"

cd "$SCRIPT_DIR/frontend"
npm run dev
