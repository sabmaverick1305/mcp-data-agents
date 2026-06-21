"""
Context Assembly Engine — builds a structured, priority-ordered context package
before each planning call.

Previously, context was assembled ad-hoc by string concatenation across three
different places in _run_pipeline. This module centralizes that logic with:

  Priority ordering (highest → lowest):
    1. Long-term memory context  (LTM patterns + past analyses + user prefs)
    2. RAG semantic cache context (ChromaDB Q&A pairs + domain knowledge)
    3. Conversation history      (last HISTORY_TURNS turns, summarized)
    4. Ingested document chunks  (user-uploaded .txt files)

  Token budget enforcement:
    Soft cap of MAX_CONTEXT_CHARS characters. Truncation removes from the
    lowest-priority sources first. LTM context is never truncated — it's
    always short and highest-signal.

  Source tracking:
    ContextPackage.sources_used records which sources contributed, enabling
    the /query trace to explain what context was available.

Output:
  ContextPackage.to_planner_string() → the formatted string to inject into
  the planner's uncached dynamic block.

  ContextPackage.to_history_list() → list[dict] for the conversation_history
  param of planner.create_plan() (separated from the RAG context string to
  allow the planner to use it for follow-up resolution).
"""
from dataclasses import dataclass, field

MAX_CONTEXT_CHARS = 8_000   # ~2k tokens; beyond this the planner prompt gets unwieldy
HISTORY_TURNS     = 3       # how many recent Q&A pairs to include
MAX_HISTORY_CHARS = 1_500   # per history block


@dataclass
class ContextPackage:
    """Assembled context from all sources, ready for injection into the planner."""

    ltm_context:     str        = ""
    rag_context:     str        = ""
    history_context: str        = ""
    doc_context:     str        = ""
    total_chars:     int        = 0
    truncated:       bool       = False
    sources_used:    list[str]  = field(default_factory=list)

    def to_planner_string(self) -> str:
        """Return the ordered, formatted string to inject into the planner dynamic block."""
        parts: list[str] = []
        if self.ltm_context:
            parts.append(self.ltm_context)
        if self.rag_context:
            parts.append(self.rag_context)
        if self.history_context:
            parts.append(self.history_context)
        if self.doc_context:
            parts.append(self.doc_context)
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        return not any([self.ltm_context, self.rag_context,
                        self.history_context, self.doc_context])


class ContextAssemblyEngine:
    """
    Assemble, prioritize, and budget context from multiple sources.

    None or empty strings for any source are silently skipped.
    """

    def assemble(
        self,
        question: str,          # noqa: ARG002 — reserved for future query-aware truncation
        rag_context: str        = "",
        ltm_context: str        = "",
        history: list[dict]     = None,
        doc_chunks: list[dict]  = None,
    ) -> ContextPackage:
        """
        Build a ContextPackage from the provided sources.

        Parameters
        ----------
        question    : the current question (reserved for future relevance-aware trimming)
        rag_context : ChromaDB L2 output (Q&A pairs + domain knowledge snippets)
        ltm_context : long-term memory string from LongTermMemory.get_context_for_planner()
        history     : full conversation history list (dicts with 'question'/'answer')
        doc_chunks  : ingested document chunks [{source, chunk}]
        """
        pkg       = ContextPackage()
        remaining = MAX_CONTEXT_CHARS

        # ── Priority 1: LTM context (always included, never truncated) ─────────
        if ltm_context and ltm_context.strip():
            pkg.ltm_context = ltm_context
            remaining -= len(ltm_context)
            pkg.sources_used.append("ltm")

        # ── Priority 2: RAG semantic cache + domain knowledge ──────────────────
        if rag_context and remaining > 500:
            allowed = min(len(rag_context), remaining - 300)   # leave headroom for history
            if allowed > 0:
                trimmed = rag_context[:allowed]
                if len(trimmed) < len(rag_context):
                    trimmed += "\n…[rag context truncated]"
                    pkg.truncated = True
                pkg.rag_context = trimmed
                remaining -= len(trimmed)
                pkg.sources_used.append("rag")

        # ── Priority 3: Conversation history (last N turns, summarized) ────────
        if history and remaining > 300:
            recent = history[-HISTORY_TURNS:]
            lines: list[str] = ["Recent conversation:"]
            for h in recent:
                q = h.get("question", "")
                a = h.get("answer", "")[:200]
                if a and not a.endswith("…"):
                    a += "…"
                lines.append(f"Q: {q}\nA: {a}")
            history_str = "\n\n".join(lines)
            allowed = min(len(history_str), MAX_HISTORY_CHARS, remaining - 100)
            if allowed > 0:
                pkg.history_context = history_str[:allowed]
                remaining -= len(pkg.history_context)
                pkg.sources_used.append("history")

        # ── Priority 4: Ingested document chunks ───────────────────────────────
        if doc_chunks and remaining > 200:
            chunks_str = "\n\n".join(
                f"[Doc: {c['source']}]\n{c['chunk']}" for c in doc_chunks
            )
            allowed = min(len(chunks_str), remaining)
            if allowed > 0:
                trimmed = chunks_str[:allowed]
                if len(trimmed) < len(chunks_str):
                    pkg.truncated = True
                pkg.doc_context = trimmed
                pkg.sources_used.append("docs")

        pkg.total_chars = (
            len(pkg.ltm_context)
            + len(pkg.rag_context)
            + len(pkg.history_context)
            + len(pkg.doc_context)
        )
        return pkg
