"""Framework tool for Main-requested bedtime transitions."""

ENTER_BEDTIME_TOOL_NAME = "enter_bedtime"

ENTER_BEDTIME_DEF = {
    "type": "function",
    "function": {
        "name": ENTER_BEDTIME_TOOL_NAME,
        "description": (
            "Request bedtime after this reply when the user is genuinely ending "
            "the current conversation to sleep. You can still leave a natural farewell."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}
