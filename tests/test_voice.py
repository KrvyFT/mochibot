"""Voice skill: CosyVoice URL normalization, send_voice side-channel, durable payload."""

import asyncio
from pathlib import Path

from mochi.ai_client import (
    VOICE_FILE_RE,
    _has_visible_payload,
    _history_placeholder,
)
from mochi.heartbeat_runtime import remove_delivered_component
from mochi.main_runtime import DurableChatResult
from mochi.skills.base import SkillContext
from mochi.skills.voice import handler as voice_handler
from mochi.skills.voice.handler import (
    MAX_VOICE_CHARS,
    VoiceSkill,
    clean_voice_id,
    cosyvoice_start_parameters,
    is_voice_config_ready,
    model_for_voice,
    websocket_url_from_base,
    workspace_id_from_ws_url,
    _voice_failure_message,
)
from mochi.transport.utils import clean_reply_markers, strip_stage_directions


READY_CONFIG = {
    "VOICE_PROVIDER": "dashscope",
    "VOICE_API_KEY": "sk-test",
    "VOICE_MODEL": "cosyvoice-v3.5-plus",
    "VOICE_BASE_URL": (
        "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    ),
    "VOICE_ID": "cosyvoice-v3.5-plus-vd-announcer-test",
}


def test_websocket_url_from_compatible_and_workspace_root():
    host = "ws-example.cn-beijing.maas.aliyuncs.com"
    expected = f"wss://{host}/api-ws/v1/inference"
    assert websocket_url_from_base(
        f"https://{host}/compatible-mode/v1"
    ) == expected
    assert websocket_url_from_base(f"https://{host}") == expected
    assert websocket_url_from_base(f"https://{host}/") == expected
    assert websocket_url_from_base(expected) == expected
    assert websocket_url_from_base(host) == expected


def test_workspace_and_voice_id_helpers():
    host = "ws-3qxnr8a1ywbopjvt.cn-beijing.maas.aliyuncs.com"
    assert workspace_id_from_ws_url(f"wss://{host}/api-ws/v1/inference") == (
        "ws-3qxnr8a1ywbopjvt"
    )
    assert workspace_id_from_ws_url("wss://dashscope.aliyuncs.com/api-ws/v1/inference") == ""
    assert clean_voice_id('  "cosyvoice-v3.5-plus-vd-announcer-abc"  ') == (
        "cosyvoice-v3.5-plus-vd-announcer-abc"
    )


def test_model_for_voice_follows_voice_id_prefix():
    voice = "cosyvoice-v3.5-plus-vd-announcer-abc"
    assert model_for_voice(voice, "cosyvoice-v2") == "cosyvoice-v3.5-plus"
    assert model_for_voice("custom-voice", "cosyvoice-v3.5-plus") == "cosyvoice-v3.5-plus"


def test_cosyvoice_start_parameters_omit_sdk_defaults():
    params = cosyvoice_start_parameters("cosyvoice-v3.5-plus-vd-announcer-abc")
    assert params["format"] == "wav"
    assert params["sample_rate"] == 24000
    assert "enable_ssml" not in params
    assert "seed" not in params
    assert params["voice"] == "cosyvoice-v3.5-plus-vd-announcer-abc"


def test_voice_418_maps_to_mismatch_message():
    msg = _voice_failure_message(
        RuntimeError("[cosyvoice]Engine return error code: 418")
    )
    assert "音色无效" in msg
    assert "target_model" in msg


def test_voice_config_ready_requires_all_fields():
    assert is_voice_config_ready(READY_CONFIG)
    incomplete = dict(READY_CONFIG)
    incomplete["VOICE_ID"] = ""
    assert not is_voice_config_ready(incomplete)
    assert not is_voice_config_ready({"VOICE_PROVIDER": "dashscope"})


def test_send_voice_hidden_until_configured(monkeypatch):
    skill = VoiceSkill()
    skill.config = {}
    monkeypatch.setattr(voice_handler, "ffmpeg_available", lambda: True)
    assert skill.tool_available("send_voice") is False
    skill.config = READY_CONFIG
    assert skill.tool_available("send_voice") is True
    monkeypatch.setattr(voice_handler, "ffmpeg_available", lambda: False)
    assert skill.tool_available("send_voice") is False


def test_send_voice_rejects_empty_and_long_text():
    skill = VoiceSkill()
    skill.config = READY_CONFIG
    empty = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_voice",
        args={"text": "  "},
    )))
    assert empty.success is False
    long = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_voice",
        args={"text": "啊" * (MAX_VOICE_CHARS + 1)},
    )))
    assert long.success is False
    assert "太长" in long.output


