"""Embedding client for Ollama's nomic-embed-text model."""

import json
import subprocess
import numpy as np
from typing import Optional


class EmbeddingClient:
    """Generate text embeddings via local Ollama."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def embed(self, text: str, prefix: str = "search_document") -> np.ndarray:
        """Generate embedding for text with optional task prefix.

        nomic-embed-text uses prefixes for best retrieval quality:
        - 'search_document' for content being stored
        - 'search_query' for search queries
        - None for no prefix
        """
        if prefix:
            text = f"{prefix}: {text}"

        result = subprocess.run(
            [
                "curl", "-s",
                f"{self.base_url}/api/embeddings",
                "-d", json.dumps({"model": self.model, "prompt": text}),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            raise RuntimeError(f"Ollama embedding failed: {result.stderr}")

        data = json.loads(result.stdout)
        if "embedding" not in data:
            raise RuntimeError(f"No embedding in response: {data}")

        return np.array(data["embedding"], dtype=np.float32)

    def embed_batch(self, texts: list[str], prefix: str = "search_document") -> list[np.ndarray]:
        """Generate embeddings for multiple texts."""
        return [self.embed(text, prefix) for text in texts]

    def health_check(self) -> bool:
        """Check if Ollama is running and model is available."""
        try:
            result = subprocess.run(
                ["curl", "-s", f"{self.base_url}/api/tags"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
            models = [m["name"] for m in data.get("models", [])]
            return any(self.model in m for m in models)
        except Exception:
            return False


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm == 0:
        return 0.0
    return float(dot / norm)


def batch_cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Compute cosine similarity between a query vector and a matrix of vectors.

    Returns array of similarity scores, one per row in matrix.
    """
    query_norm = np.linalg.norm(query)
    if query_norm == 0:
        return np.zeros(matrix.shape[0])

    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0  # avoid division by zero

    return np.dot(matrix, query) / (norms * query_norm)
