"""Framework tool for Main-requested bedtime transitions."""

ENTER_BEDTIME_TOOL_NAME = "enter_bedtime"

ENTER_BEDTIME_DEF = {
    "type": "function",
    "function": {
        "name": ENTER_BEDTIME_TOOL_NAME,
        "description": (
            "当用户明确准备结束对话去睡觉时，在当前告别送达后进入休息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}
