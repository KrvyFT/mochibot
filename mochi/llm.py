"""LLM provider abstraction — provider-agnostic.

Supports the OpenAI-compatible protocol for OpenAI/DeepSeek and Anthropic.

Usage:
    from mochi.llm import get_client_for_tier
    client = get_client_for_tier()         # main tier (default)
    client = get_client_for_tier("lite")   # optional low-cost tier
    response = client.chat(messages, tools=...)
"""

import json
import base64
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypedDict
from urllib.parse import urlsplit, urlunsplit

import httpx

from mochi.qwen_image import generate_qwen_image, is_qwen_image_model

log = logging.getLogger(__name__)

# Explicit timeout for OpenAI-compatible HTTP clients. SDK default is 600s read,
# which silently masks slow gateways. Read=120s is well above worst-case
# reasoning-model latency on slow third-party gateways but fails fast on hangs.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
# Gemini-style image models often spend 30–90s thinking before the first byte.
# 60s cut off in-flight generations; extra_body retries then stacked more waits.
_IMAGE_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=10.0)

# Failures worth another attempt are those where the request never reached a
# verdict, or the gateway explicitly said "later". A request the provider
# rejected on its merits — bad schema, bad key, unknown model — fails
# identically every time, so retrying it only delays the report to the owner.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_RETRYABLE_ERROR_NAMES = frozenset({
    # openai / anthropic SDK transport failures, which carry no status code
    "APIConnectionError",
    "APITimeoutError",
    "APIConnectionTimeoutError",
    "InternalServerError",
    "RateLimitError",
    "OverloadedError",
    # httpx
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutException",
    "RemoteProtocolError",
    # stdlib, for providers that surface the socket error directly
    "TimeoutError",
    "ConnectionError",
})

# Redacted before any failure text reaches the owner: gateway errors routinely
# echo the request URL or Authorization header back in the message.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\b(?:api[-_]?key|access[-_]?token|token)\b\s*[=:]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
)


def is_retryable_error(exc: BaseException) -> bool:
    """Report whether a failed LLM call could succeed on another attempt.

    Classified by HTTP status when the provider returned one, since that is the
    provider's own verdict, and otherwise by exception type, since transport
    failures never carry a status.

    # Examples

    >>> is_retryable_error(httpx.ConnectError("connection refused"))
    True
    >>> is_retryable_error(ValueError("bad schema"))
    False
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS_CODES
    names = {klass.__name__ for klass in type(exc).__mro__}
    return bool(names & _RETRYABLE_ERROR_NAMES)


def describe_error(exc: BaseException, *, limit: int = 300) -> str:
    """Render a call failure as one compact line safe to show the owner.

    Credentials are stripped and the result is truncated, because provider
    error bodies can run to kilobytes of HTML.
    """
    text = f"{type(exc).__name__}: {exc}".strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or type(exc).__name__


def _decode_data_image(block: dict) -> tuple[str, bytes]:
    """Decode one canonical OpenAI image_url block for native providers."""
    image_url = block.get("image_url", {})
    url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:image/"):
        raise ValueError("Only base64 data URL images are supported")
    header, encoded = url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Image data URL must use base64 encoding")
    media_type = header[5:].split(";", 1)[0]
    return media_type, base64.b64decode(encoded, validate=True)


class ToolCallDict(TypedDict):
    """Typed structure for a single tool call in LLMResponse."""
    id: str
    name: str
    arguments: object
    argument_error: str | None


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCallDict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    tool_calls_complete: bool = False
    # None = SDK didn't report (legacy SDK / non-reasoning model / non-OpenAI
    # provider). 0 = model explicitly reported zero. The distinction matters
    # for cost telemetry — see plan P1-2.
    reasoning_tokens: int | None = None
    cached_prompt_tokens: int | None = None


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        """Send a chat completion request.

        json_mode=True asks the provider to return strict JSON. Each provider
        maps this to its native capability (response_format / response_mime_type).
        Anthropic has no native JSON mode — caller must rely on prompting plus
        the framework-layer markdown fence strip.
        """
        ...

    def generate_image(
        self,
        prompt: str,
        *,
        reference_images: list[tuple[str, bytes]] | None = None,
    ) -> bytes:
        """Generate one image via chat.completions or a native image API."""
        raise NotImplementedError(
            f"{self.provider_name()} does not support image generation"
        )

    @abstractmethod
    def provider_name(self) -> str:
        ...


# Anchored fence matcher. The ^...$ anchors are an INVARIANT: they prevent
# matching fences that appear inside JSON string values (e.g. {"x": "```json"}).
# Do not relax to a non-anchored search — see TestStripJsonFence + case 20.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

# Reasoning-model wrappers some models emit around (or instead of) JSON.
# Paired non-greedy match: a TRUNCATED tag (no closing) WILL NOT match,
# which is intentional — better to leave content alone than risk eating
# real JSON because the closing tag is missing.
_REASONING_XML_RE = re.compile(
    r"<(thinking|analysis|reasoning|scratchpad)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# Trailing comma before } or ] — a common LLM JSON defect.
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _try_extract(s: str) -> str | None:
    """Find the first complete JSON object/array in s using stdlib raw_decode.

    Returns the JSON substring on success, None if no parseable JSON found.
    O(n²) worst case — do not call on >100KB inputs (LLM JSON < 10KB in
    practice). One trailing-comma fixup retry per candidate position.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            _, end = decoder.raw_decode(s[i:])
            return s[i:i + end]
        except json.JSONDecodeError:
            chunk = s[i:]
            fixed = _TRAILING_COMMA_RE.sub(r"\1", chunk)
            if fixed != chunk:
                try:
                    _, end = decoder.raw_decode(fixed)
                    return fixed[:end]
                except json.JSONDecodeError:
                    pass
            continue
    return None


