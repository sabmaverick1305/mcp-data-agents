"""
Query Intelligence Layer — structural understanding of incoming questions.

Two classes serve the INTELLIGENCE LAYER and CACHE REGISTRY:

  QueryIntelligence   Classifies a raw question into a QueryIntent using regex
                      heuristics (no LLM cost). Extracts temporal markers,
                      named entities, and metrics. Normalizes the question for
                      better cache key matching.

  SimilarityRules     Maps QueryType → (cache_threshold, rag_threshold).
                      Temporal and comparative queries get tighter thresholds
                      so Q1 answers never satisfy Q2 lookups, etc.

Used before every cache lookup in CacheRegistry so the right thresholds are
applied before ChromaDB's cosine distance is compared.

QueryType taxonomy:
  TEMPORAL     "what happened in Q1 2024?"
  DIMENSIONAL  "break down revenue by region"
  COMPARATIVE  "compare Q1 vs Q2 performance"
  DIAGNOSTIC   "why did revenue drop in Q1?"
  SUMMARY      "executive summary of 2024"
  RANKING      "top 5 products by revenue"
  TRENDING     "revenue trend over last 6 months"
  GENERAL      catch-all for anything else

Normalization:
  - Lowercase and strip trailing punctuation
  - Remove filler phrases ("please", "can you", "show me")
  - Canonicalize time refs ("Q1 2024" → "Q1-2024")
  - Collapse whitespace
  This ensures "What is total revenue?" and "show me total revenue" resolve
  to the same cache key.
"""
import re
from dataclasses import dataclass, field
from enum import Enum


class QueryType(str, Enum):
    TEMPORAL    = "temporal"
    DIMENSIONAL = "dimensional"
    COMPARATIVE = "comparative"
    DIAGNOSTIC  = "diagnostic"
    SUMMARY     = "summary"
    RANKING     = "ranking"
    TRENDING    = "trending"
    GENERAL     = "general"


@dataclass
class QueryIntent:
    question:         str
    query_type:       QueryType
    temporal_markers: list[str]  = field(default_factory=list)
    entities:         list[str]  = field(default_factory=list)
    metrics:          list[str]  = field(default_factory=list)
    complexity:       str        = "medium"    # "simple" | "medium" | "complex"
    normalized:       str        = ""
    suggested_agents: list[str]  = field(default_factory=list)


# ── QueryIntelligence ──────────────────────────────────────────────────────────

class QueryIntelligence:
    """Classify and normalize questions using regex heuristics only (zero LLM cost)."""

    _TEMPORAL_RE = re.compile(
        r'\b(20\d{2})\b'
        r'|\bQ[1-4]\b'
        r'|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?'
        r'|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b'
        r'|\b(?:last|this|next)\s+(?:quarter|month|year|week)\b',
        re.IGNORECASE,
    )

    _REGION_RE = re.compile(
        r'\b(?:North\s+America|Europe|Asia\s+Pacific|Latin\s+America|APAC|EMEA|LATAM|AMER)\b',
        re.IGNORECASE,
    )

    _PRODUCT_RE = re.compile(
        r'\b(?:Software|Infrastructure|Security|Services)\b',
        re.IGNORECASE,
    )

    _METRIC_RE = re.compile(
        r'\b(?:revenue|gross\s+margin|gross\s+profit|growth|LTV|customer\s+lifetime\s+value'
        r'|avg\s+order\s+value|AOV|MoM|YoY|ARR|MRR|churn|conversion|pipeline)\b',
        re.IGNORECASE,
    )

    _SEGMENT_RE = re.compile(
        r'\b(?:Enterprise|Mid-Market|SMB|segment)\b',
        re.IGNORECASE,
    )

    # Intent classifiers — ordered from most specific to least specific
    _WHY_RE      = re.compile(
        r'\b(?:why|reason|cause|root\s+cause|explain|investigate|diagnose|what\s+caused)\b', re.I)
    _COMPARE_RE  = re.compile(
        r'\b(?:vs\.?|versus|compare|comparison|against|benchmark|relative\s+to)\b', re.I)
    _TREND_RE    = re.compile(
        r'\b(?:trend|over\s+time|month.over.month|quarter.over.quarter|trajectory|growth\s+rate)\b', re.I)
    _RANK_RE     = re.compile(
        r'\b(?:top\s+\d+|bottom\s+\d+|best|worst|rank(?:ing)?|highest|lowest|leading|lagging)\b', re.I)
    _SUMMARY_RE  = re.compile(
        r'\b(?:summary|overview|executive|briefing|recap|highlight(?:s)?)\b', re.I)
    _BREAKDOWN_RE = re.compile(
        r'\b(?:break\s*down|by\s+region|by\s+product|by\s+segment|by\s+category|drill.down|split\s+by)\b', re.I)

    _FILLER_RE = re.compile(
        r'\b(?:please|can\s+you|could\s+you|tell\s+me|show\s+me|what\s+(?:is|are)|give\s+me|i\s+want\s+to\s+know)\b',
        re.IGNORECASE,
    )

    _TIME_CANON_RE = re.compile(r'\bq([1-4])\s+(20\d{2})\b', re.IGNORECASE)

    def analyze(self, question: str) -> QueryIntent:
        """Extract intent, entities, temporality and complexity from a raw question."""
        temporal  = [m.group() for m in self._TEMPORAL_RE.finditer(question)]
        regions   = [m.group() for m in self._REGION_RE.finditer(question)]
        products  = [m.group() for m in self._PRODUCT_RE.finditer(question)]
        segments  = [m.group() for m in self._SEGMENT_RE.finditer(question)]
        metrics   = [m.group() for m in self._METRIC_RE.finditer(question)]
        entities  = list({e.title() for e in (regions + products + segments)})

        # Intent: match from most specific to most general
        if self._WHY_RE.search(question):
            qtype = QueryType.DIAGNOSTIC
        elif self._COMPARE_RE.search(question):
            qtype = QueryType.COMPARATIVE
        elif self._TREND_RE.search(question):
            qtype = QueryType.TRENDING
        elif self._RANK_RE.search(question):
            qtype = QueryType.RANKING
        elif self._SUMMARY_RE.search(question):
            qtype = QueryType.SUMMARY
        elif self._BREAKDOWN_RE.search(question):
            qtype = QueryType.DIMENSIONAL
        elif temporal:
            qtype = QueryType.TEMPORAL
        else:
            qtype = QueryType.GENERAL

        # Complexity: count distinct features
        feat = len(temporal) + len(entities) + len(metrics)
        if feat >= 4 or (self._COMPARE_RE.search(question) and len(temporal) >= 2):
            complexity = "complex"
        elif feat >= 2:
            complexity = "medium"
        else:
            complexity = "simple"

        # Suggest agents based on type (hint for planner, not a mandate)
        suggested: list[str] = {
            QueryType.TEMPORAL:    ["semantic", "benchmark"],
            QueryType.DIAGNOSTIC:  ["semantic", "insight"],
            QueryType.COMPARATIVE: ["benchmark", "insight"],
            QueryType.TRENDING:    ["semantic", "benchmark"],
            QueryType.SUMMARY:     ["semantic", "benchmark", "insight"],
            QueryType.RANKING:     ["benchmark", "insight"],
            QueryType.DIMENSIONAL: ["insight"],
            QueryType.GENERAL:     ["semantic"],
        }.get(qtype, ["semantic"])

        return QueryIntent(
            question=question,
            query_type=qtype,
            temporal_markers=temporal,
            entities=entities,
            metrics=metrics,
            complexity=complexity,
            normalized=self.normalize(question),
            suggested_agents=suggested,
        )

    def normalize(self, question: str) -> str:
        """
        Canonicalize a question to improve cache key matching.

        "What is total revenue?" and "Show me total revenue" → same normalized string.
        "Q1 2024 revenue" and "Q1-2024 revenue" → same.
        """
        q = question.lower().strip().rstrip("?.,!")
        q = self._FILLER_RE.sub("", q)
        q = self._TIME_CANON_RE.sub(r"Q\1-\2", q)
        return re.sub(r"\s+", " ", q).strip()


