"""
Evaluation package — automated quality and routing accuracy assessment.

Three modules run the full evaluation suite against the live agent pipeline:

dataset     Ground-truth evaluation cases (EvalCase dataclass + EVAL_DATASET list).
            Each case specifies: question, expected_agents, required_keywords,
            forbidden_phrases, and category (routing | quality | edge_case | security).
            The dataset covers single-agent routing, multi-agent orchestration, edge cases,
            and adversarial prompt injection attempts.

judge       LLM-as-judge scorer. Uses Claude to rate each answer on four dimensions
            (1–5): relevance, specificity, format, groundedness. Returns a QualityScore
            dataclass with per-dimension values and an overall mean.

runner      Orchestrates the full eval loop:
              1. Runs check_query (security layer) — expects injection cases to be blocked.
              2. Calls planner.create_plan — checks routing accuracy against expected_agents.
              3. Runs the full agent pipeline (semantic + benchmark + insight + synthesis).
              4. Checks required_keywords and forbidden_phrases in the answer.
              5. Optionally calls the LLM judge for quality scoring.
              6. Computes an EvalReport with routing accuracy, avg quality, latency, cost.
            Also provides stress_test() for concurrent planner load testing.

Usage:
    python -m eval.runner                       # full eval, all categories
    python -m eval.runner --category routing    # routing accuracy only
    python -m eval.runner --stress 20           # 20 concurrent planner calls
    python -m eval.runner --no-judge            # skip LLM judge (faster)
    python -m eval.runner --output report.json  # write JSON report
"""
