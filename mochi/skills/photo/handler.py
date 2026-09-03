"""Photo skill — send_photo generates an anime-in-real-world image."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import random
import re
import uuid
from pathlib import Path

from mochi.prompt_loader import get_prompt
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.photo.queries import init_photo_refs_schema, list_photo_refs
from mochi.skills.photo.quota import (
    chat_photo_guidance,
    note_photo_send,
    photo_quota_denial,
)
from mochi.skills.photo.seed import DATA_DIR, PHOTO_REFS_DIR, start_seed_thread
from mochi.transport import ImageAttachment

log = logging.getLogger(__name__)

GENERATED_DIR = DATA_DIR / "generated_photos"
_MAX_SELF_REFS = 2
_MAX_SCENE_REFS = 2
_CHINA_HINTS = (
    "北京", "上海", "西湖", "胡同", "苏州", "成都", "广州", "杭州",
    "中国", "夜市", "校园", "寺庙",
)
_JAPAN_HINTS = (
    "神社", "东京", "京都", "大阪", "鸟居", "便利店", "新宿", "涩谷",
    "日本", "秋叶原", "车站",
)
_TYPING_REFRESH_S = 4.0
_DONE_LINES = (
    "照片找到了。",
    "照片拍好了。",
    "找到了，给你看。",
)
_DONE_BANNED = ("出来啦", "出来了", "已生成", "生成好了")
SIZE_LANDSCAPE = "1920*1080"
SIZE_PORTRAIT = "1080*1920"
_ORIENT_HEADING_RE = re.compile(
    r"^(?:画面方向|方向|orientation)\s*",
    re.IGNORECASE,
)


def _photo_failure_message(exc: BaseException) -> str:
    from mochi.llm import describe_error

    return f"生成照片失败：{describe_error(exc)}"


def finish_line_for_user(reply: str) -> str:
    """Keep a short found/shot closer; drop '出来啦' / '已生成'."""
    text = (reply or "").strip()
    if text and not any(token in text for token in _DONE_BANNED):
        return text
    return random.choice(_DONE_LINES)


async def _keep_typing(context: SkillContext, done: asyncio.Event) -> None:
    """Refresh Telegram typing while Draw runs; do not send waiting chat lines."""
    callback = context.on_interim
    if not callback:
        return
    while not done.is_set():
        try:
            await callback(None, tool_name="send_photo")
        except Exception:
            log.debug("photo typing refresh dropped")
        try:
            await asyncio.wait_for(done.wait(), timeout=_TYPING_REFRESH_S)
            return
        except asyncio.TimeoutError:
            continue


def infer_scene_region(subject: str) -> str:
    text = subject or ""
    if any(token in text for token in _CHINA_HINTS):
        return "china"
    if any(token in text for token in _JAPAN_HINTS):
        return "japan"
    return ""


def pick_photo_refs(subject: str) -> list[dict]:
    """Choose 1-2 character refs and 1-2 matching real-world scenes."""
    self_refs = list_photo_refs(kind="self", limit=40)
    chosen: list[dict] = []
    if self_refs:
        chosen.extend(random.sample(self_refs, k=min(_MAX_SELF_REFS, len(self_refs))))
    region = infer_scene_region(subject)
    scenes = list_photo_refs(kind="scene", region=region, limit=60) if region else []
    if not scenes:
        scenes = list_photo_refs(kind="scene", limit=60)
    if scenes:
        chosen.extend(random.sample(scenes, k=min(_MAX_SCENE_REFS, len(scenes))))
    return chosen


def _mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")


def _ref_path(filename: str) -> Path | None:
    path = (PHOTO_REFS_DIR / Path(filename).name).resolve()
    try:
        path.relative_to(PHOTO_REFS_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _reference_images(refs: list[dict]) -> list[tuple[str, bytes]]:
    """At most one character ref and one scene ref — extra images slow Draw."""
    out: list[tuple[str, bytes]] = []
    used_kinds: set[str] = set()
    for ref in refs:
        kind = str(ref.get("kind") or "")
        if kind in used_kinds:
            continue
        path = _ref_path(ref.get("filename") or "")
        if path is None:
            continue
        if kind:
            used_kinds.add(kind)
        out.append((_mime_for(path), path.read_bytes()))
        if len(out) >= 2:
            break
    return out


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return ".jpg"


def _clean_prompt(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:\w+)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip().strip('"').strip("“”")


def parse_drawn_prompt(text: str) -> tuple[str, str]:
    """Split Main's first-line 横幅/竖幅 from the cinematography prompt."""
    cleaned = _clean_prompt(text)
    if not cleaned:
        return "", "landscape"
    lines = cleaned.splitlines()
    first = _ORIENT_HEADING_RE.sub("", lines[0]).strip().strip("：:")
    first = re.sub(r"\s+", "", first)
    lowered = first.lower()
    portrait = {"竖幅", "竖屏", "竖向", "竖", "portrait", "9:16", "9：16"}
    landscape = {"横幅", "横屏", "横向", "横", "landscape", "16:9", "16：9"}
    rest = "\n".join(lines[1:]).strip()
    if first in portrait or lowered in portrait:
        return rest or cleaned, "portrait"
    if first in landscape or lowered in landscape:
        return rest or cleaned, "landscape"
    return cleaned, "landscape"


