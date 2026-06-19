"""
ChromaDB client singleton — local PersistentClient or remote HttpClient.

Provides a single shared ChromaDB client instance for the entire application.
All RAG modules (store.py, ingest.py) call get_client() rather than constructing
their own clients, ensuring one connection pool is reused across all collections.

Runtime selection:
  Local dev (CHROMA_HOST not set):
    chromadb.PersistentClient(path=data/chroma/)
    Collections are stored as files under data/chroma/<collection-uuid>/.
    Data persists across restarts without any external service.

  Docker / Kubernetes (CHROMA_HOST set):
    chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    Points to the chromadb service defined in docker-compose.yml or the
    chromadb-service ClusterIP in k8s/chromadb-statefulset.yaml.
    Allows ChromaDB to run as a separate scalable pod.

Environment variables:
  CHROMA_HOST   hostname of the ChromaDB HTTP server (empty = use local)
  CHROMA_PORT   port of the ChromaDB HTTP server (default: 8000)

Note: The singleton is module-level (_client). It is initialised on first call to
get_client() and reused for the lifetime of the process. This is safe because
ChromaDB clients are not connection-pooled per-request but are long-lived objects.
"""
import os

import chromadb

CHROMA_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chroma")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))

_client = None


def get_client():
    global _client
    if _client is None:
        if CHROMA_HOST:
            _client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        else:
            _client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _client
