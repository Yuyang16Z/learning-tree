"""Pinned, CPU-only local models. Query-time inference never downloads weights.

Only public model weights are downloaded during explicit preparation. Documents,
queries and memory content remain in the local process. Cross-encoder outputs are
sigmoid scores in [0, 1], not raw logits or calibrated confidence values.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
RERANKER_LICENSE = "apache-2.0"
MODEL_KEY = f"{EMBEDDING_MODEL}@{EMBEDDING_REVISION}"
CACHE_DIR = Path(__file__).resolve().parents[1] / ".runtime" / "retrieval" / "models"
_MODEL_FILES = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "modules.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
]


class SemanticUnavailable(Exception):
    """The caller should use lexical retrieval; messages never contain user text."""


def _enabled() -> bool:
    # Importing settings does not open the application database or call a provider.
    from .config import settings

    mode = getattr(settings, "memory_retrieval_mode", os.getenv("MEMORY_RETRIEVAL_MODE", "hybrid"))
    return str(mode).lower() != "lexical"


class SemanticBackend:
    model_key = MODEL_KEY

    def __init__(self) -> None:
        self._embedding: Any = None
        self._reranker: Any = None
        self._state_lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._preparing = False
        self._worker: threading.Thread | None = None
        self._download_requested = False

    def status(self) -> dict:
        with self._state_lock:
            enabled = _enabled()
            embedding_ready = enabled and self._embedding is not None
            reranker_ready = enabled and self._reranker is not None
            if not enabled:
                state = "disabled"
            elif self._preparing:
                state = "preparing"
            elif embedding_ready and reranker_ready:
                state = "ready"
            else:
                state = "degraded"
            return {
                "state": state,
                "embedding_ready": embedding_ready,
                "reranker_ready": reranker_ready,
            }

    def _require(self, name: str):
        if not _enabled():
            raise SemanticUnavailable("Local semantic retrieval is disabled")
        with self._state_lock:
            model = self._embedding if name == "embedding" else self._reranker
        if model is None:
            raise SemanticUnavailable("Local semantic model is not ready")
        return model

    def embed_query(self, query: str) -> list[float]:
        return self._encode(["query: " + query])[0]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        if not documents:
            return []
        return self._encode(["passage: " + text for text in documents])

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._require("embedding")
        try:
            with self._inference_lock:
                values = model.encode(
                    texts,
                    batch_size=16,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                )
            return values.tolist()
        except Exception:
            with self._state_lock:
                if self._embedding is model:
                    self._embedding = None
            raise SemanticUnavailable("Local embedding inference is unavailable") from None

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = self._require("reranker")
        try:
            import torch

            with self._inference_lock:
                values = model.predict(
                    [(query, text) for text in documents],
                    batch_size=8,
                    show_progress_bar=False,
                    activation_fn=torch.nn.Sigmoid(),
                    convert_to_numpy=True,
                )
            return [float(value) for value in values.reshape(-1)]
        except Exception:
            with self._state_lock:
                if self._reranker is model:
                    self._reranker = None
            raise SemanticUnavailable("Local reranking inference is unavailable") from None

    def _load(self, allow_download: bool) -> None:
        if not _enabled():
            return
        # Native libraries read these flags at import; cap CPU use on laptops.
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "2")
        os.environ.setdefault("MKL_NUM_THREADS", "2")
        import torch
        from huggingface_hub import snapshot_download
        from sentence_transformers import CrossEncoder, SentenceTransformer

        torch.set_num_threads(2)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            # PyTorch permits this only before its first parallel operation.
            pass
        if allow_download:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, repo, revision in [
            ("embedding", EMBEDDING_MODEL, EMBEDDING_REVISION),
            ("reranker", RERANKER_MODEL, RERANKER_REVISION),
        ]:
            if not _enabled():
                return
            with self._state_lock:
                if getattr(self, "_" + name) is not None:
                    continue
            try:
                path = snapshot_download(
                    repo_id=repo,
                    revision=revision,
                    cache_dir=str(CACHE_DIR),
                    allow_patterns=_MODEL_FILES,
                    local_files_only=not allow_download,
                    token=False,
                    max_workers=2,
                )
                if not _enabled():
                    return
                if name == "embedding":
                    model = SentenceTransformer(
                        path,
                        device="cpu",
                        local_files_only=True,
                        trust_remote_code=False,
                        model_kwargs={"use_safetensors": True},
                    )
                    model.max_seq_length = 512
                else:
                    model = CrossEncoder(
                        path,
                        device="cpu",
                        max_length=512,
                        local_files_only=True,
                        trust_remote_code=False,
                        model_kwargs={"use_safetensors": True},
                    )
                with self._state_lock:
                    if _enabled():
                        setattr(self, "_" + name, model)
            except Exception:
                # Partial readiness is useful: an embedding-only fallback can
                # still work. Do not expose hub errors, filesystem paths or text.
                continue

    def prepare(self, allow_download: bool) -> dict:
        if not _enabled():
            return self.status()
        with self._prepare_lock:
            with self._state_lock:
                self._preparing = True
            try:
                self._load(allow_download)
            except Exception:
                pass
            finally:
                with self._state_lock:
                    self._preparing = False
        return self.status()

    def _background(self, allow_download: bool) -> None:
        while True:
            self.prepare(allow_download)
            with self._state_lock:
                # If an explicit preparation request arrives during cached-only
                # warming, honor it once that work finishes rather than dropping
                # the request or launching a competing download.
                if self._download_requested and _enabled():
                    self._download_requested = False
                    self._preparing = True
                    allow_download = True
                    continue
                self._download_requested = False
                self._worker = None
                return

    def start(self, allow_download: bool) -> dict:
        if not _enabled():
            return self.status()
        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                self._download_requested = self._download_requested or allow_download
                return self.status()
            if self._embedding is not None and self._reranker is not None:
                return self.status()
            self._preparing = True
            self._worker = threading.Thread(
                target=self._background,
                args=(allow_download,),
                name="learning-tree-local-retrieval",
                daemon=True,
            )
            self._worker.start()
        return self.status()


_BACKEND = SemanticBackend()


def get_backend() -> SemanticBackend:
    return _BACKEND


def get_status() -> dict:
    return _BACKEND.status()


def start_prepare() -> dict:
    """Start explicitly authorized public-weight downloads without blocking HTTP."""
    return _BACKEND.start(allow_download=True)


def warm_cached() -> dict:
    """Load cached public weights in the background, with all downloads disabled."""
    return _BACKEND.start(allow_download=False)


def prepare() -> dict:
    """Synchronous setup for the preparation CLI; only this path may download."""
    return _BACKEND.prepare(allow_download=True)
