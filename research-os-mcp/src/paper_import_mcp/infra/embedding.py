"""本地 BGE 向量化；模型按需加载，推理不阻塞事件循环。"""

from __future__ import annotations

import asyncio

from paper_import_mcp.config import settings


class BGEEmbedder:
    def __init__(self, model_name: str = settings.graph_embedding_model) -> None:
        self.model_name = model_name
        self._model = None

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed_sync, text)

    def _embed_sync(self, text: str) -> list[float]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        vector = self._model.encode(text, normalize_embeddings=True)
        return vector.tolist()
