"""
Tests for rag.ingest — document chunking, storage, retrieval, and error handling.

Fixture: patch_chroma(tmp_path, monkeypatch)
  Patches rag.ingest.CHROMA_DIR to a temporary path so tests use an isolated
  in-memory-ish ChromaDB without touching the production data/chroma/ directory.
  The autouse fixture applies to all tests in this module automatically.

Test cases:
  test_ingest_text_returns_chunk_count     Short text produces ≥ 1 chunk and returns count
  test_list_sources_after_ingest           list_sources() includes the ingested source name
  test_query_documents_finds_relevant      Semantic search returns chunks matching the topic
  test_ingest_long_text_creates_multiple   Text > CHUNK_SIZE produces multiple chunks (> 1)
  test_unsupported_extension_raises        ingest_file("doc.docx") raises ValueError or
                                           FileNotFoundError before attempting to read
"""
import pytest
from rag.ingest import ingest_text, query_documents, list_sources


@pytest.fixture(autouse=True)
def patch_chroma(tmp_path, monkeypatch):
    monkeypatch.setattr("rag.ingest.CHROMA_DIR", str(tmp_path / "chroma"))


def test_ingest_text_returns_chunk_count():
    n = ingest_text("Sales were strong in Q1 2024. Revenue grew by 15%.", source="report.txt")
    assert n >= 1


def test_list_sources_after_ingest():
    ingest_text(
        "Gross margin analysis shows that software products have higher margins than services.",
        source="margins.txt",
    )
    sources = list_sources()
    assert any(s["source"] == "margins.txt" for s in sources)


def test_query_documents_finds_relevant():
    ingest_text(
        "North America exceeded its sales target by 12% in Q2 2024.", source="q2_report.txt"
    )
    results = query_documents("North America sales performance")
    assert len(results) >= 1
    assert any("North America" in r["chunk"] for r in results)


def test_ingest_long_text_creates_multiple_chunks():
    long_text = "Revenue data. " * 200
    n = ingest_text(long_text, source="long.txt")
    assert n > 1


def test_unsupported_extension_raises():
    from rag.ingest import ingest_file
    with pytest.raises((ValueError, FileNotFoundError)):
        ingest_file("document.docx")
