#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Clean Neo4j Graph Database
# Wipes all nodes (Document, Entity, Community) and relationships from Neo4j Aura.
# Usage:
#   ./clean_neo4j.sh         (Interactive with confirmation)
#   ./clean_neo4j.sh -y      (Automatic confirmation / force)
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${RED}"
echo "============================================================"
echo "   ⚠️  Multi-Store RAG Chatbot - Neo4j Aura Graph Cleanup"
echo "============================================================"
echo -e "${NC}"

FORCE=false
if [ "$1" == "-y" ] || [ "$1" == "--force" ]; then
    FORCE=true
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

if [ "$FORCE" = true ]; then
    "$PYTHON_CMD" "$SCRIPT_DIR/backend/scripts/clear_neo4j.py" --all --yes
else
    "$PYTHON_CMD" "$SCRIPT_DIR/backend/scripts/clear_neo4j.py"
fi
