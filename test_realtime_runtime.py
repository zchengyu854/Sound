"""Incremental sentence splitting and bounded queue tests for realtime runtime."""
import threading
import time
import unittest

from realtime_runtime import BoundedSegments, SentenceSplitter, TurnControl


class RealtimeRuntimeTests(unittest.TestCase):
    def test_punctuation_boundaries_and_trailing_quote(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed("你好。"), ["你好。"])
        self.assertEqual(splitter.feed("他说“早"), [])
        self.assertEqual(splitter.feed("上好。”再"), ["他说“早上好。”"])
        self.assertEqual(splitter.flush(), "再")

    def test_newline_and_semicolons_split(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed("第一句！第二句？"), ["第一句！", "第二句？"])
        self.assertEqual(splitter.feed("甲；乙\n"), ["甲；", "乙"])

    def test_no_punctuation_forces_cut_at_reasonable_boundary(self):
        splitter = SentenceSplitter(max_chars=10)
        chunks = splitter.feed("这是一段没有标点但很长的中文句子需要切断"
                               "后面还有很多内容")
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        tail = splitter.flush()
        self.assertTrue(all(len(item) <= 10 for item in chunks + ([tail] if tail else [])))

    def test_empty_chunks_and_flush(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed(""), [])
        self.assertEqual(splitter.feed("未完"), [])
        self.assertEqual(splitter.flush(), "未完")
        self.assertEqual(splitter.flush(), "")

    def test_bounded_queue_waits_for_space_instead_of_dropping(self):
        queue = BoundedSegments(maxsize=2)
        self.assertTrue(queue.put("一"))
        self.assertTrue(queue.put("二"))
        started, result = threading.Event(), []

        def producer():
            started.set()
            result.append(queue.put("三"))

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        self.assertEqual(queue.get(0.05), "一")
        thread.join(1)
        self.assertEqual(result, [True])
        queue.finish()
        self.assertEqual(queue.get(0.05), "二")
        self.assertEqual(queue.get(0.05), "三")
        self.assertIsNone(queue.get(0.05))

    def test_queue_close_discards_and_unblocks(self):
        queue = BoundedSegments(maxsize=1)
        self.assertTrue(queue.put("x"))
        started, result = threading.Event(), []

        def producer():
            started.set()
            result.append(queue.put("y"))

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        queue.close()
        thread.join(1)
        self.assertIsNone(queue.get(0.05))
        self.assertEqual(result, [False])
        self.assertFalse(queue.put("y"))

    def test_queue_get_waits_for_item_then_finish(self):
        queue = BoundedSegments(maxsize=2)
        result = []

        def producer():
            time.sleep(0.05)
            queue.put("延迟")
            queue.finish()

        threading.Thread(target=producer, daemon=True).start()
        self.assertEqual(queue.get(0.5), "延迟")
        self.assertIsNone(queue.get(0.2))

    def test_turn_control_ignores_stale_rounds(self):
        control = TurnControl()
        first = control.begin()
        self.assertTrue(control.is_current(first))
        second = control.begin()
        self.assertFalse(control.is_current(first))
        self.assertTrue(control.is_current(second))
        control.invalidate()
        self.assertFalse(control.is_current(second))


if __name__ == "__main__":
    unittest.main()
