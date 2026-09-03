"""Qwen-Image 3.x via DashScope native multimodal-generation HTTP.

Qwen-Image is not on OpenAI compatible-mode chat/completions. Dedicated
MAAS workspaces expose:

    POST https://{WorkspaceId}.{region}.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
_MAX_REFERENCE_IMAGES = 3
_REF_MAX_EDGE = 1024
_REF_SHRINK_MIN_BYTES = 80_000
# Same Key cannot usefully run two 3.0-pro jobs at once (Admin test vs send_photo).
_GENERATE_LOCK = threading.Lock()
# Thinking + prompt rewrite on 3.0-pro can sit well past Gemini's 180s.
# I2I JSON with two photos also needs a longer write window than a chat POST.
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=180.0, pool=10.0)


def is_qwen_image_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("qwen-image")


def canonical_qwen_image_model(model: str) -> str:
    return (model or "").strip().lower()


def generation_url_from_base(base_url: str) -> str:
    """Turn a workspace HTTP root into the Qwen-Image generation URL."""
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("Base URL is required")
    if "://" not in raw:
        host = raw.split("/")[0].strip()
        if not host:
            raise ValueError("Base URL is invalid")
        return f"https://{host}{_GENERATION_PATH}"
    parsed = urlparse(raw)
    host = parsed.netloc
    if not host:
        raise ValueError("Base URL is invalid")
    path = parsed.path.rstrip("/")
    if path.endswith(_GENERATION_PATH):
        return f"{parsed.scheme}://{host}{path}"
    scheme = "http" if parsed.scheme == "http" else "https"
    return f"{scheme}://{host}{_GENERATION_PATH}"


def _image_url_from_response(body: dict) -> str:
    output = body.get("output") or {}
    choices = output.get("choices") or []
    if not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    content = message.get("content") or []
    if isinstance(content, dict):
        content = [content]
    for part in content:
        if isinstance(part, dict) and part.get("image"):
            return str(part["image"])
    return ""


def _shrink_reference(mime: str, data: bytes) -> tuple[str, bytes]:
    """Downscale large refs so I2I JSON does not stall the write timeout."""
    if not data or len(data) < _REF_SHRINK_MIN_BYTES or not shutil.which("ffmpeg"):
        return mime, data
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", "pipe:0",
                "-vf", f"scale='min({_REF_MAX_EDGE},iw)':-2",
                "-q:v", "5",
                "-f", "image2pipe", "-vcodec", "mjpeg",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return mime, data
    if proc.returncode != 0 or not proc.stdout:
        return mime, data
    log.info(
        "Qwen-Image shrunk ref %s %sB -> jpeg %sB",
        mime, len(data), len(proc.stdout),
    )
    return "image/jpeg", proc.stdout


def generate_qwen_image(
    prompt: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    reference_images: list[tuple[str, bytes]] | None = None,
    prompt_extend: bool = True,
    enable_thinking: bool = True,
    size: str | None = None,
    download=None,
) -> bytes:
    """Synchronous T2I/I2I. I2I accepts at most three reference images."""
    queued = time.monotonic()
    with _GENERATE_LOCK:
        waited = time.monotonic() - queued
        if waited >= 0.5:
            log.info("Qwen-Image waited %.1fs for in-process lock", waited)
        return _generate_qwen_image_locked(
            prompt,
            api_key=api_key,
            base_url=base_url,
            model=model,
            reference_images=reference_images,
            prompt_extend=prompt_extend,
            enable_thinking=enable_thinking,
            size=size,
            download=download,
        )


def _generate_qwen_image_locked(
    prompt: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    reference_images: list[tuple[str, bytes]] | None,
    prompt_extend: bool,
    enable_thinking: bool,
    size: str | None,
    download,
) -> bytes:
    url = generation_url_from_base(base_url)
    synth_model = canonical_qwen_image_model(model)
    if not synth_model:
        raise ValueError("Model is required")
    refs = [
        _shrink_reference(mime, data)
        for mime, data in (reference_images or [])[:_MAX_REFERENCE_IMAGES]
    ]
    content: list[dict] = []
    for mime, data in refs:
        encoded = base64.b64encode(data).decode("ascii")
        content.append({"image": f"data:{mime};base64,{encoded}"})
    content.append({"text": prompt})
    parameters = {
        "n": 1,
        "watermark": False,
        "prompt_extend": prompt_extend,
        "enable_thinking": enable_thinking,
    }
    if size:
        parameters["size"] = size
    payload = {
        "model": synth_model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    log.info(
        "Qwen-Image create model=%s refs=%s ref_bytes=%s thinking=%s url=%s",
        synth_model,
        len(refs),
        [len(data) for _, data in refs],
        enable_thinking,
        url,
    )
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as http:
            response = http.post(url, json=payload, headers=headers)
            try:
                body = response.json()
            except ValueError as exc:
                snippet = (response.text or "")[:300]
                raise RuntimeError(
                    f"Qwen-Image 返回非 JSON：HTTP {response.status_code} {snippet}"
                ) from exc
        if response.status_code >= 400 or body.get("code"):
            detail = (
                body.get("message") or body.get("code") or f"HTTP {response.status_code}"
            )
            raise RuntimeError(f"Qwen-Image 失败：{detail}")
        image_url = _image_url_from_response(body)
        if not image_url:
            raise RuntimeError("Qwen-Image 没有返回图片")
        getter = download
        if getter is None:
            from mochi.llm import _download_image_url
            getter = _download_image_url
        data = getter(image_url)
        if not data:
            raise RuntimeError("Qwen-Image 图片下载为空")
    except Exception:
        log.warning(
            "Qwen-Image failed elapsed=%.1fs refs=%s",
            time.monotonic() - started, len(refs),
        )
        raise
    log.info(
        "Qwen-Image done elapsed=%.1fs bytes=%s",
        time.monotonic() - started, len(data),
    )
    return data
