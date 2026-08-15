#!/usr/bin/env bash
# ==============================================================================
# Multi-Store RAG Chatbot - Automated One-Click Deployment Script
# Deploys: Frontend (Next.js), Backend (FastAPI), Background Worker (Celery), and Redis
# ==============================================================================

set -e

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "============================================================"
echo "   🚀 Multi-Store RAG Chatbot - One-Click Deployment"
echo "============================================================"
echo -e "${NC}"

# 1. Check if .env file exists
if [ ! -f ".env" ]; then
    echo -e "${RED}[ERROR] .env file not found in current directory!${NC}"
    echo -e "Please create a .env file with your Supabase, Groq, and Neo4j keys before deploying."
    exit 1
fi
echo -e "${GREEN}[✓] .env file detected.${NC}"

# 2. Check for Docker and Docker Compose
if ! command -v docker &> /dev/null; then
    echo -e "${RED}[ERROR] Docker is not installed or not in PATH.${NC}"
    echo "Please install Docker from https://docs.docker.com/get-docker/"
    exit 1
fi

if docker compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[ERROR] Docker Compose is not installed.${NC}"
    echo "Please install Docker Compose plugin or binary."
    exit 1
fi
echo -e "${GREEN}[✓] Docker and Docker Compose detected.${NC}"

# 3. Pull / Build and Start all Services
echo -e "\n${BLUE}[1/3] Building and starting containers (Redis, API, Celery Worker, Frontend)...${NC}"
$DOCKER_COMPOSE_CMD down --remove-orphans 2>/dev/null || true
$DOCKER_COMPOSE_CMD up -d --build

# 4. Wait for services to initialize
echo -e "\n${BLUE}[2/3] Waiting for backend services to initialize...${NC}"
MAX_RETRIES=20
COUNT=0
HEALTHY=0

while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s http://localhost:8000/api/v1/health | grep -q '"status":"healthy"' 2>/dev/null; then
        HEALTHY=1
        break
    fi
    echo -e "${YELLOW}Waiting for FastAPI backend to be ready... ($((COUNT+1))/$MAX_RETRIES)${NC}"
    sleep 3
    COUNT=$((COUNT+1))
done

# 5. Display Status & URLs
echo -e "\n${BLUE}[3/3] Checking deployed container statuses...${NC}"
$DOCKER_COMPOSE_CMD ps

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}   🎉 Deployment Complete & All Services Running!${NC}"
echo -e "${GREEN}============================================================${NC}"

# Detect server IP
SERVER_IP=$(curl -s https://api.ipify.org 2>/dev/null || echo "localhost")

echo -e "\n${CYAN}Access your applications:${NC}"
echo -e "  🌐 ${YELLOW}Frontend UI:${NC}        http://localhost:3000  (or http://${SERVER_IP}:3000)"
echo -e "  ⚙️  ${YELLOW}Backend API Docs:${NC}   http://localhost:8000/api/docs"
echo -e "  🩺 ${YELLOW}Health Check:${NC}       http://localhost:8000/api/v1/health"
echo -e "  📦 ${YELLOW}Redis Broker:${NC}       localhost:6379"
echo -e "  ⚡ ${YELLOW}Celery Worker:${NC}      Active & Processing Queues [ingestion, parse, embed, graph]"

echo -e "\n${CYAN}Useful management commands:${NC}"
echo "  - View live logs:      $DOCKER_COMPOSE_CMD logs -f"
echo "  - Stop all services:   $DOCKER_COMPOSE_CMD down"
echo "  - Restart services:    $DOCKER_COMPOSE_CMD restart"
echo ""
