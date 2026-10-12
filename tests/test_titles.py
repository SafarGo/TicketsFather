import unittest

from kinobot.titles import clean_title, detect_language, normalize_title, similarity


class TitlesTest(unittest.TestCase):
    def test_strip_language_tags(self):
        self.assertEqual(clean_title("Диггер (RUS)"), "Диггер")
        self.assertEqual(clean_title("Сумерки. Сага. Новолуние (RUS) "), "Сумерки. Сага. Новолуние")
        self.assertEqual(clean_title("Гордость и предубеждение ENG"), "Гордость и предубеждение")
        self.assertEqual(clean_title("Verity (ENG) (2D)"), "Verity")

    def test_normalize_is_stable_across_sources(self):
        self.assertEqual(normalize_title("Диггер (RUS)"), normalize_title("Диггер"))
        self.assertEqual(normalize_title("Ёлки!"), normalize_title("елки"))
        self.assertEqual(normalize_title("Де Голль. Часть 1. Сопротивление (RUS)"),
                         normalize_title("Де Голль: Часть 1 — Сопротивление"))

    def test_language_detection(self):
        self.assertEqual(detect_language("Диггер (RUS)"), "ru")
        self.assertEqual(detect_language("Один в один (ARM)"), "hy")
        self.assertEqual(detect_language("Гордость и предубеждение ENG"), "en")
        self.assertIsNone(detect_language("Диггер"))

    def test_similarity(self):
        self.assertEqual(similarity("Диггер (RUS)", "диггер"), 1.0)
        self.assertGreater(similarity("The Odyssey", "Odyssey"), 0.7)
        self.assertLess(similarity("Один в один", "Pulp Fiction"), 0.5)
        self.assertEqual(similarity("", "x"), 0.0)


if __name__ == "__main__":
    unittest.main()
