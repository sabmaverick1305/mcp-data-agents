"""
LLM client factory — Anthropic direct API or AWS Bedrock.

Controlled by environment variables:
  USE_BEDROCK=true          Use AWS Bedrock (requires boto3 + IAM credentials)
  BEDROCK_REGION            AWS region (default: us-east-1)
  BEDROCK_MODEL_ID          Override Bedrock model ID
  ANTHROPIC_API_KEY         Used when USE_BEDROCK is not set

Bedrock authentication uses the standard boto3 credential chain:
  1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
  2. ~/.aws/credentials
  3. EKS pod service account (IRSA) — the production path
  4. EC2 instance profile

Usage:
  client = make_client()          # AsyncAnthropic or AsyncAnthropicBedrock
  model  = default_model()        # correct model ID for whichever backend is active
"""
import os

# ── Config ────────────────────────────────────────────────────────────────────

USE_BEDROCK     = os.environ.get("USE_BEDROCK", "false").lower() == "true"
BEDROCK_REGION  = os.environ.get("BEDROCK_REGION", "us-east-1")

# Cross-region inference profile ID for Claude 3.5 Sonnet v2
_BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
_DIRECT_DEFAULT_MODEL  = "claude-sonnet-4-6"

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", _BEDROCK_DEFAULT_MODEL)


def make_client():
    """
    Return an async Anthropic client for either the direct API or AWS Bedrock.
    Both expose the same `.messages.create` / `.messages.stream` interface.
    """
    if USE_BEDROCK:
        from anthropic import AsyncAnthropicBedrock
        return AsyncAnthropicBedrock(aws_region=BEDROCK_REGION)
    else:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        return anthropic.AsyncAnthropic(api_key=api_key)


def default_model() -> str:
    """Return the correct model identifier for the active backend."""
    return BEDROCK_MODEL_ID if USE_BEDROCK else _DIRECT_DEFAULT_MODEL


def backend_label() -> str:
    """Human-readable label for startup logging."""
    if USE_BEDROCK:
        return f"AWS Bedrock ({BEDROCK_REGION}) · {BEDROCK_MODEL_ID}"
    return f"Anthropic API · {_DIRECT_DEFAULT_MODEL}"
