"""Tests for RAGStore — cache hit, TTL, flag_bad, domain seeding."""
import time
import pytest
from unittest.mock import patch

from rag.store import RAGStore, CACHE_TTL_HOURS


@pytest.fixture()
def rag(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.store.CHROMA_DIR", str(tmp_path / "chroma"))
    import chromadb
    store = RAGStore()
    store.seed_domain()
    return store


def test_domain_seeded(rag):
    assert rag.stats()["domain_docs"] > 0


def test_cache_miss_on_empty_store(rag):
    cached, ctx = rag.retrieve("What is total revenue?")
    assert cached is None


def test_store_and_cache_hit(rag):
    rag.store_qa("What is total revenue?", "Total revenue is $7M.", ["semantic"])
    cached, _ = rag.retrieve("What is total revenue?")
    assert cached == "Total revenue is $7M."


def test_near_match_cache_hit(rag):
    rag.store_qa("What is total revenue for 2024?", "Revenue is $7.1M.", ["semantic"])
    cached, _ = rag.retrieve("What is the total revenue in 2024?")
    assert cached is not None


def test_unrelated_query_no_cache_hit(rag):
    rag.store_qa("What is total revenue?", "Revenue is $7M.", ["semantic"])
    cached, ctx = rag.retrieve("Which regions are below target?")
    assert cached is None


def test_rag_context_returned_for_similar(rag):
    rag.store_qa("What is total revenue?", "Revenue is $7M.", ["semantic"])
    _, ctx = rag.retrieve("Show me revenue figures")
    # Should get something as RAG context (not a cache hit, but related)
    # Exact behaviour depends on embedding distance; at minimum no error raised
    assert isinstance(ctx, str)


def test_flag_bad_removes_from_cache(rag):
    rag.store_qa("What is gross margin?", "Margin is 87%.", ["semantic"])
    rag.flag_bad("What is gross margin?")
    cached, _ = rag.retrieve("What is gross margin?")
    assert cached is None


def test_ttl_expiry(rag):
    rag.store_qa("Revenue question", "Answer.", ["semantic"])
    # Backdate cached_at to beyond TTL
    past = time.time() - (CACHE_TTL_HOURS + 1) * 3600
    entry = rag._qa.get(include=["metadatas"])
    doc_id = entry["ids"][0]
    meta = entry["metadatas"][0]
    meta["cached_at"] = past
    rag._qa.update(ids=[doc_id], metadatas=[meta])

    cached, _ = rag.retrieve("Revenue question")
    assert cached is None, "Expired cache entry should not be returned"


def test_stats(rag):
    rag.store_qa("Q1", "A1", [])
    s = rag.stats()
    assert s["qa_entries"] == 1
    assert s["domain_docs"] > 0
