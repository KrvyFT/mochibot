"""Admin portal: restore the v0.9 sidebar while keeping current Draw / Free Time."""

from pathlib import Path

HTML = Path("mochi/admin/index.html").read_text(encoding="utf-8")


def test_sidebar_has_legacy_pages():
    for page, label in [
        ("settings", "设置"),
        ("models", "模型"),
        ("heartbeat", "Heartbeat"),
        ("basic", "基本配置"),
        ("skills", "技能"),
        ("persona", "人格"),
        ("memory", "记忆库"),
        ("migrate", "搬家"),
    ]:
        assert f'data-page="{page}"' in HTML
        assert label in HTML


def test_setup_guide_and_kept_features():
    assert "Setup Guide" in HTML
    assert "配置模型" in HTML
    assert "消息平台" in HTML
    assert "配置人格" in HTML
    assert "技能管理" in HTML
    assert "Draw · 绘图" in HTML
    assert "语音合成" in HTML
    assert "Free Time 自主思考" in HTML
    assert "允许 Telegram 访客" in HTML
    assert "TELEGRAM_ALLOW_VISITORS" in HTML
    assert "goToPage('skills')" in HTML
    assert "api('GET','/skills')" in HTML or 'api(\'GET\',\'/skills\')' in HTML
