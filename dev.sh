#!/bin/bash

# M3U Proxy Development Helper Script

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}M3U Streaming Proxy - Development Helper${NC}"
echo "========================================"

# Function to check if virtual environment exists
check_venv() {
    if [ ! -d "$PROJECT_DIR/venv" ]; then
        echo -e "${YELLOW}Virtual environment not found. Creating...${NC}"
        python3 -m venv "$PROJECT_DIR/venv"
        echo -e "${GREEN}Virtual environment created.${NC}"
    fi
}

# Function to activate virtual environment
activate_venv() {
    source "$PROJECT_DIR/venv/bin/activate"
    echo -e "${GREEN}Virtual environment activated.${NC}"
}

# Function to install dependencies
install_deps() {
    echo -e "${YELLOW}Installing Python dependencies...${NC}"
    pip install -r "$PROJECT_DIR/requirements.txt"
    echo -e "${GREEN}Dependencies installed.${NC}"
}

# Function to check FFmpeg
check_ffmpeg() {
    if command -v ffmpeg >/dev/null 2>&1; then
        echo -e "${GREEN}FFmpeg found: $(which ffmpeg)${NC}"
        ffmpeg -version | head -1
    else
        echo -e "${RED}FFmpeg not found! Installing with Homebrew...${NC}"
        if command -v brew >/dev/null 2>&1; then
            brew install ffmpeg
            echo -e "${GREEN}FFmpeg installed.${NC}"
        else
            echo -e "${RED}Homebrew not found. Please install FFmpeg manually.${NC}"
            echo "Visit: https://ffmpeg.org/download.html"
            exit 1
        fi
    fi
}

# Function to start the development server
start_dev() {
    echo -e "${YELLOW}Starting development server...${NC}"
    
    # Load port from .env if available
    if [ -f "$PROJECT_DIR/.env" ]; then
        PORT=$(grep "^PORT=" "$PROJECT_DIR/.env" | cut -d'=' -f2)
        PORT=${PORT:-8080}
    else
        PORT=8080
    fi
    
    echo -e "${BLUE}Server will start on port ${PORT}${NC}"
    
    # Make sure we're in the project directory and activate venv
    cd "$PROJECT_DIR"
    source "$PROJECT_DIR/venv/bin/activate"
    python "$PROJECT_DIR/main.py" --debug --reload
}

# Function to run tests
run_tests() {
    echo -e "${YELLOW}Running tests...${NC}"
    
    # Load port from .env if available
    if [ -f "$PROJECT_DIR/.env" ]; then
        PORT=$(grep "^PORT=" "$PROJECT_DIR/.env" | cut -d'=' -f2)
        PORT=${PORT:-8080}
    else
        PORT=8080
    fi
    
    # Make sure we're in the project directory and activate venv
    cd "$PROJECT_DIR"
    source "$PROJECT_DIR/venv/bin/activate"
    python "$PROJECT_DIR/test_setup.py" "http://localhost:${PORT}"
}

# Function to build Docker image
build_docker() {
    echo -e "${YELLOW}Building Docker image...${NC}"
    docker build -t m3u-proxy "$PROJECT_DIR"
    echo -e "${GREEN}Docker image built successfully.${NC}"
}

# Function to run Docker container
run_docker() {
    echo -e "${YELLOW}Running Docker container...${NC}"
    docker run -d \
        --name m3u-proxy \
        -p 8080:8080 \
        -v /tmp/m3u-proxy-streams:/tmp/m3u-proxy-streams \
        --device /dev/dri:/dev/dri 2>/dev/null || true \
        m3u-proxy
    echo -e "${GREEN}Docker container started.${NC}"
    echo "Container ID: $(docker ps -q --filter name=m3u-proxy)"
}

# Function to stop Docker container
stop_docker() {
    echo -e "${YELLOW}Stopping Docker container...${NC}"
    docker stop m3u-proxy 2>/dev/null || true
    docker rm m3u-proxy 2>/dev/null || true
    echo -e "${GREEN}Docker container stopped.${NC}"
}

