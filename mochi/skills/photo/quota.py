"""Daily send_photo quotas: Free Time 1–3, chat at most 2, requested unlimited."""

from __future__ import annotations

import re

from mochi.config import logical_today
from mochi.skills.photo.queries import count_photo_sends, record_photo_send

FREE_TIME_MIN = 1
FREE_TIME_MAX = 3
CHAT_MAX = 2

_REQUEST_RE = re.compile(
    r"(?:"
    r"发[一两几]?(?:张|个)?(?:照片|相片|自拍|图)|"
    r"拍[一两几]?(?:张|个)?(?:照|照片|相片)|"
    r"来[一两几]?(?:张|个)?(?:照片|相片|图)|"
    # 「再发一张」「发一张看看」「重拍一张」——张后可不带「照片」
    r"(?:再|重新)?发[一两几]?(?:张|个)(?:看看|瞧瞧|来看)?"
    r"|(?:再|重新)?(?:拍|重拍)[一两几]?(?:张|个)(?:看看|瞧瞧)?"
    r"|(?:没|没能|没有)?拍好|"
    r"看看(?:这?张)?(?:照片|相片|图|自拍)|"
    r"(?:给|让)我看看你|"
    r"看看你(?:现在|今天|自己)?"
    r"|想看你(?:的)?(?:照片|相片)?"
    r"|出镜|自拍|"
    r"send\s*(?:a\s*)?(?:photo|pic(?:ture)?)"
    r")",
    re.IGNORECASE,
)


def user_requested_photo(text: str) -> bool:
    return bool(text and _REQUEST_RE.search(text))


def photo_bucket(source: str, user_text: str) -> str:
    if user_requested_photo(user_text):
        return "requested"
    if (source or "").startswith("runtime:free_time"):
        return "free_time"
    return "chat"


def today_photo_count(user_id: int, bucket: str, *, day: str | None = None) -> int:
    return count_photo_sends(user_id, day or logical_today(), bucket)


def photo_quota_denial(
    user_id: int,
    source: str,
    user_text: str,
    *,
    day: str | None = None,
) -> tuple[str, str]:
    """Return (bucket, denial). Empty denial means the send is allowed."""
    bucket = photo_bucket(source, user_text)
    if bucket == "requested":
        return bucket, ""
    day = day or logical_today()
    if bucket == "free_time":
        if today_photo_count(user_id, "free_time", day=day) >= FREE_TIME_MAX:
            return bucket, "今天 Free Time 照片已经发过三张了。"
        return bucket, ""
    if today_photo_count(user_id, "chat", day=day) >= CHAT_MAX:
        return bucket, "今天闲聊照片已经发过两张了。对方点名要看才再发。"
    return bucket, ""


def note_photo_send(
    user_id: int,
    bucket: str,
    *,
    turn_id: str = "",
    day: str | None = None,
) -> None:
    record_photo_send(user_id, day or logical_today(), bucket, turn_id)


def note_photo_miss(
    user_id: int,
    bucket: str,
    *,
    turn_id: str = "",
    day: str | None = None,
) -> None:
    """Record a failed Free Time / chat draw so we stop re-forcing the same day."""
    miss_bucket = "free_time_miss" if bucket == "free_time" else f"{bucket}_miss"
    record_photo_send(user_id, day or logical_today(), miss_bucket, turn_id)


def free_time_photo_must_send(user_id: int, *, day: str | None = None) -> bool:
    from mochi.admin.admin_db import is_draw_tier_ready

    if not is_draw_tier_ready():
        return False
    day = day or logical_today()
    if today_photo_count(user_id, "free_time", day=day) >= FREE_TIME_MIN:
        return False
    # One timed-out/failed attempt is enough — do not burn every slot on miss copy.
    if today_photo_count(user_id, "free_time_miss", day=day) >= 1:
        return False
    return True


def free_time_photo_guidance(user_id: int, *, day: str | None = None) -> str:
    from mochi.admin.admin_db import is_draw_tier_ready

    if not is_draw_tier_ready():
        return ""
    day = day or logical_today()
    n = today_photo_count(user_id, "free_time", day=day)
    misses = today_photo_count(user_id, "free_time_miss", day=day)
    if n >= FREE_TIME_MAX:
        return "今日 Free Time 照片已满三张，不要再调用 send_photo。"
    if misses >= 1 and n < FREE_TIME_MIN:
        return (
            "今日 Free Time 出图已经失败过。不要再调用 send_photo，"
            "也不要提照片找不到、被删了、夹进书里；直接用文字找他说话，换一件新事。"
        )
    if n < FREE_TIME_MIN:
        return (
            "今日 Free Time 还没发过照片。这一轮尽量调用 send_photo，"
            "递一张照片给他看：可以是自己在真实世界散步/游玩/吃饭，"
            "也可以是刚在网上看到的景物或物件画成/拍成的样子。"
            "字仍然要找他说话。若出图失败，不要反复提照片，用文字把事说完即可。"
        )
    remaining = FREE_TIME_MAX - n
    return (
        f"今日 Free Time 已发 {n} 张照片，还可以再发 {remaining} 张（最多三张）。"
        "可以发也可以不发。"
    )


def chat_photo_guidance(user_id: int, *, day: str | None = None) -> str:
    from mochi.admin.admin_db import is_draw_tier_ready

    if not is_draw_tier_ready():
        return ""
    n = today_photo_count(user_id, "chat", day=day)
    remaining = CHAT_MAX - n
    if remaining <= 0:
        return (
            "今日闲聊照片已满两张。除非用户明确要求发照片，不要再调用 send_photo。"
        )
    return (
        f"今日闲聊还可以主动发 {remaining} 张照片（每天最多两张）。"
        "有时发一张自己在真实街景里的日常，不要每轮都发。"
        "用户点名要看的不算进这两张。"
    )
