FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && rm -rf /var/lib/apt/lists/*

# Clone llama.cpp for GGUF conversion
RUN git clone --depth 1 https://github.com/ggerganov/llama.cpp.git /opt/llama.cpp \
    && pip install --no-cache-dir gguf numpy sentencepiece

# App dependencies (rarely changes — cached)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Cache bust: this URL changes on every git push, forcing re-fetch of code below
ADD https://api.github.com/repos/Nox-forge/aispace/git/refs/heads/master /tmp/cache_bust.json

# Fetch app code fresh from GitHub (not from build context cache)
RUN curl -sL https://raw.githubusercontent.com/Nox-forge/aispace/master/projects/fine-tune-lab/trainer.py -o /app/trainer.py \
    && curl -sL https://raw.githubusercontent.com/Nox-forge/aispace/master/projects/fine-tune-lab/server.py -o /app/server.py

WORKDIR /app
RUN mkdir -p /data/checkpoints /data/gguf /data/training

EXPOSE 8881

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881"]
