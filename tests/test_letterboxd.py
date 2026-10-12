import io
import unittest
import zipfile

from kinobot.letterboxd import parse_export, select_entries, LbEntry

WATCHLIST = "Date,Name,Year,Letterboxd URI\n2024-01-01,Dune: Part Two,2024,https://boxd.it/a\n2024-01-02,\"Perfect Days\",2023,https://boxd.it/b\n"
RATINGS = "Date,Name,Year,Letterboxd URI,Rating\n2024-01-01,Parasite,2019,https://boxd.it/c,5\n2024-01-02,Cats,2019,https://boxd.it/d,0.5\n"


class LetterboxdTest(unittest.TestCase):
    def test_zip_export(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("watchlist.csv", WATCHLIST)
            z.writestr("ratings.csv", RATINGS)
            z.writestr("likes/films.csv", "Date,Name,Year,Letterboxd URI\n2024-01-01,Heat,1995,x\n")
            z.writestr("profile.csv", "Username\nfoo\n")
        entries = parse_export("letterboxd-user.zip", buf.getvalue())
        kinds = sorted({e.kind for e in entries})
        self.assertEqual(kinds, ["liked", "ratings", "watchlist"])
        self.assertEqual(len(entries), 5)
        parasite = next(e for e in entries if e.name == "Parasite")
        self.assertEqual((parasite.year, parasite.rating), (2019, 5.0))

    def test_single_csv_by_name_and_header(self):
        e = parse_export("watchlist.csv", WATCHLIST.encode())
        self.assertTrue(all(x.kind == "watchlist" for x in e))
        e = parse_export("my_export.csv", RATINGS.encode())
        self.assertTrue(all(x.kind == "ratings" for x in e))
        e = parse_export("my_export.csv", WATCHLIST.encode())
        self.assertTrue(all(x.kind == "watched" for x in e))

    def test_bom_and_broken_rows(self):
        data = ("\ufeff" + RATINGS + "2024,,2020,x,3\n2024,NoRating,2020,x,\n").encode("utf-8")
        e = parse_export("ratings.csv", data)
        self.assertEqual([x.name for x in e], ["Parasite", "Cats"])

    def test_selection_limits(self):
        entries = [LbEntry("watchlist", f"w{i}", 2000) for i in range(300)]
        entries += [LbEntry("ratings", f"r{i}", 2000, 4.0) for i in range(300)]
        # по дизайну: до 120 из вишлиста и до 60 лучших оценок
        chosen, skipped = select_entries(entries, max_items=250)
        self.assertEqual(len(chosen), 180)
        self.assertEqual(skipped, 0)
        self.assertEqual(sum(1 for e in chosen if e.kind == "watchlist"), 120)
        chosen, skipped = select_entries(entries, max_items=100)
        self.assertEqual(len(chosen), 100)
        self.assertEqual(skipped, 80)


if __name__ == "__main__":
    unittest.main()