# ── SimilarityRules ────────────────────────────────────────────────────────────

class SimilarityRules:
    """
    Map QueryType → (cache_threshold, rag_threshold) for ChromaDB cosine distance.

    Lower threshold = stricter matching (less likely to return a wrong cached answer).
    Temporal and comparative queries use the tightest thresholds because a small
    wording difference (Q1 vs Q2) implies a completely different answer.
    General and summary queries can tolerate looser matches.

    Defaults (when no intent is available):
      cache_threshold = 0.10 (same as rag/store.py CACHE_THRESHOLD)
      rag_threshold   = 0.50 (same as rag/store.py RAG_THRESHOLD)
    """

    _CACHE_THRESHOLDS: dict[QueryType, float] = {
        QueryType.TEMPORAL:    0.05,   # tight — time period confusion is dangerous
        QueryType.COMPARATIVE: 0.05,   # tight — entity names must match precisely
        QueryType.DIAGNOSTIC:  0.08,   # moderate
        QueryType.DIMENSIONAL: 0.08,
        QueryType.RANKING:     0.10,   # normal
        QueryType.TRENDING:    0.10,
        QueryType.SUMMARY:     0.12,   # loose — summaries are broadly similar
        QueryType.GENERAL:     0.10,
    }

    _RAG_THRESHOLDS: dict[QueryType, float] = {
        QueryType.TEMPORAL:    0.40,
        QueryType.COMPARATIVE: 0.40,
        QueryType.DIAGNOSTIC:  0.50,
        QueryType.DIMENSIONAL: 0.50,
        QueryType.RANKING:     0.55,
        QueryType.TRENDING:    0.55,
        QueryType.SUMMARY:     0.60,
        QueryType.GENERAL:     0.50,
    }

    def get_thresholds(self, intent: QueryIntent) -> tuple[float, float]:
        """Return (cache_threshold, rag_threshold) for this query intent."""
        ct = self._CACHE_THRESHOLDS.get(intent.query_type, 0.10)
        rt = self._RAG_THRESHOLDS.get(intent.query_type, 0.50)

        # Tighten cache further if multiple distinct temporal markers are present
        # (cross-period comparison like "Q1 vs Q2" needs even stricter matching)
        if len(intent.temporal_markers) >= 2:
            ct = max(0.03, ct - 0.02)

        return ct, rt

    def cache_threshold(self, intent: QueryIntent) -> float:
        return self.get_thresholds(intent)[0]

    def rag_threshold(self, intent: QueryIntent) -> float:
        return self.get_thresholds(intent)[1]