def extract_json(content: str) -> str:
    """Extract the first complete JSON object/array from a string.

    Handles four real-world failure modes from reasoning-era LLMs:
      1. Markdown fence wrap: ```json\\n{...}\\n```
      2. Reasoning XML wrap: <thinking>...</thinking>{...}
      3. Prose before/after: "Sure, here you go: {...}"
      4. Trailing commas: {"a": 1,}

    Strategy — fence strip → FAST PATH (raw_decode on stripped content) →
    SLOW PATH (strip reasoning XML, retry). The fast path runs FIRST so
    that legitimate JSON containing XML-shaped string values (e.g.
    {"comment": "<analysis>..."}) is never corrupted.

    NEVER raises. On total failure returns the (best-effort stripped)
    content so the caller's json.loads gives a clear error including
    the raw input.
    """
    if not content:
        return ""
    s = content.strip()

    fence_match = _FENCE_RE.match(s)
    if fence_match:
        s = fence_match.group(1).strip()

    result = _try_extract(s)
    if result is not None:
        return result

    stripped = _REASONING_XML_RE.sub("", s).strip()
    if stripped != s:
        result = _try_extract(stripped)
        if result is not None:
            return result
        s = stripped

    return s


def _parse_openai_tool_calls(choice) -> list[ToolCallDict]:
    """Extract tool calls from an OpenAI-style chat completion choice."""
    tool_calls: list[ToolCallDict] = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            try:
                parsed_args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                log.warning("Malformed tool_call arguments for %s",
                            tc.function.name)
                parsed_args = None
                argument_error = "arguments were not valid JSON"
            else:
                argument_error = None
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": parsed_args,
                "argument_error": argument_error,
            })
    return tool_calls


def _openai_response(choice, usage, model: str, tool_calls: list[ToolCallDict]) -> LLMResponse:
    """Build LLMResponse from OpenAI-style completion."""
    reasoning: int | None = None
    cached: int | None = None
    if usage:
        comp_details = getattr(usage, "completion_tokens_details", None)
        if comp_details is not None:
            r = getattr(comp_details, "reasoning_tokens", None)
            reasoning = int(r) if r is not None else None
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            c = getattr(prompt_details, "cached_tokens", None)
            cached = int(c) if c is not None else None
        if cached is None:
            c = getattr(usage, "prompt_cache_hit_tokens", None)
            cached = int(c) if c is not None else None
    return LLMResponse(
        content=choice.message.content or "",
        reasoning_content=getattr(choice.message, "reasoning_content", "") or "",
        tool_calls=tool_calls,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        model=model,
        finish_reason=choice.finish_reason or "",
        tool_calls_complete=choice.finish_reason == "tool_calls",
        reasoning_tokens=reasoning,
        cached_prompt_tokens=cached,
    )


