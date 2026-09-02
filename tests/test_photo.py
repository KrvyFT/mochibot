"""Draw tier, chat image generation, photo-ref seeding, and prompt constraints."""

import asyncio
import base64
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
    assert "动漫角色" in text
    assert "现实世界" in text
    assert "但是真人" not in text
    assert "素颜或淡妆" not in text
    assert "人设" in text


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
        def chat(self, messages, max_tokens=700):
            system = next(m["content"] for m in messages if m["role"] == "system")
            assert "动漫角色" in system
            return SimpleNamespace(
                content="动漫角色站在真实咖啡馆窗边搅动咖啡，木质装修，侧光。",
            )

    class _Draw:
        def generate_image(self, prompt, **kwargs):
            assert "动漫角色" in prompt
            assert "咖啡馆" in prompt
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
    assert path.endswith(".png")
    with open(path, "rb") as handle:
        assert handle.read() == png


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


def test_send_photo_chatter_emits_looking_line(tmp_path, monkeypatch):
    from mochi.admin import admin_db
    from mochi.skills.base import SkillContext
    from mochi.skills.photo import handler as photo_handler
    import mochi.core_store as core_store
    import mochi.llm as llm_mod

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    monkeypatch.setattr(admin_db, "decrypt_api_key", lambda value: value)
    monkeypatch.setattr(photo_handler, "_WAIT_DELAYS", (0.01, 0.01))
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
    assert spoken
    assert spoken[0] in photo_handler._START_LINES


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


def test_draw_image_timeout_is_three_minutes():
    assert llm._IMAGE_HTTP_TIMEOUT.read == 180.0
