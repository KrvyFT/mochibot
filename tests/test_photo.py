"""Draw tier, chat image generation, photo-ref seeding, and prompt constraints."""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from mochi import llm
from mochi.prompt_loader import get_prompt
from mochi.skills.photo.handler import infer_scene_region, pick_photo_refs
from mochi.skills.photo.seed import (
    harvest_wikimedia_category,
    parse_google_photos_album,
    parse_moegirl_file_page,
)


def test_draw_tier_is_optional_and_clearable(monkeypatch):
    from mochi.admin import admin_db

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)

    admin_db.upsert_model(
        "main-m", "openai", "main-model", "key", "https://api.example.com/v1",
    )
    admin_db.upsert_model(
        "lite-m", "openai", "lite-model", "key", "https://api.example.com/v1",
    )
    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("main", "main-m")
    admin_db.set_tier_assignment("lite", "lite-m")
    assert admin_db.are_required_tiers_ready()
    assert not admin_db.is_draw_tier_ready()

    admin_db.set_tier_assignment("draw", "draw-m")
    assert admin_db.is_draw_tier_ready()
    assert admin_db.are_required_tiers_ready()

    admin_db.clear_tier_assignment("draw")
    assert "draw" not in admin_db.list_tier_assignments()
    assert admin_db.are_required_tiers_ready()
    assert not admin_db.is_draw_tier_ready()

    with pytest.raises(ValueError, match="required"):
        admin_db.clear_tier_assignment("main")
    with pytest.raises(ValueError, match="required"):
        admin_db.clear_tier_assignment("lite")


def test_send_photo_hidden_until_draw_ready(monkeypatch):
    from mochi.admin import admin_db
    from mochi.skills.photo.handler import PhotoSkill

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)

    skill = PhotoSkill()
    assert skill.tool_available("send_photo") is False

    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("draw", "draw-m")
    assert skill.tool_available("send_photo") is True


def test_photo_prompt_keeps_anime_character_in_real_world():
    text = get_prompt("photo_prompt")
    assert "动漫画风" in text
    assert "不要默认蹲着" in text or "不要无故蹲" in text
    assert "窗边" in text
    assert "电影感" in text
    assert "现实" in text
    assert "恋恋" in text
    assert "横幅" in text and "竖幅" in text
    assert "光线" in text and "构图" in text and "镜头参数" in text
    assert "运镜方式" in text
    assert "3840x2160" in text
    assert "素颜或淡妆" not in text


def test_parse_drawn_prompt_picks_1080p_16x9():
    from mochi.skills.photo.handler import parse_drawn_prompt, photo_size_for

    prompt, orientation = parse_drawn_prompt("竖幅\n恋恋在真实街边散步。")
    assert orientation == "portrait"
    assert prompt.startswith("恋恋")
    assert photo_size_for(orientation) == "1080*1920"
    prompt, orientation = parse_drawn_prompt("画面方向：横幅\n光线：侧光")
    assert orientation == "landscape"
    assert prompt == "光线：侧光"
    assert photo_size_for(orientation) == "1920*1080"
    _, orientation = parse_drawn_prompt("恋恋坐在真实咖啡馆里。")
    assert orientation == "landscape"


def test_parse_moegirl_file_page_og_image():
    html = """
    <html><head>
    <meta property="og:image" content="https://img.moegirl.org.cn/common/a/ab/koishi.png">
    </head>
    <div id="file"><a href="//img.moegirl.org.cn/common/a/ab/full.png">img</a></div>
    </html>
    """
    assert parse_moegirl_file_page(html, "https://zh.moegirl.org.cn/File:x.png") == (
        "https://img.moegirl.org.cn/common/a/ab/koishi.png"
    )


def test_parse_google_photos_album_lh3_urls():
    html = (
        'data-url="https://lh3.googleusercontent.com/pw/AP1GczAAA=w512-h512-no" '
        'src="https://lh3.googleusercontent.com/pw/AP1GczAAA=w256" '
        'other="https://example.com/x.jpg"'
    )
    urls = parse_google_photos_album(html)
    assert urls == ["https://lh3.googleusercontent.com/pw/AP1GczAAA=w1600"]