class _OpenAICompatChat:
    """Mixin: negotiate max_tokens vs max_completion_tokens.

    On first call, tries the modern token parameter. Precise protocol 400s can
    negotiate the alternate token parameter or empty reasoning placeholders.

    Learned capabilities are also persisted in a class-level cache keyed by
    endpoint and model, so a fresh equivalent provider instance can skip the
    probe-and-retry round-trip.
    """

    # Class-level cache: endpoint + model → negotiated OpenAI-compatible quirks.
    # Survives provider instance recreation (hot-swap, pool reload).
    # Best-effort only: losing a concurrent cache update causes another safe probe.
    _model_caps: dict[str, dict[str, bool]] = {}

    # Per-instance capability flags (set after first successful call)
    # None = unknown, True = supported, False = not supported
    _use_max_completion_tokens: bool | None = None
    _requires_reasoning_placeholders: bool = False

    def _init_caps_from_cache(self, model: str, base_url: str = "") -> None:
        """Seed instance flags from class-level cache if available."""
        if base_url:
            parsed = urlsplit(base_url.rstrip("/"))
            endpoint = urlunsplit((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                parsed.fragment,
            ))
        else:
            endpoint = "openai-default"
        self._caps_cache_key = f"{endpoint}::{model}"
        cached = self._model_caps.get(self._caps_cache_key)
        if cached:
            self._use_max_completion_tokens = cached.get("use_max_completion_tokens")
            self._requires_reasoning_placeholders = cached.get(
                "requires_reasoning_placeholders", False,
            )
            log.debug(
                "Model %s: restored max_completion_tokens=%s, "
                "reasoning_placeholders=%s from cache",
                model, self._use_max_completion_tokens,
                self._requires_reasoning_placeholders,
            )

    def _save_caps_to_cache(self, model: str) -> None:
        """Persist resolved capability flags to the class-level cache."""
        cache_key = getattr(self, "_caps_cache_key", model)
        cached = dict(self._model_caps.get(cache_key, {}))
        if self._use_max_completion_tokens is not None:
            cached["use_max_completion_tokens"] = self._use_max_completion_tokens
        if self._requires_reasoning_placeholders:
            cached["requires_reasoning_placeholders"] = True
        if cached:
            self._model_caps[cache_key] = cached

    @staticmethod
    def _with_reasoning_placeholders(messages: list[dict]) -> list[dict]:
        """Copy assistant history with explicit empty reasoning placeholders."""
        return [
            (
                {**message, "reasoning_content": ""}
                if message.get("role") == "assistant"
                and "reasoning_content" not in message
                else message
            )
            for message in messages
        ]

    def _do_chat(self, client, model: str, messages: list[dict],
                 tools: list[dict] | None, temperature: float | None,
                 max_tokens: int, json_mode: bool = False) -> Any:
        """Call chat.completions.create with auto-negotiation."""
        from openai import BadRequestError

        request_messages = (
            self._with_reasoning_placeholders(messages)
            if self._requires_reasoning_placeholders
            else messages
        )
        kwargs: dict = {"model": model, "messages": request_messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # --- max tokens parameter ---
        if self._use_max_completion_tokens is None:
            # Unknown — try new param first
            kwargs["max_completion_tokens"] = max_tokens
        elif self._use_max_completion_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        if temperature is not None:
            kwargs["temperature"] = temperature

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        for attempt in range(3):
            try:
                resp = client.chat.completions.create(**kwargs)
                if self._use_max_completion_tokens is None:
                    self._use_max_completion_tokens = True
                    log.debug("Model %s: using max_completion_tokens", model)
                self._save_caps_to_cache(model)
                return resp
            except BadRequestError as exc:
                err_msg = str(exc).lower()
                changed = False

                if "max_tokens" in err_msg and "max_completion_tokens" in err_msg:
                    if "max_completion_tokens" in kwargs:
                        self._use_max_completion_tokens = False
                        kwargs.pop("max_completion_tokens", None)
                        kwargs["max_tokens"] = max_tokens
                        log.info("Model %s: falling back to max_tokens", model)
                        changed = True
                    elif "max_tokens" in kwargs:
                        self._use_max_completion_tokens = True
                        kwargs.pop("max_tokens", None)
                        kwargs["max_completion_tokens"] = max_tokens
                        log.info(
                            "Model %s: falling back to max_completion_tokens",
                            model,
                        )
                        changed = True

                if (
                    "reasoning_content" in err_msg
                    and "must be passed back" in err_msg
                    and not self._requires_reasoning_placeholders
                ):
                    normalized = self._with_reasoning_placeholders(messages)
                    if normalized != messages:
                        self._requires_reasoning_placeholders = True
                        kwargs["messages"] = normalized
                        log.info(
                            "Model %s: adding empty reasoning placeholders to "
                            "assistant history",
                            model,
                        )
                        changed = True

                if not changed or attempt == 2:
                    raise

        raise RuntimeError("OpenAI-compatible capability negotiation exhausted")


class OpenAIProvider(_OpenAICompatChat, LLMProvider):
    """OpenAI-compatible API provider for OpenAI and DeepSeek."""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        from openai import OpenAI
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._use_max_completion_tokens = None
        self._requires_reasoning_placeholders = False
        self._init_caps_from_cache(model, base_url)
        kwargs: dict = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": _HTTP_TIMEOUT,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def provider_name(self) -> str:
        return "openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        resp = self._do_chat(self._client, self._model, messages, tools,
                             temperature, max_tokens, json_mode=json_mode)
        choice = resp.choices[0]
        response = _openai_response(choice, resp.usage, self._model,
                                    _parse_openai_tool_calls(choice))
        if json_mode and response.content:
            response.content = extract_json(response.content)
        return response

    def generate_image(
        self,
        prompt: str,
        *,
        reference_images: list[tuple[str, bytes]] | None = None,
        prompt_extend: bool = True,
        enable_thinking: bool = True,
        size: str | None = None,
        timeout_s: float | None = None,
    ) -> bytes:
        if is_qwen_image_model(self._model):
            return generate_qwen_image(
                prompt,
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
                reference_images=reference_images or [],
                prompt_extend=prompt_extend,
                enable_thinking=enable_thinking,
                size=size,
                timeout_s=timeout_s,
            )
        image_timeout = _IMAGE_HTTP_TIMEOUT
        if timeout_s is not None and timeout_s > 0:
            image_timeout = float(timeout_s)
        client = self._client.with_options(timeout=image_timeout)
        return generate_image_via_chat(
            client, self._model, prompt, reference_images=reference_images or [],
        )


