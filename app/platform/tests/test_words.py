"""core/words: склонение после числа и разряды — близнец plural/fmtNum из ui.js (TXT-002)."""

import unittest

from aiplatform.core import words

CHANNEL = ("канал", "канала", "каналов")
CASES = {
    0: "каналов",
    1: "канал",
    2: "канала",
    4: "канала",
    5: "каналов",
    11: "каналов",
    12: "каналов",
    14: "каналов",
    21: "канал",
    22: "канала",
    25: "каналов",
    111: "каналов",
    101: "канал",
}


class PluralTest(unittest.TestCase):
    def test_forms(self):
        for n, want in CASES.items():
            with self.subTest(n=n):
                self.assertEqual(words.plural(n, *CHANNEL), want)

    def test_negative_like_positive(self):
        self.assertEqual(words.plural(-3, "подписчик", "подписчика", "подписчиков"), "подписчика")

    def test_fmt_num_groups_with_nbsp(self):
        self.assertEqual(words.fmt_num(1200), "1 200")
        self.assertEqual(words.fmt_num(999), "999")
        self.assertEqual(words.count_of(1200, "просмотр", "просмотра", "просмотров"), "1 200 просмотров")


if __name__ == "__main__":
    unittest.main()
