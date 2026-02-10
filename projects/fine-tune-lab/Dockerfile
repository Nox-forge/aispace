FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System deps (git for llama.cpp clone)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Clone llama.cpp for GGUF conversion (Python script only, no build needed)
RUN git clone --depth 1 https://github.com/ggerganov/llama.cpp.git /opt/llama.cpp \
    && pip install --no-cache-dir gguf numpy sentencepiece

# App dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App code
COPY trainer.py server.py /app/
WORKDIR /app

# Data directories
RUN mkdir -p /data/checkpoints /data/gguf /data/training

EXPOSE 8881

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881"]
