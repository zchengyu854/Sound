"""Choose a Breeze delivery instruction from the text of a spoken sentence."""
from realtime_runtime import SentenceSplitter
from tone_styles import TONES

_COMFORT = ("别担心", "别着急", "慢慢来", "没关系", "我理解", "辛苦了")
_POSITIVE = ("太好了", "恭喜", "真棒", "真不错")
_SERIOUS = ("请注意", "务必", "认真说", "重要的是")


def tone_instruction(sentence):
    opening = sentence.lstrip(" “‘「『\"")
    ending = sentence.rstrip(" ”’」』）)】\"'")
    if opening.startswith(_COMFORT):
        return TONES["comfort"][1]
    if opening.startswith(_POSITIVE):
        return TONES["bright"][1]
    if opening.startswith(_SERIOUS):
        return TONES["serious"][1]
    if ending.endswith(("？", "?")):
        return TONES["question"][1]
    if ending.endswith(("！", "!")):
        return TONES["emphasis"][1]
    return None


def spoken_segments(text):
    splitter = SentenceSplitter()
    sentences = []
    for line in text.splitlines():
        sentences.extend(splitter.feed(line.strip()))
        tail = splitter.flush()
        if tail:
            sentences.append(tail)
    spoken = [(sentence, tone_instruction(sentence)) for sentence in sentences
              if any(char.isalnum() for char in sentence)]
    # shortcut: explicit phrases and punctuation only, add a classifier when context is needed.
    return spoken or [(text, None)]
