"""OpenRouter clients: chat completions for SQL and the d1 decisions API."""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from dex.config import ENV_FILE

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CHAT_TIMEOUT_SECONDS = 60
DECISION_TIMEOUT_SECONDS = 8
DECISION_MODEL = "liquid/d1-ultra"


class LlmError(Exception):
    """OpenRouter returned an error or an unusable response."""


@dataclass(frozen=True)
class ModelProfile:
    """One SQL-writing model and the request settings it is benchmarked with."""

    key: str
    model: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Completion:
    text: str
    model: str
    elapsed_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    raise LlmError(f"OPENROUTER_API_KEY is not set (checked the environment and {ENV_FILE})")


def _post(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "X-Title": "dex",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        raise LlmError(f"OpenRouter {error.code} for {payload.get('model')}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise LlmError(f"OpenRouter unreachable for {payload.get('model')}: {error}") from None
    if "error" in body:
        raise LlmError(f"OpenRouter error for {payload.get('model')}: {body['error']}")
    return body


def chat(profile: ModelProfile, messages: list[dict], max_tokens: int = 1500) -> Completion:
    started = time.perf_counter()
    payload = {"model": profile.model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
    payload.update(profile.extra)
    body = _post(CHAT_URL, payload, CHAT_TIMEOUT_SECONDS)
    choices = body.get("choices") or []
    if not choices:
        raise LlmError(f"{profile.model} returned no choices")
    text = choices[0].get("message", {}).get("content") or ""
    usage = body.get("usage") or {}
    return Completion(
        text=text,
        model=body.get("model", profile.model),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost_usd=float(usage.get("cost") or 0.0),
    )


@dataclass
class Decision:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    elapsed_ms: float


def decide(state: str, instructions: str, criteria: dict[str, str]) -> Decision:
    """One `choice` question to the decision model."""
    started = time.perf_counter()
    body = _post(
        DECISIONS_URL,
        {
            "model": DECISION_MODEL,
            "state": state,
            "questions": {"decision": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        },
        DECISION_TIMEOUT_SECONDS,
    )
    answer = (body.get("answers") or {}).get("decision")
    if not answer or answer.get("choice") not in criteria:
        raise LlmError(f"{DECISION_MODEL} returned no usable choice: {body}")
    return Decision(
        choice=answer["choice"],
        confidence=float(answer.get("confidence") or 0.0),
        probabilities={label: float(value) for label, value in (answer.get("probabilities") or {}).items()},
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
