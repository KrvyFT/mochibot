"""Multi-model pool — tier-based routing for LLM tasks.

Tiers:
    lite  — cheap/fast model for classification, tagging, simple tasks
    main  — Mochi's conversation and background reasoning model
    draw  — optional image-generation model (chat.completions or Qwen-Image native)

All tier config comes from DB. .env model vars are seed data only —
auto-imported on first startup via seed_models_from_env().
"""

import struct
import logging
import time
import threading
from collections import OrderedDict
from urllib.parse import urlsplit

from mochi.config import (
    EMBEDDING_PROVIDER, EMBEDDING_API_KEY, EMBEDDING_MODEL, EMBEDDING_BASE_URL,
    EMBEDDING_CACHE_MAX_SIZE, EMBEDDING_CACHE_TTL_S,
)
from mochi.llm import LLMProvider, _make_client

log = logging.getLogger(__name__)

VALID_TIERS = frozenset({"lite", "main", "draw"})
_OPTIONAL_TIERS = frozenset({"draw"})
_OFFICIAL_EMBEDDING_BASE_URLS = frozenset({
    "",
    "https://api.openai.com/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
})


def _is_supported_embedding_base_url(base_url: str) -> bool:
    """Whether the endpoint is an official provider the API key may be sent to.

    The allowlist exists because embedding requests carry the user's API key
    and raw memory text; only vendor-controlled domains are accepted.
    """
    normalized = (base_url or "").strip().rstrip("/")
    if normalized in _OFFICIAL_EMBEDDING_BASE_URLS:
        return True
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")

    if path == "/openai/v1" and (
        host.endswith(".services.ai.azure.com")
        or host.endswith(".openai.azure.com")
    ):
        return True
    # Alibaba Cloud Bailian dedicated (MaaS) deployments get a per-workspace
    # host but keep the shared DashScope-compatible path.
    if path == "/compatible-mode/v1" and host.endswith(".maas.aliyuncs.com"):
        return True
    return False


# ---------------------------------------------------------------------------
# TTL LRU cache (thread-safe, per-entry expiry)
# ---------------------------------------------------------------------------

class _TTLCache:
    """Thread-safe LRU cache with per-entry TTL expiry."""

    def __init__(self, max_size: int = 128, ttl_s: int = 300):
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> object | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.monotonic() - ts > self._ttl_s:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return val

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)


# ---------------------------------------------------------------------------
# Embedding provider resolution + factory
# ---------------------------------------------------------------------------

def _resolve_embedding_config() -> tuple[str, str, str, str]:
    """Resolve (provider, api_key, model, base_url) for embedding.

    Only ``none`` and the OpenAI-compatible ``openai`` adapter are supported.
    """
    provider = (EMBEDDING_PROVIDER or "").strip().lower()

    if not provider or provider == "none":
        return ("none", "", "", "")

    if provider == "openai":
        if not _is_supported_embedding_base_url(EMBEDDING_BASE_URL):
            log.warning(
                "Unsupported embedding endpoint '%s', disabling embedding",
                EMBEDDING_BASE_URL,
            )
            return ("none", "", "", "")
        return (
            "openai",
            EMBEDDING_API_KEY,
            EMBEDDING_MODEL,
            EMBEDDING_BASE_URL,
        )

    log.warning("Unknown EMBEDDING_PROVIDER '%s', disabling embedding", provider)
    return ("none", "", "", "")