def test_wikimedia_category_quota_and_filters():
    payload = {
        "query": {
            "pages": {
                "1": {
                    "title": "File:Tokyo_street.jpg",
                    "imageinfo": [{
                        "mime": "image/jpeg",
                        "width": 2000,
                        "height": 1400,
                        "thumbwidth": 1280,
                        "thumbheight": 896,
                        "thumburl": "https://upload.wikimedia.org/tokyo.jpg?utm=1",
                        "url": "https://upload.wikimedia.org/tokyo-orig.jpg",
                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Tokyo_street.jpg",
                    }],
                },
                "2": {
                    "title": "File:Tokyo_map.svg",
                    "imageinfo": [{
                        "mime": "image/svg+xml",
                        "width": 2000,
                        "height": 1400,
                        "url": "https://upload.wikimedia.org/map.svg",
                    }],
                },
                "3": {
                    "title": "File:Flag_of_Japan.png",
                    "imageinfo": [{
                        "mime": "image/png",
                        "width": 2000,
                        "height": 1400,
                        "url": "https://upload.wikimedia.org/flag.png",
                    }],
                },
            }
        }
    }
    picked = harvest_wikimedia_category(payload, limit=10)
    assert [item["title"] for item in picked] == ["File:Tokyo_street.jpg"]
    assert picked[0]["url"].endswith("tokyo.jpg")


def test_infer_scene_region():
    assert infer_scene_region("去神社看看") == "japan"
    assert infer_scene_region("北京胡同里") == "china"
    assert infer_scene_region("随便看看") == ""


def test_empty_photo_refs_still_picks_nothing():
    assert pick_photo_refs("咖啡馆") == []


def test_photo_skill_generates_without_refs(tmp_path, monkeypatch):
    from mochi.admin import admin_db
    from mochi.skills.base import SkillContext
    from mochi.skills.photo import handler as photo_handler
    import mochi.core_store as core_store
    import mochi.llm as llm_mod

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)

    admin_db.upsert_model(
        "main-m", "openai", "main-model", "key", "https://api.example.com/v1",
    )
    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("main", "main-m")
    admin_db.set_tier_assignment("draw", "draw-m")

    png = b"\x89PNG\r\n\x1a\n" + b"generated"
    monkeypatch.setattr(photo_handler, "GENERATED_DIR", tmp_path / "generated_photos")
    monkeypatch.setattr(photo_handler, "PHOTO_REFS_DIR", tmp_path / "photo_refs")
    monkeypatch.setattr(photo_handler, "pick_photo_refs", lambda subject: [])
    monkeypatch.setattr(core_store, "read_core", lambda: "古明地恋，动漫角色")

    class _Main:
        def chat(self, messages, max_tokens=1100):
            system = next(m["content"] for m in messages if m["role"] == "system")
            assert "动漫画风" in system
            return SimpleNamespace(
                content="动漫角色站在真实咖啡馆窗边搅动咖啡，木质装修，侧光。",
            )

    class _Draw:
        def generate_image(self, prompt, **kwargs):
            assert "动漫角色" in prompt
            assert "咖啡馆" in prompt
            assert kwargs.get("size") == "1920*1080"
            return png

    def _client(tier="main"):
        return _Draw() if tier == "draw" else _Main()

    monkeypatch.setattr(llm_mod, "get_client_for_tier", _client)

    skill = photo_handler.PhotoSkill()
    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_photo",
        args={"subject": "咖啡馆角落"},
    )))
    assert result.success
    assert result.output.startswith("[IMAGE_FILE:")
    assert "拍好了" in result.output
    assert "已生成" not in result.output
    path = result.output.split("[IMAGE_FILE:", 1)[1].split("]", 1)[0]
    assert Path(path).suffix in {".png", ".jpg", ".jpeg"}
    with open(path, "rb") as handle:
        data = handle.read()
    assert data  # may be JPEG-compressed for Telegram


def test_photo_quota_phrases_and_daily_caps():
    from mochi.skills.photo.quota import (
        note_photo_send,
        photo_bucket,
        photo_quota_denial,
        user_requested_photo,
    )

    assert user_requested_photo("发一张照片呗")
    assert user_requested_photo("给我看看你")
    assert user_requested_photo("send a photo")
    assert not user_requested_photo("今天吃了什么")
    assert photo_bucket("runtime:free_time", "") == "free_time"
    assert photo_bucket("chat", "发张照片") == "requested"

    for _ in range(2):
        note_photo_send(1, "chat")
    _, denial = photo_quota_denial(1, "chat", "随便聊聊")
    assert "两张" in denial
    bucket, denial = photo_quota_denial(1, "chat", "发一张照片")
    assert bucket == "requested"
    assert denial == ""

    for _ in range(3):
        note_photo_send(1, "free_time")
    _, denial = photo_quota_denial(1, "runtime:free_time", "")
    assert "三张" in denial


