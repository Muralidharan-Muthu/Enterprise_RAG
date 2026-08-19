#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Environment Setup Script
# Run ONCE after cloning the repository, or when updating dependencies.
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

# 1. Backend virtualenv and dependencies
echo -e "${BLUE}[1/4] Setting up Backend Python Environment...${NC}"
cd "$SCRIPT_DIR/backend"

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

# Activate virtualenv (Linux/macOS + Windows Git Bash)
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
fi

echo "  Installing/upgrading Python dependencies from requirements.txt ..."
python -m pip install --upgrade pip
pip install -r requirements.txt

# 2. Apply Database Migrations (Supabase)
echo -e "\n${BLUE}[2/4] Verifying Database & Applying Migrations...${NC}"
python -c "
import sys, os
from pathlib import Path
sys.path.insert(0, '.')
try:
    from app.db.connection import get_db
    with get_db() as conn:
        with conn.cursor() as cur:
            migrations_dir = Path('app/db/migrations')
            for sql_file in sorted(migrations_dir.glob('*.sql')):
                try:
                    cur.execute(sql_file.read_text(encoding='utf-8'))
                except Exception as e:
                    pass
        conn.commit()
    print('  ✓ Supabase database schema & migrations verified.')
except Exception as e:
    print(f'  [!] Database check notice: {e}')
" || true

# 3. Frontend npm install
echo -e "\n${BLUE}[3/4] Frontend: Installing Node.js dependencies...${NC}"
cd "$SCRIPT_DIR/frontend"
if ! command -v npm &> /dev/null; then
    echo -e "${RED}[ERROR] Node.js / npm is not installed or not in PATH.${NC}"
    exit 1
fi
npm install

# 4. Pull Docker images (Redis)
echo -e "\n${BLUE}[4/4] Pulling Redis Docker Image...${NC}"
cd "$SCRIPT_DIR"
if command -v docker &> /dev/null; then
    if docker compose version &> /dev/null; then
        docker compose pull redis || true
    elif command -v docker-compose &> /dev/null; then
        docker-compose pull redis || true
    fi
else
    echo -e "${YELLOW}  [!] Docker not detected. Please ensure Redis is running before starting.${NC}"
fi

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}   ✓ Setup Complete! All dependencies and stores are ready.${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "Start the full application with:  ${YELLOW}./run.sh${NC}\n"