_DATA_URI_RE = re.compile(
    r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)


def _bytes_from_image_url(url: str, *, download=None) -> bytes | None:
    if not url or not isinstance(url, str):
        return None
    match = _DATA_URI_RE.search(url)
    if match:
        return base64.b64decode(re.sub(r"\s+", "", match.group(1)))
    if url.startswith("http://") or url.startswith("https://"):
        getter = download or _download_image_url
        return getter(url)
    return None


def _extract_part_url(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not part:
        return ""
    if isinstance(part, dict):
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            return str(image_url.get("url") or "")
        if isinstance(image_url, str):
            return image_url
        inline = part.get("inline_data") or part.get("inlineData") or {}
        if isinstance(inline, dict) and inline.get("data"):
            mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
            return f"data:{mime};base64,{inline['data']}"
        if part.get("data") and (
            str(part.get("type") or "").startswith("image")
            or part.get("mime_type")
            or part.get("mimeType")
        ):
            mime = part.get("mime_type") or part.get("mimeType") or "image/png"
            return f"data:{mime};base64,{part['data']}"
        return str(part.get("url") or "")
    image_url = getattr(part, "image_url", None)
    if isinstance(image_url, str):
        return image_url
    if image_url is not None:
        return str(getattr(image_url, "url", "") or "")
    return str(getattr(part, "url", "") or "")


def _message_as_mapping(message: Any) -> dict:
    if isinstance(message, dict):
        return message
    if hasattr(message, "model_dump"):
        try:
            dumped = message.model_dump()
            if isinstance(dumped, dict):
                extra = getattr(message, "model_extra", None) or {}
                if extra:
                    dumped = {**dumped, **extra}
                return dumped
        except Exception:
            pass
    extra = getattr(message, "model_extra", None)
    mapping: dict[str, Any] = {}
    if isinstance(extra, dict):
        mapping.update(extra)
    for key in ("images", "image", "content"):
        if hasattr(message, key):
            mapping[key] = getattr(message, key)
    return mapping


def _walk_for_image_bytes(obj: Any, *, download=None, depth: int = 0) -> bytes | None:
    if obj is None or depth > 8:
        return None
    if isinstance(obj, str):
        return _bytes_from_image_url(obj, download=download)
    if isinstance(obj, dict):
        data = _bytes_from_image_url(_extract_part_url(obj), download=download)
        if data:
            return data
        for value in obj.values():
            data = _walk_for_image_bytes(value, download=download, depth=depth + 1)
            if data:
                return data
        return None
    if isinstance(obj, (list, tuple)):
        for item in obj:
            data = _walk_for_image_bytes(item, download=download, depth=depth + 1)
            if data:
                return data
        return None
    if hasattr(obj, "model_dump"):
        try:
            return _walk_for_image_bytes(
                obj.model_dump(), download=download, depth=depth + 1,
            )
        except Exception:
            return None
    return _bytes_from_image_url(_extract_part_url(obj), download=download)


def image_bytes_from_chat_message(message: Any, *, download=None) -> bytes:
    """Pull image bytes from a chat.completions image-generation message."""
    mapping = _message_as_mapping(message)
    data = _walk_for_image_bytes(mapping, download=download)
    if data:
        return data
    raise RuntimeError("Chat image response contained no image")


def generate_image_via_chat(
    client: Any,
    model: str,
    prompt: str,
    *,
    reference_images: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Generate an image through chat.completions."""
    content: str | list[dict]
    if reference_images:
        blocks: list[dict] = [{"type": "text", "text": prompt}]
        for mime, data in reference_images:
            encoded = base64.b64encode(data).decode("ascii")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })
        content = blocks
    else:
        content = prompt
    messages = [{"role": "user", "content": content}]
    extra_bodies = (
        {"modalities": ["text", "image"]},
        {"modalities": ["image", "text"]},
        {"generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
        {"generation_config": {"responseModalities": ["TEXT", "IMAGE"]}},
        {},
    )
    last_exc: BaseException | None = None
    log.info(
        "Chat image create model=%s refs=%s",
        model, len(reference_images or []),
    )
    for extra_body in extra_bodies:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            log.info("Chat image create failed (%s): %s", extra_body, describe_error(exc))
            if not _chat_image_extra_rejected(exc):
                raise
            continue
        try:
            message = resp.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            last_exc = exc
            continue
        try:
            return image_bytes_from_chat_message(message)
        except RuntimeError:
            last_exc = RuntimeError("Chat image response contained no image")
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Chat image generation produced no image")


def _chat_image_extra_rejected(exc: BaseException) -> bool:
    """Whether the gateway rejected this extra_body, so another shape is worth trying.

    Timeouts and 5xx mean the request was accepted and still running or the
    upstream failed; cycling modalities would only stack more waits.
    """
    status = getattr(exc, "status_code", None)
    if status in (400, 422):
        return True
    names = {klass.__name__ for klass in type(exc).__mro__}
    return bool(names & {"BadRequestError", "UnprocessableEntityError"})


def _download_image_url(url: str) -> bytes:
    with httpx.Client(timeout=_IMAGE_HTTP_TIMEOUT, follow_redirects=True) as http:
        response = http.get(url)
        response.raise_for_status()
        return response.content


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, model: str):
        import anthropic
        self._model = model
        self._client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=_HTTP_TIMEOUT,
        )

    def provider_name(self) -> str:
        return "anthropic"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        # Anthropic has no native JSON mode. Caller must rely on prompting.
        # Framework-layer strip below is the safety net (gated on json_mode).
        # Separate system message from conversation
        system_msg = ""
        conversation = []
        for m in messages:
            if m["role"] == "system":
                system_msg += m["content"] + "\n"
            else:
                conversation.append(m)

        # Convert OpenAI-format tool messages to Anthropic format
        conversation = self._convert_messages(conversation)

        kwargs = dict(
            model=self._model,
            messages=conversation,
            max_tokens=max_tokens,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if system_msg:
            # System as a list-of-blocks with cache_control: ephemeral.
            # Mochi's system prompt (Core + Agent + runtime) is 4-8KB and
            # 100% stable across a conversation — perfect cache target.
            # Cached reads bill at 10% of input rate.
            kwargs["system"] = [{
                "type": "text",
                "text": system_msg.strip(),
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            # Convert OpenAI tool format to Anthropic format
            kwargs["tools"] = self._convert_tools(tools)

        resp = self._client.messages.create(**kwargs)

        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                arguments = block.input
                argument_error = None
                if not isinstance(arguments, dict):
                    arguments = None
                    argument_error = "arguments were not an object"
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": arguments,
                    "argument_error": argument_error,
                })
            elif block.type in ("thinking", "redacted_thinking"):
                # Internal reasoning — NEVER leak into user-facing content.
                continue

        if json_mode and content:
            content = extract_json(content)

        usage = resp.usage
        cached: int | None = None
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            cache_create = getattr(usage, "cache_creation_input_tokens", None)
            if cache_read is not None or cache_create is not None:
                # Only "read" counts as savings. cache_creation is the FIRST
                # write (full price + 25% surcharge) — don't conflate.
                cached = int(cache_read or 0)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=usage.input_tokens if usage else 0,
            completion_tokens=usage.output_tokens if usage else 0,
            total_tokens=(usage.input_tokens + usage.output_tokens) if usage else 0,
            model=self._model,
            finish_reason=resp.stop_reason or "",
            tool_calls_complete=resp.stop_reason == "tool_use",
            # Anthropic doesn't separately report thinking-token usage; it's
            # bundled into output_tokens. Leave None to preserve the P1-2
            # semantic (None = not reported by SDK).
            reasoning_tokens=None,
            cached_prompt_tokens=cached,
        )

    @staticmethod
    def _convert_tools(openai_tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Anthropic tool format."""
        anthropic_tools = []
        for t in openai_tools:
            func = t.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return anthropic_tools

    @staticmethod
    def _convert_messages(messages: list[dict]) -> list[dict]:
        """Convert OpenAI-format tool messages to Anthropic format.

        OpenAI uses:
          - assistant msg with "tool_calls" list
          - separate "tool" role messages with tool_call_id
        Anthropic uses:
          - assistant msg with content blocks: [{"type":"tool_use","id":...,"name":...,"input":...}]
          - user msg with content blocks: [{"type":"tool_result","tool_use_id":...,"content":"..."}]
        """
        converted = []
        i = 0
        while i < len(messages):
            m = messages[i]

            if m["role"] == "assistant" and "tool_calls" in m:
                # Convert assistant tool_calls to content blocks
                content_blocks = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError as exc:
                            raise ValueError(
                                "Cannot convert malformed tool arguments "
                                "to Anthropic format"
                            ) from exc
                    if not isinstance(args, dict):
                        raise ValueError(
                            "Anthropic tool arguments must be an object"
                        )
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": func.get("name", ""),
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": content_blocks})
                i += 1

            elif m["role"] == "tool":
                # Collect consecutive tool results into one user message
                result_blocks = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": messages[i].get("tool_call_id", ""),
                        "content": messages[i].get("content", ""),
                    })
                    i += 1
                converted.append({"role": "user", "content": result_blocks})

            else:
                if m["role"] == "user" and isinstance(m.get("content"), list):
                    blocks = []
                    for block in m["content"]:
                        if block.get("type") == "text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "image_url":
                            media_type, data = _decode_data_image(block)
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(data).decode("ascii"),
                                },
                            })
                    converted.append({**m, "content": blocks})
                else:
                    converted.append(m)
                i += 1

        return converted


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def _make_client(provider: str, api_key: str, model: str, base_url: str) -> LLMProvider:
    """Instantiate a fresh LLM provider."""
    model = model.strip()
    if not model:
        raise ValueError(
            "Model name is required. Configure it in the admin portal."
        )
    if provider == "openai":
        return OpenAIProvider(api_key=api_key, model=model, base_url=base_url)
    elif provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Supported: openai (including compatible APIs) and anthropic"
        )


def get_client_for_tier(tier: str = "main") -> LLMProvider:
    """Get an LLM client via the model pool tier routing.

    Always delegates to ModelPool.get_tier(), which resolves DB tier
    assignments.
    """
    from mochi.model_pool import get_pool
    return get_pool().get_tier(tier)
