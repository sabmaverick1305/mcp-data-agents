"""
ChromaDB client singleton.

Local dev  (default):       PersistentClient writing to data/chroma/
Docker/K8s (CHROMA_HOST):   HttpClient connecting to the ChromaDB container
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
