from mochi.tool_router import _build_router_prompt


def test_router_prompt_recalls_complementary_candidate_skills():
    prompt = _build_router_prompt({
        "habit": "track a repeated target",
        "reminder": "contact at a scheduled time",
    })

    assert "不替 Main 决定最终动作" in prompt
    assert "理解、澄清或处理" in prompt
    assert "一条消息可以包含多个目标" in prompt
