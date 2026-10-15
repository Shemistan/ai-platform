import unittest

from aiplatform.video import models, timeline
from aiplatform.video.timeline import ValidationError, build_spec


def frames(*positions):
    return [{"upload_id": f"u{i}", "frame_idx": p} for i, p in enumerate(positions)]


class FramesGridTest(unittest.TestCase):
    def test_durations_land_on_8k_plus_1_grid(self):
        for seconds in timeline.DURATIONS_S:
            n = timeline.frames_for_duration(seconds)
            self.assertEqual((n - 1) % 8, 0, seconds)
        self.assertEqual(timeline.frames_for_duration(4), 97)

    def test_presets_are_multiples_of_64(self):
        for key, (w, h, _) in timeline.PRESETS.items():
            self.assertEqual((w % 64, h % 64), (0, 0), key)


class ModelLimitsTest(unittest.TestCase):
    wan = next(m for m in models.CATALOG if m.id == "wan-2.2-t2v-q4").limits

    def test_wan_durations_land_on_4k_plus_1_grid_up_to_81_frames(self):
        counts = [timeline.frames_for_duration(s, self.wan.fps) for s in self.wan.durations_s]
        self.assertEqual(counts, [33, 49, 65, 81])

    def test_wan_spec_is_prompt_only_and_silent(self):
        spec = build_spec({"prompt": "p", "duration_s": 5}, self.wan)
        self.assertEqual((spec.num_frames, spec.fps, spec.audio), (81, 16, False))
        for name, payload in {
            "кадры": {"prompt": "p", "frames": [{"upload_id": "a"}]},
            "длиннее обученного": {"prompt": "p", "duration_s": 6},
        }.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                build_spec(payload, self.wan)


class DistributeTest(unittest.TestCase):
    def test_single_frame_is_start(self):
        self.assertEqual(timeline.distribute(1, 97), [0])

    def test_last_frame_lands_on_end(self):
        self.assertEqual(timeline.distribute(2, 97), [0, 96])
        self.assertEqual(timeline.distribute(3, 97), [0, 48, 96])
        self.assertEqual(timeline.distribute(4, 97), [0, 32, 64, 96])


class BuildSpecTest(unittest.TestCase):
    def test_text_only_defaults(self):
        spec = build_spec({"prompt": "  кот идёт  "})
        self.assertEqual(spec.prompt, "кот идёт")
        self.assertEqual((spec.width, spec.height, spec.num_frames), (512, 320, 97))
        self.assertEqual(spec.seed, -1)
        self.assertTrue(spec.audio)
        self.assertEqual(spec.keyframes, ())

    def test_frames_without_positions_are_distributed_in_order(self):
        spec = build_spec({"prompt": "p", "frames": [{"upload_id": "a"}, {"upload_id": "b"}, {"upload_id": "c"}]})
        self.assertEqual([(k.upload_id, k.frame_idx) for k in spec.keyframes], [("a", 0), ("b", 48), ("c", 96)])

    def test_explicit_positions_kept(self):
        spec = build_spec({"prompt": "p", "duration_s": 2, "frames": frames(0, 16, 48)})
        self.assertEqual([k.frame_idx for k in spec.keyframes], [0, 16, 48])

    def test_rejects(self):
        cases = {
            "пустой промпт": {"prompt": " "},
            "длительность": {"prompt": "p", "duration_s": 7},
            "пресет": {"prompt": "p", "preset": "8k"},
            "seed": {"prompt": "p", "seed": "abc"},
            "первый не на нуле": {"prompt": "p", "frames": frames(8, 40)},
            "порядок нарушен": {"prompt": "p", "frames": frames(0, 60, 40)},
            "слишком близко": {"prompt": "p", "frames": frames(0, 4)},
            "за концом": {"prompt": "p", "frames": frames(0, 97)},
            "позиции у части": {"prompt": "p", "frames": [{"upload_id": "a", "frame_idx": 0}, {"upload_id": "b"}]},
            "нет upload_id": {"prompt": "p", "frames": [{}]},
            "слишком много": {"prompt": "p", "duration_s": 2, "frames": [{"upload_id": "a"}] * 8},
            "не объект": [],
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                build_spec(payload)


if __name__ == "__main__":
    unittest.main()
