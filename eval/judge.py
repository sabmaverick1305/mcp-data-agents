"""
LLM-as-judge for answer quality evaluation.

Scores each answer on four dimensions (1–5 each):
  relevance     — does it directly answer the question?
  specificity   — does it include concrete numbers / data?
  format        — appropriate use of markdown, tables, structure?
  groundedness  — is it grounded in retrieved data, not hallucinated?

Returns a QualityScore with per-dimension scores and an overall mean.
"""
import json
import re

import anthropic
from dataclasses import dataclass


@dataclass
class QualityScore:
    relevance:    float
    specificity:  float
    format:       float
    groundedness: float

    @property
    def overall(self) -> float:
        return round(
            (self.relevance + self.specificity + self.format + self.groundedness) / 4, 2
        )

    def to_dict(self) -> dict:
        return {
            "relevance":    self.relevance,
            "specificity":  self.specificity,
            "format":       self.format,
            "groundedness": self.groundedness,
            "overall":      self.overall,
        }


_JUDGE_SYSTEM = """You are an expert evaluator for AI-generated data analytics answers.
Score the answer on exactly these four dimensions (integers 1–5):

relevance    — 1 = completely off-topic, 5 = directly and fully answers the question
specificity  — 1 = vague generalities only, 5 = concrete numbers, percentages, dates
format       — 1 = unstructured wall of text, 5 = well-structured markdown with tables where useful
groundedness — 1 = contains made-up facts or hallucinations, 5 = all claims traceable to data

Respond with ONLY a JSON object, no markdown:
{"relevance": <int>, "specificity": <int>, "format": <int>, "groundedness": <int>, "reasoning": "<one sentence>"}"""


async def score_answer(
    client: anthropic.AsyncAnthropic,
    question: str,
    answer: str,
    model: str = "claude-sonnet-4-6",
) -> QualityScore:
    """Run the LLM judge and return a QualityScore. Falls back to zeros on parse failure."""
    prompt = f"Question: {question}\n\nAnswer:\n{answer}"
    response = await client.messages.create(
        model=model,
        max_tokens=256,
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        data = json.loads(raw)
        return QualityScore(
            relevance    = float(data.get("relevance",    0)),
            specificity  = float(data.get("specificity",  0)),
            format       = float(data.get("format",       0)),
            groundedness = float(data.get("groundedness", 0)),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return QualityScore(relevance=0, specificity=0, format=0, groundedness=0)
