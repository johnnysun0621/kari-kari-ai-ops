"""OpenAI-compatible LLM response parsing shared by AI features."""
from typing import Any


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
