"""OpenAI-compatible LLM response parsing shared by AI features."""
from typing import Any


class UnsupportedLLMProviderError(ValueError):
    """Raised before a request when the configured provider is prohibited."""


def reject_prohibited_provider(*, base_url: str, model: str) -> None:
    configured_provider = f"{base_url} {model}".lower()
    if "longcat" in configured_provider or "long cat" in configured_provider:
        raise UnsupportedLLMProviderError(
            "LongCat is prohibited for this project; configure an approved LLM provider."
        )


def configure_chat_completion(
    payload: dict[str, Any],
    *,
    base_url: str,
    model: str,
    max_tokens: int,
) -> None:
    """Apply provider-compatible output settings without hiding answer tokens."""
    reject_prohibited_provider(base_url=base_url, model=model)
    payload["max_completion_tokens"] = max_tokens
    if model.lower().startswith("gpt-"):
        payload["reasoning_effort"] = "none"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(text, dict) and isinstance(text.get("value"), str):
            parts.append(text["value"])
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def extract_text(payload: Any) -> str:
    """Extract answer text without exposing provider reasoning fields."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                text = _content_text(message.get("content"))
                if text:
                    return text
            text = _content_text(choice.get("text"))
            if text:
                return text
    return _content_text(payload.get("output_text"))
