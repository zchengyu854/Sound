"""Breeze tone labels and the matching training and inference prompts."""

TONES = {
    "neutral": ("默认", None),
    "question": ("提问", "像日常聊天中的自然提问，句尾轻微上扬。"),
    "comfort": ("安慰", "用温和、安慰的语气说，语速稍慢，句尾轻收。"),
    "bright": ("轻快", "用轻快、真诚的语气说，重读重点，保持日常聊天的节奏。"),
    "serious": ("认真", "用认真、平稳的语气说，清楚地强调重点。"),
    "emphasis": ("强调", "用有力度的语气说，重读重点，保持自然语速。"),
}