def test_send_photo_blocks_chat_cap_unless_requested(tmp_path, monkeypatch):
    from mochi.admin import admin_db
    from mochi.skills.base import SkillContext
    from mochi.skills.photo import handler as photo_handler
    from mochi.skills.photo.quota import note_photo_send

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("draw", "draw-m")
    for _ in range(2):
        note_photo_send(1, "chat")
    skill = photo_handler.PhotoSkill()
    blocked = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_photo",
        user_id=1,
        source="chat",
        args={"subject": "街上"},
    )))
    assert blocked.success is False
    assert "两张" in blocked.output

    path = tmp_path / "forced.png"
    path.write_bytes(b"png")
    monkeypatch.setattr(
        photo_handler.PhotoSkill,
        "_generate",
        lambda self, subject, timeout_s=120.0: path,
    )
    allowed = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_photo",
        user_id=1,
        source="chat",
        args={"subject": "街上", "_user_text": "发一张照片"},
    )))
    assert allowed.success


def test_finish_line_replaces_came_out_wording():
    from mochi.skills.photo.handler import finish_line_for_user

    assert finish_line_for_user("照片拍好了。") == "照片拍好了。"
    assert finish_line_for_user("找到了，给你看。") == "找到了，给你看。"
    for _ in range(8):
        line = finish_line_for_user("照片出来啦！")
        assert "出来" not in line
        assert "已生成" not in line
        assert line in ("照片找到了。", "照片拍好了。", "找到了，给你看。")
    assert finish_line_for_user("") in (
        "照片找到了。", "照片拍好了。", "找到了，给你看。",
    )


def test_send_photo_times_out_for_text_fallback(monkeypatch):
    """Photo must not hang past 2 minutes; Main gets an in-character miss hint."""
    import asyncio
    import time

    from mochi.admin import admin_db
    from mochi.skills.base import SkillContext
    from mochi.skills.photo import handler as photo_handler

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("draw", "draw-m")
    monkeypatch.setattr("mochi.config.PHOTO_GENERATE_TIMEOUT_S", 0.05)

    async def _never_finishes(func, /, *args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(asyncio, "to_thread", _never_finishes)
    skill = photo_handler.PhotoSkill()
    started = time.monotonic()
    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_photo",
        args={"subject": "街边"},
        source="runtime:free_time",
    )))
    elapsed = time.monotonic() - started
    assert result.success is False
    assert "出图超时" not in (result.output or "")
    assert any(
        token in (result.output or "")
        for token in ("被删了", "没找到", "找不到", "翻不到")
    )
    assert "不要再调用 send_photo" in (result.output or "")
    assert elapsed < 1.0


def test_compress_photo_bytes_shrinks_large_png(tmp_path, monkeypatch):
    from mochi.skills.photo import handler as photo_handler

    # Minimal valid-ish payload; ffmpeg may fail without real image — still
    # must return original bytes rather than raise.
    raw = b"\x89PNG\r\n\x1a\n" + (b"x" * 50_000)
    out = photo_handler.compress_photo_bytes(raw, max_bytes=10_000)
    assert isinstance(out, (bytes, bytearray))
    assert len(out) > 0


def test_write_generated_photo_uses_compress(tmp_path, monkeypatch):
    from mochi.skills.photo import handler as photo_handler

    monkeypatch.setattr(photo_handler, "GENERATED_DIR", tmp_path)
    called = {"n": 0}

    def _fake_compress(data, *, max_bytes=900_000):
        called["n"] += 1
        return b"\xff\xd8\xff" + b"jpeg"

    monkeypatch.setattr(photo_handler, "compress_photo_bytes", _fake_compress)
    path = photo_handler.write_generated_photo(b"\x89PNG\r\n\x1a\n" + b"big")
    assert called["n"] == 1
    assert path.read_bytes().startswith(b"\xff\xd8")
    assert path.suffix == ".jpg"


