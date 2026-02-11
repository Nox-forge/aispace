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

# App dependencies (changes when requirements.txt changes)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Fetch app code fresh from GitHub via git clone (bypasses CDN cache)
RUN git clone --depth 1 https://github.com/Nox-forge/aispace.git /tmp/aispace \
    && cp /tmp/aispace/projects/fine-tune-lab/trainer.py /app/ \
    && cp /tmp/aispace/projects/fine-tune-lab/server.py /app/ \
    && rm -rf /tmp/aispace

WORKDIR /app
RUN mkdir -p /data/checkpoints /data/gguf /data/training /data/rag

EXPOSE 8881

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881"]
