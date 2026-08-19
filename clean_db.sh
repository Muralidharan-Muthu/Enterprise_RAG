#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Delete All Records from Supabase
# Safely truncates all documents, chunks, tables, clauses, images, and chat history.
# Usage:
#   ./clean_db.sh         (Interactive with confirmation)
#   ./clean_db.sh -y      (Automatic confirmation / force)
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${RED}"
echo "============================================================"
echo "   ⚠️  Multi-Store RAG Chatbot - Supabase DB Cleanup"
echo "============================================================"
echo -e "${NC}"

FORCE=false
if [ "$1" == "-y" ] || [ "$1" == "--force" ]; then
    FORCE=true
fi

if [ "$FORCE" = false ]; then
    echo -e "${YELLOW}WARNING: This will permanently delete ALL documents, tables,"
    echo -e "clauses, embeddings, images, and chat history from Supabase.${NC}"
    echo ""
    read -p "Are you sure you want to proceed? (y/N): " CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        echo -e "${CYAN}Operation cancelled. No tables were modified.${NC}"
        exit 0
    fi
fi

# Locate Python environment
PYTHON_CMD=""
if [ -f "$SCRIPT_DIR/backend/.venv/Scripts/python.exe" ]; then
    PYTHON_CMD="$SCRIPT_DIR/backend/.venv/Scripts/python.exe"
elif [ -f "$SCRIPT_DIR/backend/.venv/bin/python" ]; then
    PYTHON_CMD="$SCRIPT_DIR/backend/.venv/bin/python"
else
    PYTHON_CMD="python"
fi

echo -e "\n${CYAN}Running database cleanup script...${NC}"
"$PYTHON_CMD" "$SCRIPT_DIR/backend/scripts/truncate_supabase.py"

echo -e "\n${GREEN}============================================================"
echo "   ✓ All Supabase table records have been deleted."
echo "============================================================${NC}"
