"""Admin skill list metadata used by the restored Skills page."""

from mochi.db import set_skill_enabled
from mochi.skills import get_skill, get_skill_info_all


def test_skill_info_exposes_core_alias_and_toggle():
    infos = {item["name"]: item for item in get_skill_info_all()}
    assert infos, "skills should be discovered in tests"
    photo = infos.get("photo")
    if photo is not None:
        assert "enabled" in photo
        assert "config_schema" in photo
        assert photo["core"] == photo["locked"]

    sample = next(item for item in infos.values() if not item.get("locked"))
    set_skill_enabled(sample["name"], False)
    skill = get_skill(sample["name"])
    if skill is not None:
        skill.refresh_config()
    after = {item["name"]: item for item in get_skill_info_all()}
    assert after[sample["name"]]["admin_disabled"] is True
    set_skill_enabled(sample["name"], True)