def test_send_voice_writes_ogg_marker(tmp_path, monkeypatch):
    skill = VoiceSkill()
    skill.config = READY_CONFIG
    monkeypatch.setattr(voice_handler, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        voice_handler, "synthesize_configured", lambda text, config=None: b"mp3-bytes",
    )
    monkeypatch.setattr(
        voice_handler, "convert_to_ogg_opus", lambda audio: b"OggS-fake",
    )
    monkeypatch.setattr(voice_handler, "GENERATED_DIR", tmp_path / "generated_voices")
    spoken: list[str] = []

    async def _interim(text=None, *, tool_name=None):
        if text:
            spoken.append(text)

    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_voice",
        args={"text": "晚安。"},
        on_interim=_interim,
    )))
    assert result.success
    assert spoken == []
    assert result.output.startswith("[VOICE_FILE:")
    assert "录好了" not in result.output
    path = result.output.split("[VOICE_FILE:", 1)[1].split("]", 1)[0]
    assert path.endswith(".ogg")
    assert Path(path).read_bytes() == b"OggS-fake"
    match = VOICE_FILE_RE.search(result.output)
    assert match and match.group(1) == path


def test_voice_file_marker_stripped_from_outgoing_text():
    cleaned = clean_reply_markers("先听这段。[VOICE_FILE:/tmp/a.ogg]")
    assert "VOICE_FILE" not in cleaned
    assert "先听这段。" in cleaned


def test_stage_directions_stripped_from_outgoing_text():
    kept = strip_stage_directions(
        "（我一下子坐直了，帽沿差点被风掀起来，声音里都带上了点……压都压不住的小心。）"
        "那道口子真的凿开。"
    )
    assert kept == "那道口子真的凿开。"
    assert strip_stage_directions(
        "（缓了半拍，声音落下来，轻轻软软地递过去）"
    ) == ""
    mixed = clean_reply_markers(
        "风把声音顺过来了。（轻轻软软地递过去）"
    )
    assert mixed == "风把声音顺过来了。"
    assert "（" not in mixed


def test_history_placeholder_and_visible_payload_include_voice():
    assert _history_placeholder("", [], [], ["/tmp/a.ogg"]) == "[语音]"
    assert _has_visible_payload("", [], [], ["/tmp/a.ogg"]) is True
    assert _has_visible_payload("", [], [], []) is False


def test_durable_chat_result_roundtrips_voices():
    original = DurableChatResult(text="", voices=("/tmp/a.ogg",), stickers=())
    restored = DurableChatResult.from_json(original.to_json())
    assert restored.voices == ("/tmp/a.ogg",)
    legacy = DurableChatResult.from_json(
        '{"version":1,"text":"hi","stickers":[],"pending_history":null,'
        '"tool_audit":[],"successful_effects":false,"disposition":"deliver"}'
    )
    assert legacy.voices == ()
    assert legacy.images == ()


def test_remove_delivered_voice_component():
    durable = DurableChatResult(
        text="hi",
        voices=("/tmp/a.ogg", "/tmp/b.ogg"),
        images=("/tmp/c.png",),
    )
    remaining = remove_delivered_component(durable, "voice", "/tmp/a.ogg")
    assert remaining.voices == ("/tmp/b.ogg",)
    assert remaining.images == ("/tmp/c.png",)
    assert remaining.text == "hi"
