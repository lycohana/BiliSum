ANTHROPIC_API_VERSION = "2023-06-01"


def normalize_llm_thinking_type(value: str | None, default: str = "enabled") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"enabled", "disabled"} else default


def normalize_llm_reasoning_effort(value: str | None, default: str = "low") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"low", "high", "max"} else default


def apply_openai_reasoning_settings(
    payload: dict[str, object],
    *,
    thinking_type: str | None,
    reasoning_effort: str | None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    """Add reasoning controls only for model families known to accept them.

    The settings are exposed for OpenAI-compatible endpoints, but the request
    fields are not part of the OpenAI-compatible contract. Sending them to an
    arbitrary endpoint can turn an otherwise valid request into a 400. MiMo
    is the currently supported model family; unknown providers/models keep
    the original payload unchanged until their request schema is verified.
    """
    next_payload = dict(payload)
    normalized_provider = str(provider or "").strip().lower().replace("_", "-")
    normalized_model = normalize_openai_compatible_model_name(
        str(model or next_payload.get("model") or "")
    ).lower()
    supports_controls = (
        normalized_provider in {"mimo", "xiaomi-mimo"}
        or normalized_model.startswith(("mimo-", "mimo_"))
    )
    if not supports_controls:
        return next_payload
    next_payload["thinking"] = {"type": normalize_llm_thinking_type(thinking_type)}
    next_payload["reasoning_effort"] = normalize_llm_reasoning_effort(reasoning_effort)
    return next_payload


def normalize_openai_compatible_model_name(model: str) -> str:
    normalized = str(model or "").strip()
    if normalized.lower().startswith("mimo-"):
        return normalized.lower()
    return normalized


def normalize_llm_provider(provider: str | None) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"anthropic", "claude"}:
        return "anthropic"
    if normalized in {"openai", "openai-compatible", "openai_compatible", "custom"}:
        return normalized.replace("_", "-")
    return "openai-compatible"


def is_anthropic_llm(provider: str | None, base_url: str | None = None) -> bool:
    if normalize_llm_provider(provider) == "anthropic":
        return True
    normalized_base_url = str(base_url or "").strip().lower()
    return "api.anthropic.com" in normalized_base_url


def anthropic_messages_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    return f"{normalized}/messages"


def openai_chat_completions_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _adapt_content_blocks_for_anthropic(content: object) -> object:
    """Convert OpenAI-format content blocks to Anthropic-compatible format."""
    if not isinstance(content, list):
        return content
    adapted: list[dict[str, object]] = []
    for block in content:
        if not isinstance(block, dict):
            adapted.append(block)
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "image_url":
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
                if url.startswith("data:"):
                    # data:image/png;base64,<data>
                    header, _, b64_data = url.partition("base64,")
                    media_type = header.removeprefix("data:").rstrip(";")
                    if not media_type.startswith("image/"):
                        media_type = "image/png"
                    adapted.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        },
                    })
                    continue
            adapted.append(block)
        else:
            adapted.append(block)
    return adapted


def build_anthropic_messages_payload(payload: dict[str, object]) -> dict[str, object]:
    messages = payload.get("messages")
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, object]] = []
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = item.get("content")
            if role == "system":
                text = extract_text_from_content_blocks(content)
                if text:
                    system_parts.append(text)
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            anthropic_messages.append({"role": role, "content": _adapt_content_blocks_for_anthropic(content) if content is not None else ""})

    request_payload: dict[str, object] = {
        "model": str(payload.get("model") or "").strip(),
        "messages": anthropic_messages or [{"role": "user", "content": ""}],
        "max_tokens": int(payload.get("max_tokens") or 1024),
    }
    if system_parts:
        request_payload["system"] = "\n\n".join(system_parts)
    if "temperature" in payload and payload.get("temperature") is not None:
        request_payload["temperature"] = payload["temperature"]
    if payload.get("stream") is True:
        request_payload["stream"] = True
    return request_payload


def extract_llm_message_content(body: object) -> str:
    if not isinstance(body, dict):
        return ""

    top_level_content = extract_text_from_content_blocks(body.get("content"))
    if top_level_content:
        return top_level_content

    for key in ("output_text", "response", "text"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    output = body.get("output")
    if isinstance(output, list):
        output_text = extract_text_from_content_blocks(output)
        if output_text:
            return output_text

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    choice_text = first.get("text")
    if isinstance(choice_text, str) and choice_text.strip():
        return choice_text.strip()

    message = first.get("message")
    if not isinstance(message, dict):
        return ""

    content = extract_text_from_content_blocks(message.get("content"))
    if content:
        return content

    return ""


def extract_llm_reasoning_content(body: object) -> str:
    if not isinstance(body, dict):
        return ""

    for key in ("reasoning_content", "reasoning", "thinking"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first = choices[0]
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = first.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        message = first.get("message")
        if isinstance(message, dict):
            for key in ("reasoning_content", "reasoning", "thinking"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def extract_llm_finish_reason(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    value = body.get("finish_reason")
    if isinstance(value, str):
        return value
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        return value if isinstance(value, str) else None
    return None


def extract_text_from_content_blocks(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                text_parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").strip().lower()
        if block_type in {"thinking", "reasoning", "redacted_thinking"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
            continue
        nested_content = item.get("content")
        nested_text = extract_text_from_content_blocks(nested_content)
        if nested_text:
            text_parts.append(nested_text)
    return "\n".join(text_parts).strip()
