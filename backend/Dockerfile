FROM python:3.11-slim

WORKDIR /app

# System dependencies for Docling OCR
# (libgl1 replaces the obsolete libgl1-mesa-glx, removed in Debian 12 "bookworm")
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Pre-install CPU-only torch to avoid 2.5GB CUDA wheels crashing Docker Desktop
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
