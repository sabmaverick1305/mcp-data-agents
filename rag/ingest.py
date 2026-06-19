"""
Document ingestion pipeline — chunks and stores .txt / .pdf files in ChromaDB for RAG retrieval.

This module handles the /ingest REST endpoint and the /ingest CLI command. Ingested
documents are stored in a per-tenant ChromaDB collection (documents_{tenant_id}) and
retrieved at query time to augment agent prompts with domain-specific context.

Chunking strategy:
  CHUNK_SIZE    = 500 characters (roughly 1–2 paragraphs of business text)
  CHUNK_OVERLAP = 80 characters (preserves sentence context across chunk boundaries)
  Minimum chunk length: 40 chars (discards whitespace-only or trivially short chunks)

Collection naming:
  documents_{tenant_id}    e.g. documents_acme, documents_default
  This is separate from the Q&A cache collection (qa_history_{tenant_id}) in store.py,
  so document retrieval and answer caching are independently queryable.

Supported file types:
  .txt   — UTF-8 text, read directly
  .pdf   — requires pypdf (pip install pypdf); pages concatenated with newlines

Retrieval:
  query_documents(question, tenant_id, n_results=4) returns chunks with cosine
  distance < 0.55. The calling code (api.py, main.py) appends these chunks to the
  rag_context string that is injected into the planner's system prompt.

Public API:
  ingest_text(content, source, tenant_id, metadata) → int (chunks stored)
  ingest_file(path, tenant_id)                      → int (chunks stored)
  query_documents(question, tenant_id, n_results)   → list[dict]
  list_sources(tenant_id)                            → list[dict] (unique source names)
"""
import hashlib
import os

from rag.chroma_client import get_client

CHUNK_SIZE    = 500
CHUNK_OVERLAP = 80


def _chunk(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end].strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if len(c) > 40]


def _get_collection(tenant_id: str = "default"):
    return get_client().get_or_create_collection(
        f"documents_{tenant_id}", metadata={"hnsw:space": "cosine"}
    )


def ingest_text(content: str, source: str,
                tenant_id: str = "default",
                metadata: dict | None = None) -> int:
    collection = _get_collection(tenant_id)
    chunks  = _chunk(content)
    base_id = hashlib.md5(source.encode()).hexdigest()[:12]

    ids, docs, metas = [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"doc_{base_id}_{i}")
        docs.append(chunk)
        metas.append({"source": source, "chunk": i, "tenant_id": tenant_id, **(metadata or {})})

    collection.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(chunks)


def ingest_file(path: str, tenant_id: str = "default") -> int:
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")

    ext    = os.path.splitext(path)[1].lower()
    source = os.path.basename(path)

    if ext == ".txt":
        with open(path, encoding="utf-8") as f:
            content = f.read()
    elif ext == ".pdf":
        try:
            import pypdf
            reader  = pypdf.PdfReader(path)
            content = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            raise ImportError("Install pypdf to ingest PDFs: pip install pypdf")
    else:
        raise ValueError(f"Unsupported file type: {ext}. Supported: .txt, .pdf")

    return ingest_text(content, source, tenant_id=tenant_id)


def query_documents(question: str, tenant_id: str = "default", n_results: int = 4) -> list[dict]:
    collection = _get_collection(tenant_id)
    if collection.count() == 0:
        return []
    res = collection.query(
        query_texts=[question],
        n_results=min(n_results, collection.count()),
        include=["documents", "distances", "metadatas"],
    )
    return [
        {"chunk": doc, "source": meta.get("source"), "distance": dist}
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        )
        if dist < 0.55
    ]


def list_sources(tenant_id: str = "default") -> list[dict]:
    collection = _get_collection(tenant_id)
    if collection.count() == 0:
        return []
    result = collection.get(include=["metadatas"])
    seen, sources = set(), []
    for m in result["metadatas"]:
        src = m.get("source")
        if src and src not in seen:
            seen.add(src)
            sources.append({"source": src})
    return sources
