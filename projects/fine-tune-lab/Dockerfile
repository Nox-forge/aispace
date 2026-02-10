FROM nvidia/cuda:12.6.0-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    git cmake build-essential wget curl \
    && rm -rf /var/lib/apt/lists/*

# Make python3.11 the default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# Upgrade pip
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Install PyTorch with CUDA 12.6
RUN python3 -m pip install --no-cache-dir \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# Clone llama.cpp for GGUF conversion
RUN git clone --depth 1 https://github.com/ggerganov/llama.cpp.git /opt/llama.cpp \
    && cd /opt/llama.cpp \
    && python3 -m pip install --no-cache-dir -r requirements.txt 2>/dev/null || true \
    && python3 -m pip install --no-cache-dir gguf numpy sentencepiece

# App dependencies
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

# App code
COPY trainer.py server.py /app/
WORKDIR /app

# Data directories
RUN mkdir -p /data/checkpoints /data/gguf /data/training

EXPOSE 8881

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881"]
