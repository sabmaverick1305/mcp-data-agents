"""
RAG (Retrieval-Augmented Generation) package.

Three modules work together to give agents access to domain knowledge and
cached previous answers without hitting the LLM on every request.

chroma_client   Singleton factory for the ChromaDB client. Switches between
                PersistentClient (local dev, data/chroma/) and HttpClient
                (Docker / Kubernetes) based on CHROMA_HOST env var.

store           RAGStore — the semantic response cache and domain knowledge base.
                Collections:
                  domain_knowledge        schema docs + metric definitions (shared)
                  qa_history_{tenant_id}  per-tenant Q&A cache (cosine distance)
                Cache logic:
                  distance < 0.10 → exact-ish match → return cached answer
                  distance < 0.50 → related context → inject into prompt
                TTL: 24 h. Temporal tokens (year/quarter/month) guard against
                returning a stale answer to a different time-period question.

ingest          Document ingestion pipeline for the /ingest REST endpoint and
                /ingest CLI command. Splits .txt / .pdf files into 500-char
                chunks (80-char overlap), stores in documents_{tenant_id}
                collection, and exposes query_documents() for retrieval.

Data flow (query path):
  user question
    → store.retrieve(question) → cached_answer or rag_context
    → ingest.query_documents(question) → doc chunks appended to rag_context
    → planner receives rag_context in system prompt
    → agents use it to ground their answers
"""
