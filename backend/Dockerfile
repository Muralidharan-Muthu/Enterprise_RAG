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

# Pre-bake Docling models into the Docker image
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='ds4sd/docling-models', revision='v2.1.0', local_dir='/app/docling_models')"

COPY . .

# HF Spaces requires port 7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