# Function to show logs
show_logs() {
    if [ -f "$PROJECT_DIR/m3u-proxy.log" ]; then
        echo -e "${YELLOW}Showing recent logs...${NC}"
        tail -f "$PROJECT_DIR/m3u-proxy.log"
    else
        echo -e "${RED}Log file not found.${NC}"
    fi
}

# Function to clean up
cleanup() {
    echo -e "${YELLOW}Cleaning up...${NC}"
    rm -rf "$PROJECT_DIR"/tmp/stream_*
    rm -f "$PROJECT_DIR"/*.log
    find "$PROJECT_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$PROJECT_DIR" -name "*.pyc" -delete 2>/dev/null || true
    echo -e "${GREEN}Cleanup complete.${NC}"
}

# Function to show project status
show_status() {
    echo -e "${BLUE}Project Status:${NC}"
    echo "==============="
    
    # Check virtual environment
    if [ -d "$PROJECT_DIR/venv" ]; then
        echo -e "Virtual Environment: ${GREEN}✓ Present${NC}"
    else
        echo -e "Virtual Environment: ${RED}✗ Missing${NC}"
    fi
    
    # Check FFmpeg
    if command -v ffmpeg >/dev/null 2>&1; then
        echo -e "FFmpeg: ${GREEN}✓ Available${NC}"
    else
        echo -e "FFmpeg: ${RED}✗ Missing${NC}"
    fi
    
    # Check if server is running
    if [ -f "$PROJECT_DIR/.env" ]; then
        PORT=$(grep "^PORT=" "$PROJECT_DIR/.env" | cut -d'=' -f2)
        PORT=${PORT:-8080}
    else
        PORT=8080
    fi
    
    if curl -s http://localhost:${PORT}/health >/dev/null 2>&1; then
        echo -e "Development Server: ${GREEN}✓ Running on port ${PORT}${NC}"
    else
        echo -e "Development Server: ${YELLOW}○ Stopped${NC}"
    fi
    
    # Check Docker
    if command -v docker >/dev/null 2>&1; then
        echo -e "Docker: ${GREEN}✓ Available${NC}"
        if docker ps --filter name=m3u-proxy --format "table {{.Names}}" | grep -q m3u-proxy; then
            echo -e "Docker Container: ${GREEN}✓ Running${NC}"
        else
            echo -e "Docker Container: ${YELLOW}○ Stopped${NC}"
        fi
    else
        echo -e "Docker: ${YELLOW}○ Not Available${NC}"
    fi
}

# Main script logic
case "${1:-help}" in
    "setup")
        check_venv
        activate_venv
        install_deps
        check_ffmpeg
        echo -e "${GREEN}Setup complete! Run './dev.sh dev' to start development server.${NC}"
        ;;
    "dev")
        check_venv
        activate_venv
        start_dev
        ;;
    "test")
        check_venv
        activate_venv
        run_tests
        ;;
    "docker-build")
        build_docker
        ;;
    "docker-run")
        run_docker
        ;;
    "docker-stop")
        stop_docker
        ;;
    "logs")
        show_logs
        ;;
    "status")
        show_status
        ;;
    "clean")
        cleanup
        ;;
    "help"|*)
        echo "Usage: $0 {setup|dev|test|docker-build|docker-run|docker-stop|logs|status|clean|help}"
        echo ""
        echo "Commands:"
        echo "  setup         - Set up development environment"
        echo "  dev           - Start development server"
        echo "  test          - Run tests"
        echo "  docker-build  - Build Docker image"
        echo "  docker-run    - Run Docker container"
        echo "  docker-stop   - Stop Docker container"
        echo "  logs          - Show application logs"
        echo "  status        - Show project status"
        echo "  clean         - Clean up temporary files"
        echo "  help          - Show this help message"
        ;;
esac