def _coerce_batch_index(value: object) -> int | None:
    """Normalize a provider batch index, or None when it is unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# Whether a gateway omits or constant-stamps ``index`` is a property of the
# deployment, not of a request, and the embedding client is built once per
# process. Keyed by quirk kind rather than by the raw indexes so that batches
# of different sizes from the same gateway do not each announce themselves.
_reported_index_quirks: set[str] = set()


def _classify_index_quirk(indexes: list[int | None]) -> str:
    """Name the reason ``indexes`` cannot be read as a permutation.

    ``absent`` and ``constant`` are systematic gateway traits for which
    request order is the documented behaviour. ``inconsistent`` means the
    gateway does populate indexes but contradicts itself, which is the only
    case where falling back to response order is a guess rather than the
    contract.
    """
    if all(index is None for index in indexes):
        return "absent"
    if len(set(indexes)) == 1:
        return "constant"
    return "inconsistent"


def _report_index_fallback(indexes: list[int | None]) -> None:
    """Announce the response-order fallback without repeating a known trait."""
    kind = _classify_index_quirk(indexes)
    if kind == "inconsistent":
        # Self-contradicting indexes mean the gateway tracks order and got it
        # wrong, so the response order may be wrong too. Keep reporting these.
        log.warning(
            "Embedding batch indexes disagree (%r); using response order",
            indexes,
        )
        return
    if kind in _reported_index_quirks:
        log.debug(
            "Embedding batch indexes %s (%r); using response order",
            kind, indexes,
        )
        return
    _reported_index_quirks.add(kind)
    log.info(
        "Embedding gateway returns %s batch indexes; using request order "
        "for this and subsequent batches",
        kind,
    )


def _resolve_batch_order(data: list, expected: int) -> list[int]:
    """Map each response item to the input position it belongs to.

    Some OpenAI-compatible gateways return ``index`` as a string, or stamp
    every item with the same value. Those responses are still usable because
    the API contract yields results in request order, so fall back to that
    order rather than dropping the whole batch. A size mismatch is not
    recoverable — pairing embeddings with the wrong text would silently
    corrupt recall.
    """
    if len(data) != expected:
        raise ValueError(
            f"Embedding batch size mismatch: sent {expected}, "
            f"got {len(data)}"
        )
    indexes = [_coerce_batch_index(getattr(item, "index", None)) for item in data]
    if len(set(indexes)) == expected and all(
        index is not None and 0 <= index < expected for index in indexes
    ):
        return indexes  # type: ignore[return-value]
    _report_index_fallback(indexes)
    return list(range(expected))


def _make_embed_client(provider: str, api_key: str, model: str,
                       base_url: str) -> tuple:
    """Instantiate the OpenAI-compatible embedding client, or (None, "")."""
    if provider == "none" or not provider:
        return None, ""
    if provider != "openai" or not _is_supported_embedding_base_url(base_url):
        return None, ""
    if not api_key or not model:
        return None, ""

    from openai import OpenAI
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs), model


# ---------------------------------------------------------------------------
# ModelPool
# ---------------------------------------------------------------------------

class ModelPool:
    """Manages main/lite LLM clients plus embedding."""

    def __init__(self):
        self._tiers: dict[str, LLMProvider] = {}
        self._tier_models: dict[str, str] = {}
        self._lock = threading.Lock()

        # Load all tiers from DB (the single authority)
        self._load_from_db()

        log.info("Tier pool: %s", {t: m for t, m in self._tier_models.items()})

        # Optional OpenAI-compatible embedding client
        self._embed_client = None
        self._embed_model = ""
        self._embed_cache = _TTLCache(EMBEDDING_CACHE_MAX_SIZE, EMBEDDING_CACHE_TTL_S)

        self._embed_dim: int | None = None  # probed from actual model on init

        try:
            e_prov, e_key, e_model, e_base = _resolve_embedding_config()
            self._embed_client, self._embed_model = _make_embed_client(
                e_prov, e_key, e_model, e_base,
            )
            if self._embed_client:
                log.info("Embedding configured: provider=%s model=%s", e_prov, e_model)
                self._probe_embed_dim()
            else:
                log.info("Embedding disabled (provider=%s)", e_prov or "none")
        except Exception as e:
            log.warning("Embedding client init failed: %s", e)

    def get_tier(self, tier: str = "main") -> LLMProvider:
        """Get an explicitly assigned tier client."""
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}")

        client = self._get_loaded_tier(tier)
        if client is None:
            # Tier missing — maybe models were configured after pool init.
            # Retry DB load once before giving up.
            self._load_from_db(target_tier=tier)
            client = self._get_loaded_tier(tier)
        if client is None:
            raise ValueError(
                f"No model assigned to tier '{tier}'. "
                "Assign it in the admin portal."
            )
        return client

    def get_tier_model(self, tier: str) -> str:
        """Get model name for a tier (for logging/admin display)."""
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}")
        with self._lock:
            return self._tier_models.get(tier, "unknown")

    def _get_loaded_tier(self, tier: str) -> LLMProvider | None:
        with self._lock:
            return self._tiers.get(tier)

    def reload_tier(self, tier: str, provider: str, api_key: str,
                    model: str, base_url: str) -> None:
        """Hot-swap a tier's LLM client at runtime.

        Called by admin portal after model registry/tier assignment changes.
        Thread-safe via lock.
        """
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}")
        client = _make_client(provider, api_key, model, base_url)
        with self._lock:
            self._tiers[tier] = client
            self._tier_models[tier] = model
        log.info("Hot-reloaded tier '%s': provider=%s model=%s", tier, provider, model)

    def clear_tier(self, tier: str) -> None:
        """Remove a hot-loaded assignment."""
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}")
        with self._lock:
            self._tiers.pop(tier, None)
            self._tier_models.pop(tier, None)

    def _load_from_db(self, *, target_tier: str | None = None) -> None:
        """Load one or all tier configs exclusively from DB."""
        try:
            from mochi.admin.admin_db import get_tier_effective_config
            effective = get_tier_effective_config()
            for tier, cfg in effective.items():
                if target_tier is not None and tier != target_tier:
                    continue
                if not cfg.get("model") or not cfg.get("assigned_name"):
                    self.clear_tier(tier)
                    if tier not in self._tiers and tier not in _OPTIONAL_TIERS:
                        log.warning("Tier '%s' has no model assigned", tier)
                    continue
                try:
                    self.reload_tier(
                        tier, cfg["provider"], cfg.get("api_key", ""),
                        cfg["model"], cfg.get("base_url", ""),
                    )
                except Exception as e:
                    log.error("Failed to load tier '%s' from DB: %s", tier, e)
        except Exception as e:
            log.error("Failed to load tier config from DB: %s", e)

    # -------------------------------------------------------------------
    # Embedding
    # -------------------------------------------------------------------

    def _probe_embed_dim(self) -> None:
        """Probe the embedding model with a short test string to detect dimension."""
        try:
            resp = self._embed_client.embeddings.create(
                model=self._embed_model, input="dimension probe",
            )
            vec = resp.data[0].embedding
            self._embed_dim = len(vec)
            log.info("Probed embedding dimension: %d", self._embed_dim)
        except Exception as e:
            log.warning("Embedding dimension probe failed: %s", e)

    def get_embed_dim(self) -> int | None:
        """Return probed embedding dimension, or None if not available."""
        return self._embed_dim

    def embed(self, text: str) -> bytes | None:
        """Generate embedding vector, return as packed float32 bytes. Cached."""
        if not self._embed_client or not text or not text.strip():
            return None
        key = text[:8000]
        cached = self._embed_cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = self._embed_client.embeddings.create(
                model=self._embed_model, input=key,
            )
            vec = resp.data[0].embedding
            packed = struct.pack(f"{len(vec)}f", *vec)
            self._embed_cache.put(key, packed)
            return packed
        except Exception as e:
            log.warning("Embedding failed: %s", e)
            return None

    def embed_batch(self, texts: list[str]) -> list[bytes | None]:
        """Batch-embed cache misses in one provider request."""
        if not self._embed_client or not texts:
            return [None] * len(texts)

        results: list[bytes | None] = [None] * len(texts)
        missing_keys: list[str] = []
        positions_by_key: dict[str, list[int]] = {}
        for index, text in enumerate(texts):
            if not text or not text.strip():
                continue
            key = text[:8000]
            cached = self._embed_cache.get(key)
            if isinstance(cached, bytes):
                results[index] = cached
                continue
            if key not in positions_by_key:
                missing_keys.append(key)
                positions_by_key[key] = []
            positions_by_key[key].append(index)

        if not missing_keys:
            return results

        try:
            resp = self._embed_client.embeddings.create(
                model=self._embed_model, input=missing_keys,
            )
            order = _resolve_batch_order(resp.data, len(missing_keys))
            packed_by_key: dict[str, bytes] = {}
            for position, item in zip(order, resp.data, strict=True):
                key = missing_keys[position]
                if key in packed_by_key:
                    raise ValueError(
                        "Embedding batch mapped two results to one input"
                    )
                packed_by_key[key] = struct.pack(
                    f"{len(item.embedding)}f", *item.embedding,
                )
            for key, packed in packed_by_key.items():
                self._embed_cache.put(key, packed)
                for index in positions_by_key[key]:
                    results[index] = packed
            return results
        except Exception as e:
            log.warning("Batch embedding failed: %s", e)
            return results


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_pool: ModelPool | None = None


def get_pool() -> ModelPool:
    """Get (or create) the global ModelPool singleton."""
    global _pool
    if _pool is None:
        _pool = ModelPool()
    return _pool