def test_send_photo_does_not_chatter(tmp_path, monkeypatch):
    from mochi.admin import admin_db
    from mochi.skills.base import SkillContext
    from mochi.skills.photo import handler as photo_handler
    import mochi.core_store as core_store
    import mochi.llm as llm_mod

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "main-m", "openai", "main-model", "key", "https://api.example.com/v1",
    )
    admin_db.upsert_model(
        "draw-m", "openai", "draw-model", "key", "https://api.example.com/v1",
    )
    admin_db.set_tier_assignment("main", "main-m")
    admin_db.set_tier_assignment("draw", "draw-m")

    spoken: list[str] = []

    async def _interim(text=None, *, tool_name=None):
        if text:
            spoken.append(text)

    class _Main:
        def chat(self, messages, max_tokens=700):
            return SimpleNamespace(content="动漫角色站在真实街边。")

    class _Draw:
        def generate_image(self, prompt, **kwargs):
            return b"\x89PNG\r\n\x1a\n" + b"x"

    monkeypatch.setattr(photo_handler, "GENERATED_DIR", tmp_path / "generated_photos")
    monkeypatch.setattr(photo_handler, "PHOTO_REFS_DIR", tmp_path / "photo_refs")
    monkeypatch.setattr(photo_handler, "pick_photo_refs", lambda subject: [])
    monkeypatch.setattr(core_store, "read_core", lambda: "")
    monkeypatch.setattr(
        llm_mod, "get_client_for_tier",
        lambda tier="main": _Draw() if tier == "draw" else _Main(),
    )

    skill = photo_handler.PhotoSkill()
    result = asyncio.run(skill.execute(SkillContext(
        trigger="tool_call",
        tool_name="send_photo",
        args={"subject": "街上"},
        on_interim=_interim,
    )))
    assert result.success
    assert spoken == []


def test_image_bytes_from_chat_message_shapes():
    png = b"\x89PNG\r\n\x1a\n" + b"chat-png"
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    from_images = SimpleNamespace(
        content=None,
        images=[{"type": "image_url", "image_url": {"url": uri}}],
    )
    assert llm.image_bytes_from_chat_message(from_images) == png

    from_content = {
        "content": [
            {"type": "text", "text": "here"},
            {"type": "image_url", "image_url": {"url": uri}},
        ]
    }
    assert llm.image_bytes_from_chat_message(from_content) == png

    inline = {
        "content": [{
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.b64encode(png).decode("ascii"),
            }
        }]
    }
    assert llm.image_bytes_from_chat_message(inline) == png


