"""Начала фраз озвучки по паузам в звуке (BE-016): синтетическая озвучка с известным ответом."""

import random
import unittest

from aiplatform.studio import slides, timing

# Пять абзацев, в каждом по три фразы разной длины.
PARAS = [
    "Короткая фраза. Вторая фраза подлиннее, с запятой посередине. Третья.",
    "Абзац второй начинается здесь. Потом идёт очень длинное объяснение, которое тянется и тянется. Конец.",
    "Третий абзац. Снова, как обычно, пара слов. И ещё немного текста напоследок.",
    "Четвёртый абзац быстрый. Раз два три. Четыре пять шесть семь.",
    "Последний абзац. Вывод для слушателя, что делать дальше. Пока.",
]


def voiced(paras: list[str], rates: list[float], seed: int = 7) -> tuple[dict, list[tuple[float, float]], float]:
    """Озвучка «с ответом»: абзац p говорит со скоростью rates[p] слогов в секунду.

    Между абзацами — пауза 0,7 с, между фразами — 0,45 с, внутри фраз — «запятые» по 0,32 с."""
    rng = random.Random(seed)
    truth, silences, t = {}, [], 0.0
    for p, para in enumerate(paras):
        if p:
            silences.append((t, t + 0.7))
            t += 0.7
        for s, sent in enumerate(slides.sentences(para)):
            if s:
                silences.append((t, t + 0.45))
                t += 0.45
            truth[(p, s)] = t
            length = timing.weight(sent) / rates[p]
            if length > 2.5:  # пауза-запятая где-то посередине фразы
                mid = t + length * rng.uniform(0.35, 0.65)
                silences.append((mid, mid + 0.32))
                length += 0.32
            t += length
    return truth, silences, t


class TimingTest(unittest.TestCase):
    def test_match_keeps_order_and_skips(self):
        # Лишняя точка 4,0 пропущена, третьей границе точки нет — None.
        self.assertEqual(timing.match([1.0, 5.0, 30.0], [1.2, 4.0, 5.1], skip=3.0, window=8.0), [0, 2, None])
        # Длинная пауза перевешивает чуть более близкую короткую.
        self.assertEqual(timing.match([5.0], [5.1, 5.6], skip=3.0, window=8.0, bonus=[0.0, 1.0]), [1])

    def test_long_pause_threshold_follows_this_voice(self):
        silences = [(0, 1.0), (5, 6.1), (9, 9.4), (12, 12.35)]
        self.assertAlmostEqual(timing.long_pause(silences, 2), 0.75)  # две «абзацные» — от 1,0 с
        self.assertEqual(timing.long_pause([], 3), timing.LONG_MAX_S)
        self.assertEqual(timing.long_pause([(0, 0.2)], 1), timing.LONG_MIN_S)

    def test_weight_counts_syllables_and_commas(self):
        self.assertEqual(timing.weight("мама мыла раму"), 6)
        self.assertEqual(timing.weight("раз, два"), 2 + timing.COMMA_SYLLABLES)

    def test_uneven_speech_found_by_pauses(self):
        # Абзацы говорят то быстро, то медленно — по слогам начало абзаца уезжает на секунды.
        truth, silences, total = voiced(PARAS, [3.0, 7.0, 4.0, 9.0, 3.5])
        at = timing.phrase_starts(PARAS, total, silences)
        naive = timing.phrase_starts(PARAS, total, [])
        worst = max(abs(naive[k] - truth[k]) for k in truth)
        self.assertGreater(worst, 2.0, "без пауз оценка ошибается на секунды — иначе тест ничего не проверяет")
        for key, t in truth.items():
            self.assertLessEqual(abs(at[key] - t), timing.LEAD_S + 0.01, key)
            self.assertLessEqual(at[key], t + 0.01, f"{key}: слайд не позже речи")

    def test_without_pauses_falls_back_to_syllables(self):
        at = timing.phrase_starts(PARAS, 100.0, [])
        self.assertEqual(at[(0, 0)], 0.0)
        values = [at[k] for k in sorted(at)]
        self.assertEqual(values, sorted(values), "по порядку")
        self.assertLess(values[-1], 100.0)
        self.assertEqual(timing.phrase_starts([], 10.0, []), {})


if __name__ == "__main__":
    unittest.main()
