"""Bounded incremental sentence splitting and turn control for realtime replies.

Pure logic, no model or I/O dependencies, so it can be tested with arbitrary
text chunks. The model thread consumes complete sentences from the queue while
the network thread keeps reading LLM output, enabling text/audio overlap.
"""
from __future__ import annotations

MAX_SEGMENT_CHARS = 80
MAX_QUEUE = 4

# Chinese sentence-ending punctuation plus line breaks; a trailing quote is
# carried with the sentence.
_END = "。！？!?；;\n"
_TRAILING = "”\"』』）)】」'"


class SentenceSplitter:
    """Accumulate text chunks and emit complete sentences in order.

    call feed(chunk) -> list[str] of newly completed segments.
    call flush() -> remaining tail as one final segment (or empty).
    """

    def __init__(self, max_chars=MAX_SEGMENT_CHARS):
        self.max_chars = max(2, max_chars)
        self.buffer = ""

    def feed(self, chunk):
        if not chunk:
            return []
        self.buffer += chunk
        out = []
        while True:
            segment = self._take()
            if segment is None:
                break
            out.append(segment)
        return out

    def _take(self):
        text = self.buffer
        if not text:
            return None
        # Natural punctuation boundary within the buffer.
        cut = -1
        for index, char in enumerate(text):
            if char in _END:
                cut = index + 1
                # Keep a closing quote/bracket attached to the same sentence.
                while cut < len(text) and text[cut] in _TRAILING:
                    cut += 1
                break
        if cut > 0:
            segment = text[:cut].strip()
            self.buffer = text[cut:]
            return segment or None
        # No punctuation yet: force a cut near the limit at a reasonable spot.
        if len(text) >= self.max_chars:
            window = text[: self.max_chars]
            split = max(window.rfind("，"), window.rfind(","), window.rfind("、"), window.rfind(" "))
            if split >= self.max_chars // 2:
                cut = split + 1
            else:
                cut = self.max_chars
            segment = text[:cut].strip()
            self.buffer = text[cut:]
            return segment or None
        return None

    def flush(self):
        tail = self.buffer.strip()
        self.buffer = ""
        return tail


class BoundedSegments:
    """Thread-safe bounded FIFO of text segments with a done signal."""

    def __init__(self, maxsize=MAX_QUEUE):
        import threading
        self.maxsize = max(1, maxsize)
        self._items = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._done = False
        self._dropped = 0

    def put(self, item):
        with self._not_empty:
            if self._done:
                return False
            if len(self._items) >= self.maxsize:
                # Backpressure: do not block the network reader indefinitely;
                # bounded waiting gives the model thread a chance to drain.
                self._not_empty.wait(timeout=0.2)
            if self._done:
                return False
            if len(self._items) >= self.maxsize:
                self._dropped += 1
                return False
            self._items.append(item)
            self._not_empty.notify_all()
            return True

    def get(self, timeout=0.2):
        with self._not_empty:
            while not self._items and not self._done:
                self._not_empty.wait(timeout=timeout)
                if not self._items and not self._done:
                    return None
            if self._items:
                return self._items.pop(0)
            return None

    def finish(self):
        with self._not_empty:
            self._done = True
            self._not_empty.notify_all()

    def close(self):
        with self._not_empty:
            self._done = True
            self._items.clear()
            self._not_empty.notify_all()

    @property
    def dropped(self):
        with self._lock:
            return self._dropped


class TurnControl:
    """Monotonic local round id so stale callbacks can be ignored."""

    def __init__(self):
        import itertools
        self._ids = itertools.count(1)
        self._current = 0
        import threading
        self._lock = threading.Lock()

    def begin(self):
        with self._lock:
            self._current = next(self._ids)
            return self._current

    def is_current(self, round_id):
        with self._lock:
            return round_id == self._current

    def invalidate(self):
        with self._lock:
            self._current = -1