def photo_size_for(orientation: str) -> str:
    return SIZE_PORTRAIT if orientation == "portrait" else SIZE_LANDSCAPE


def _build_prompt_messages(subject: str, refs: list[dict], core: str) -> list[dict]:
    instructions = get_prompt("photo_prompt")
    system = instructions
    if core.strip():
        system = f"{instructions}\n\n# Core 人设\n{core.strip()}"
    user_blocks: list[dict] = [{
        "type": "text",
        "text": (
            f"当前想看见的内容：{subject}\n"
            "先写一行画面方向（横幅 或 竖幅），再按模板写出完整中文绘图提示词。"
            "恋恋是动漫画风，背景必须模仿附带的真实风景参考图。"
        ),
    }]
    for ref in refs:
        path = _ref_path(ref.get("filename") or "")
        if path is None:
            continue
        attachment = ImageAttachment(data=path.read_bytes(), media_type=_mime_for(path))
        kind_label = "角色立绘" if ref.get("kind") == "self" else "真实风景"
        user_blocks.append({"type": "text", "text": f"参考（{kind_label}）："})
        user_blocks.append({
            "type": "image_url",
            "image_url": {"url": attachment.data_url(), "detail": "auto"},
        })
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_blocks},
    ]


def write_generated_photo(data: bytes) -> Path:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{uuid.uuid4().hex}{_image_suffix(data)}"
    path.write_bytes(data)
    return path


class PhotoSkill(Skill):

    def tool_available(
        self,
        tool_name: str,
        *,
        user_id: int = 0,
        transport: str = "",
    ) -> bool:
        if tool_name != "send_photo":
            return True
        from mochi.admin.admin_db import is_draw_tier_ready
        return is_draw_tier_ready()

    def prompt_section(self, compact: bool = False) -> str:
        from mochi.config import OWNER_USER_ID

        if not OWNER_USER_ID:
            return ""
        return chat_photo_guidance(OWNER_USER_ID)

    def init_schema(self, conn) -> None:
        init_photo_refs_schema(conn)
        start_seed_thread()

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "send_photo":
            return SkillResult(output=f"Unknown photo tool: {context.tool_name}", success=False)

        subject = str(context.args.get("subject") or "").strip()
        if not subject:
            return SkillResult(output="请说明想看见的内容。", success=False)

        user_text = str(context.args.get("_user_text") or "")
        bucket, denial = photo_quota_denial(
            context.user_id, context.source, user_text,
        )
        if denial:
            return SkillResult(output=denial, success=False)

        try:
            from mochi.admin.admin_db import is_draw_tier_ready
            if not is_draw_tier_ready():
                return SkillResult(
                    output="绘图模型尚未配置。在管理后台把模型赋给 Draw 档后再试。",
                    success=False,
                )
            done = asyncio.Event()
            typer = asyncio.create_task(_keep_typing(context, done))
            try:
                path = await asyncio.to_thread(self._generate, subject)
            finally:
                done.set()
                typer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typer
        except Exception as exc:
            log.warning("send_photo failed: %s", exc)
            return SkillResult(
                output=_photo_failure_message(exc),
                success=False,
            )
        note_photo_send(context.user_id, bucket, turn_id=context.turn_id)
        return SkillResult(output=f"[IMAGE_FILE:{path}] 照片拍好了。")

    def _generate(self, subject: str) -> Path:
        from mochi.core_store import read_core
        from mochi.llm import get_client_for_tier

        refs = pick_photo_refs(subject)
        try:
            core = read_core()
        except Exception:
            core = ""
        prompt, orientation = self._write_prompt(subject, refs, core)
        if not prompt:
            raise RuntimeError("Main did not return a drawing prompt")
        draw = get_client_for_tier("draw")
        data = draw.generate_image(
            prompt,
            reference_images=_reference_images(refs),
            size=photo_size_for(orientation),
        )
        if not data:
            raise RuntimeError("image generation returned empty bytes")
        return write_generated_photo(data)

    def _write_prompt(self, subject: str, refs: list[dict], core: str) -> tuple[str, str]:
        from mochi.llm import get_client_for_tier

        client = get_client_for_tier("main")
        messages = _build_prompt_messages(subject, refs, core)
        response = client.chat(messages, max_tokens=1100)
        prompt, orientation = parse_drawn_prompt(response.content or "")
        return prompt, orientation
