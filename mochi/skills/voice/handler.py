"""Voice skill — synthesize cloned speech and mark a Telegram voice file."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
GENERATED_DIR = DATA_DIR / "generated_voices"
MAX_VOICE_CHARS = 200
TEST_PHRASE = "嗯，我在。"
DEFAULT_MODEL = "cosyvoice-v3.5-plus"
_WS_INFERENCE_PATH = "/api-ws/v1/inference"
_SYNTH_LOCK = threading.Lock()
_MODEL_PREFIXES = (
    "cosyvoice-v3.5-plus",
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3-plus",
    "cosyvoice-v3-flash",
    "cosyvoice-v2",
)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def websocket_url_from_base(base_url: str) -> str:
    """Turn a workspace HTTP/WS root into the CosyVoice inference WebSocket URL."""
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("Base URL is required")
    if "://" not in raw:
        host = raw.split("/")[0].strip()
        if not host:
            raise ValueError("Base URL is invalid")
        return f"wss://{host}{_WS_INFERENCE_PATH}"
    parsed = urlparse(raw)
    host = parsed.netloc
    if not host:
        raise ValueError("Base URL is invalid")
    path = parsed.path.rstrip("/")
    if parsed.scheme in {"ws", "wss"} and path.endswith(_WS_INFERENCE_PATH):
        return raw.rstrip("/")
    scheme = "ws" if parsed.scheme == "ws" else "wss"
    return f"{scheme}://{host}{_WS_INFERENCE_PATH}"


def workspace_id_from_ws_url(ws_url: str) -> str:
    """Dedicated MAAS hosts encode the workspace as the first DNS label."""
    host = urlparse(ws_url).netloc or ""
    label = host.split(".")[0]
    if label.startswith("ws-") and "maas.aliyuncs.com" in host:
        return label
    return ""


def clean_voice_id(voice_id: str) -> str:
    return (voice_id or "").strip().strip('"').strip("'")


def model_for_voice(voice_id: str, model: str) -> str:
    """Keep synthesis model aligned with the voice_id target_model prefix.

    CosyVoice 418 is the engine rejecting a voice/model pair.
    """
    cleaned = clean_voice_id(voice_id)
    for prefix in _MODEL_PREFIXES:
        if cleaned.startswith(prefix + "-"):
            return prefix
    return (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def cosyvoice_start_parameters(voice_id: str) -> dict:
    """Minimal CosyVoice run-task params. SDK DEFAULT sends format=Default
    and sample_rate=0; call() also forces enable_ssml, which designed
    voices reject with engine 418.
    """
    return {
        "voice": clean_voice_id(voice_id),
        "format": "wav",
        "sample_rate": 24000,
        "volume": 50,
        "rate": 1.0,
        "text_type": "PlainText",
    }


def is_voice_config_ready(config: dict | None) -> bool:
    cfg = config or {}
    provider = str(cfg.get("VOICE_PROVIDER") or "").strip().lower()
    return (
        provider == "dashscope"
        and bool(str(cfg.get("VOICE_API_KEY") or "").strip())
        and bool(str(cfg.get("VOICE_MODEL") or "").strip())
        and bool(str(cfg.get("VOICE_BASE_URL") or "").strip())
        and bool(str(cfg.get("VOICE_ID") or "").strip())
    )


def convert_to_ogg_opus(audio: bytes) -> bytes:
    """Re-encode synthesized audio as OGG/Opus for Telegram sendVoice."""
    if not ffmpeg_available():
        raise RuntimeError("本机没有 ffmpeg，无法把语音转成 Telegram 语音气泡")
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "24k",
            "-application", "voip",
            "-f", "ogg", "pipe:1",
        ],
        input=audio,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(detail or "ffmpeg 转码失败")
    return proc.stdout


def write_generated_voice(data: bytes) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{uuid.uuid4().hex}.ogg"
    path.write_bytes(data)
    return path


def synthesize_dashscope(
    text: str,
    *,
    api_key: str,
    ws_url: str,
    model: str,
    voice_id: str,
) -> bytes:
    """Call DashScope CosyVoice over WebSocket. Blocking; run in a worker."""
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    voice = clean_voice_id(voice_id)
    synth_model = model_for_voice(voice, model)
    workspace = workspace_id_from_ws_url(ws_url)
    kwargs = {
        "model": synth_model,
        "voice": voice,
        "format": AudioFormat.WAV_24000HZ_MONO_16BIT,
        "url": ws_url,
    }
    if workspace:
        kwargs["workspace"] = workspace

    with _SYNTH_LOCK:
        dashscope.api_key = api_key
        dashscope.base_websocket_api_url = ws_url
        synthesizer = SpeechSynthesizer(**kwargs)
        orig_start = synthesizer.request.get_start_request

        def get_start_request(additional_params=None):
            cmd = json.loads(orig_start({}))
            cmd["payload"]["parameters"] = cosyvoice_start_parameters(voice)
            return json.dumps(cmd, ensure_ascii=False)

        synthesizer.request.get_start_request = get_start_request
        audio = synthesizer.call(text)
    if not audio:
        raise RuntimeError("speech synthesis returned empty bytes")
    return audio


def _voice_failure_message(exc: BaseException) -> str:
    from mochi.llm import describe_error

    raw = f"{exc}"
    if "418" in raw or "InvalidParameter" in raw:
        return (
            "合成语音失败：音色无效，或模型和 Voice ID 不匹配。"
            "请确认 Voice ID 属于当前业务空间，且模型与报名时的 "
            "target_model 一致（例如 cosyvoice-v3.5-plus）。"
        )
    return f"合成语音失败：{describe_error(exc)}"


class VoiceSkill(Skill):

    def tool_available(
        self,
        tool_name: str,
        *,
        user_id: int = 0,
        transport: str = "",
    ) -> bool:
        if tool_name != "send_voice":
            return True
        return is_voice_config_ready(self.config) and ffmpeg_available()

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "send_voice":
            return SkillResult(
                output=f"Unknown voice tool: {context.tool_name}",
                success=False,
            )

        text = str(context.args.get("text") or "").strip()
        if not text:
            return SkillResult(output="请说明要说出口的那句话。", success=False)
        if len(text) > MAX_VOICE_CHARS:
            return SkillResult(
                output=f"这句话太长了，语音气泡请控制在 {MAX_VOICE_CHARS} 字以内。",
                success=False,
            )

        if not is_voice_config_ready(self.config):
            return SkillResult(
                output="语音合成尚未配置。在管理后台模型页填好百炼 Key、地址和 voice_id 后再试。",
                success=False,
            )
        if not ffmpeg_available():
            return SkillResult(
                output="本机没有 ffmpeg，无法发出 Telegram 语音气泡。",
                success=False,
            )

        try:
            path = await asyncio.to_thread(self._generate, text)
        except Exception as exc:
            log.warning("send_voice failed: %s", exc)
            return SkillResult(output=_voice_failure_message(exc), success=False)
        return SkillResult(output=f"[VOICE_FILE:{path}]")

    def _generate(self, text: str) -> Path:
        audio = synthesize_configured(text, self.config)
        ogg = convert_to_ogg_opus(audio)
        return write_generated_voice(ogg)


def synthesize_configured(text: str, config: dict | None = None) -> bytes:
    """Synthesize with the current skill config. Used by send_voice and Admin test."""
    cfg = config or {}
    api_key = str(cfg.get("VOICE_API_KEY") or "").strip()
    voice_id = clean_voice_id(str(cfg.get("VOICE_ID") or ""))
    model = model_for_voice(
        voice_id, str(cfg.get("VOICE_MODEL") or DEFAULT_MODEL),
    )
    ws_url = websocket_url_from_base(str(cfg.get("VOICE_BASE_URL") or ""))
    return synthesize_dashscope(
        text,
        api_key=api_key,
        ws_url=ws_url,
        model=model,
        voice_id=voice_id,
    )


def run_voice_test(config: dict | None = None) -> dict:
    """Synthesize a short phrase and transcode it. Does not send to Telegram."""
    if not is_voice_config_ready(config):
        raise RuntimeError("语音合成尚未配置")
    if not ffmpeg_available():
        raise RuntimeError("本机没有 ffmpeg")
    audio = synthesize_configured(TEST_PHRASE, config)
    ogg = convert_to_ogg_opus(audio)
    if not ogg:
        raise RuntimeError("转码结果为空")
    return {"bytes": len(ogg), "model": str((config or {}).get("VOICE_MODEL") or DEFAULT_MODEL)}