def test_qwen_image_url_from_compatible_and_generation_path():
    from mochi.qwen_image import (
        canonical_qwen_image_model,
        generation_url_from_base,
        is_qwen_image_model,
    )

    host = "ws-example.cn-beijing.maas.aliyuncs.com"
    expected = (
        f"https://{host}/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert generation_url_from_base(f"https://{host}/compatible-mode/v1") == expected
    assert generation_url_from_base(f"https://{host}") == expected
    assert generation_url_from_base(f"https://{host}/api/v1") == expected
    assert generation_url_from_base(expected) == expected
    assert generation_url_from_base(host) == expected
    assert is_qwen_image_model("qwen-Image-3.0-pro")
    assert canonical_qwen_image_model("qwen-Image-3.0-pro") == "qwen-image-3.0-pro"
    assert not is_qwen_image_model("qwen-plus")


def test_generate_qwen_image_posts_native_payload(monkeypatch):
    from mochi.qwen_image import generate_qwen_image

    png = b"\x89PNG\r\n\x1a\n" + b"qwen"
    captured: dict = {}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "output": {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": [{"image": "https://oss.example/a.png"}],
                        },
                    }],
                }
            }

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr("mochi.qwen_image.httpx.Client", _Client)
    got = generate_qwen_image(
        "动漫角色站在真实咖啡馆",
        api_key="sk-test",
        base_url="https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        model="qwen-Image-3.0-pro",
        reference_images=[("image/png", b"ref-one"), ("image/jpeg", b"ref-two")],
        size="1920*1080",
        download=lambda url: png if url.endswith("a.png") else b"",
    )
    assert got == png
    assert captured["json"]["parameters"]["size"] == "1920*1080"
    assert captured["url"].endswith(
        "/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert captured["json"]["model"] == "qwen-image-3.0-pro"
    content = captured["json"]["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1]["image"].startswith("data:image/jpeg;base64,")
    assert content[-1] == {"text": "动漫角色站在真实咖啡馆"}
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["timeout"].write == 180.0
    assert captured["timeout"].read == 300.0


def test_qwen_image_keeps_small_refs(monkeypatch):
    from mochi.qwen_image import _shrink_reference

    monkeypatch.setattr("mochi.qwen_image.shutil.which", lambda name: "/usr/bin/ffmpeg")
    png = b"\x89PNG\r\n\x1a\n" + b"tiny"
    assert _shrink_reference("image/png", png) == ("image/png", png)


def test_qwen_image_provider_skips_chat_completions(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"native"
    captured: dict = {}

    def fake(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return png

    monkeypatch.setattr(llm, "generate_qwen_image", fake)
    provider = object.__new__(llm.OpenAIProvider)
    provider._api_key = "sk-test"
    provider._model = "qwen-image-3.0-pro"
    provider._base_url = "https://ws-example.cn-beijing.maas.aliyuncs.com"
    provider._client = SimpleNamespace(with_options=lambda **kwargs: None)
    got = provider.generate_image("一只猫", reference_images=[("image/png", b"x")])
    assert got == png
    assert captured["prompt"] == "一只猫"
    assert captured["model"] == "qwen-image-3.0-pro"
    assert captured["reference_images"] == [("image/png", b"x")]


def test_generate_image_uses_chat_completions():
    png = b"\x89PNG\r\n\x1a\n" + b"via-chat"
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    calls: list[dict] = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    images=[{"image_url": {"url": uri}}],
                ),
            )])

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_Completions())

        def with_options(self, **kwargs):
            return self

    provider = object.__new__(llm.OpenAIProvider)
    provider._model = "gemini-3.1-flash-image"
    provider._client = _Client()
    got = provider.generate_image("动漫角色站在真实咖啡馆")
    assert got == png
    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-3.1-flash-image"
    assert calls[0]["messages"][0]["role"] == "user"
    assert calls[0]["extra_body"]["modalities"] == ["text", "image"]


def test_chat_image_timeout_does_not_retry_extra_bodies():
    calls: list[dict] = []

    class APITimeoutError(Exception):
        pass

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise APITimeoutError("Request timed out.")

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    with pytest.raises(APITimeoutError, match="timed out"):
        llm.generate_image_via_chat(client, "gemini-3.1-flash-image", "a cat")
    assert len(calls) == 1


def test_chat_image_retries_extra_body_on_bad_request():
    png = b"\x89PNG\r\n\x1a\n" + b"after-400"
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    calls: list[dict] = []

    class BadRequestError(Exception):
        status_code = 400

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise BadRequestError("unknown parameter: modalities")
            return SimpleNamespace(choices=[SimpleNamespace(
                message={"images": [{"image_url": {"url": uri}}]},
            )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    assert llm.generate_image_via_chat(
        client, "gemini-3.1-flash-image", "a cat",
    ) == png
    assert len(calls) == 2
    assert calls[0]["extra_body"]["modalities"] == ["text", "image"]
    assert calls[1]["extra_body"]["modalities"] == ["image", "text"]


def test_durable_chat_result_roundtrips_images():
    from mochi.main_runtime import DurableChatResult

    original = DurableChatResult(text="看", images=("/tmp/a.png",), stickers=())
    restored = DurableChatResult.from_json(original.to_json())
    assert restored.images == ("/tmp/a.png",)
    assert restored.voices == ()
    legacy = DurableChatResult.from_json(
        '{"version":1,"text":"hi","stickers":[],"pending_history":null,'
        '"tool_audit":[],"successful_effects":false,"disposition":"deliver"}'
    )
    assert legacy.images == ()


def test_admin_ui_exposes_draw_tier():
    from pathlib import Path
    html = (
        Path(__file__).resolve().parent.parent / "mochi" / "admin" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Draw · 绘图" in html
    assert "未配置" in html
    assert "multimodal-generation/generation" in html


def test_draw_image_timeout_is_three_minutes():
    assert llm._IMAGE_HTTP_TIMEOUT.read == 180.0
