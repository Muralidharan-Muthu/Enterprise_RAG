#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - First-time Setup Script
# Run ONCE after cloning / pulling the repository, then use ./run.sh to start.
# ==============================================================================

set -e

# ANSI Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
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
echo "   🚀 Multi-Store RAG Chatbot - Environment Setup"
echo "============================================================"
echo -e "${NC}"

# 1. Git pull
echo -e "${BLUE}[1/4] Pulling latest code from main...${NC}"
git pull origin main || echo -e "${YELLOW}Git pull skipped or working on local branch.${NC}"

# 2. Backend virtualenv and dependencies
echo -e "\n${BLUE}[2/4] Backend: virtualenv and Python dependencies...${NC}"
cd "$SCRIPT_DIR/backend"

# Determine python command
if command -v python3 &> /dev/null; then
    PY_CMD="python3"
elif command -v python &> /dev/null; then
    PY_CMD="python"
else
    echo -e "${RED}[ERROR] Python 3 is not installed or not in PATH.${NC}"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment in backend/.venv ..."
    $PY_CMD -m venv .venv
fi

# Activate virtualenv (handles both Linux/macOS and Git Bash on Windows)
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
fi

echo "  Upgrading pip and installing requirements.txt ..."
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. Frontend npm install
echo -e "\n${BLUE}[3/4] Frontend: Installing npm dependencies...${NC}"
cd "$SCRIPT_DIR/frontend"
if ! command -v npm &> /dev/null; then
    echo -e "${RED}[ERROR] Node.js / npm is not installed or not in PATH.${NC}"
    exit 1
fi
npm install

# 4. Pull Docker images (Redis)
echo -e "\n${BLUE}[4/4] Pulling Docker images (Redis)...${NC}"
cd "$SCRIPT_DIR"
if command -v docker &> /dev/null; then
    if docker compose version &> /dev/null; then
        docker compose pull redis || true
    elif command -v docker-compose &> /dev/null; then
        docker-compose pull redis || true
    fi
else
    echo -e "${YELLOW}[WARNING] Docker not detected. Please ensure Redis is running before starting the app.${NC}"
fi

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}   ✓ Setup Complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "\nEnsure your ${CYAN}.env${NC} file has your API keys (Supabase, Groq, Neo4j)."
echo -e "Start the full application with:  ${YELLOW}./run.sh${NC}"
echo ""
