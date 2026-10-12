import json
import unittest
from datetime import date, datetime
from pathlib import Path

from kinobot.adapters import cinemastar, kinomoscow
from kinobot.timeutil import UTC, tz_of

FIX = Path(__file__).parent / "fixtures"
TZ = tz_of("Asia/Yerevan")
NOW = datetime(2026, 10, 11, 19, 40, tzinfo=TZ)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class KinoMoscowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = kinomoscow.parse_home(read("kinomoscow_home.html"), NOW)

    def test_days_and_counts(self):
        days = sorted(d for _, d in self.res.covered)
        # 7 вкладок, но на последний день расписание ещё не опубликовано
        self.assertEqual(days[0], date(2026, 10, 11))
        self.assertEqual(days[-1], date(2026, 10, 16))
        self.assertEqual(len(days), 6)
        self.assertEqual(len(self.res.sessions), 156)

    def test_first_session_fields(self):
        s = self.res.sessions[0]
        self.assertEqual(s.title_raw, "Один в один")
        self.assertEqual(s.price, 3000)
        self.assertEqual(s.hall, "Синий зал")
        self.assertEqual(s.hall_class, "standard")
        self.assertEqual(s.url, "https://moscowcinema.com/ru/halls/52636")
        self.assertEqual(s.external_id, "52636")
        self.assertEqual(s.starts_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"), "2026-10-11 20:00")
        self.assertEqual(s.starts_at.astimezone(UTC).hour, 16)
        self.assertTrue(s.poster_url.endswith(".webp"))

    def test_vip_and_small_halls(self):
        classes = {s.hall_class for s in self.res.sessions}
        self.assertEqual(classes, {"standard", "vip", "small"})
        vip = [s for s in self.res.sessions if s.hall == "VIP"]
        self.assertTrue(all(s.price >= 3000 for s in vip))

    def test_special_price_days_and_cheap_mornings(self):
        monday = [s for s in self.res.sessions if s.starts_at.astimezone(TZ).date() == date(2026, 10, 12)]
        self.assertEqual(len(monday), 29)
        self.assertTrue(all(s.badge == "Специальная цена" for s in monday))
        self.assertEqual(min(s.price for s in monday), 1000)
        sunday = [s for s in self.res.sessions if s.starts_at.astimezone(TZ).date() == date(2026, 10, 11)]
        self.assertEqual(len(sunday), 7)
        self.assertTrue(all(s.badge is None for s in sunday))

    def test_unique_external_ids(self):
        ids = [s.external_id for s in self.res.sessions]
        self.assertEqual(len(ids), len(set(ids)))

    def test_promos(self):
        self.assertEqual([p.title for p in self.res.promos], ["Подарочная карта", "Дни Кино", "Моя кино карта"])
        self.assertTrue(all(p.url.startswith("https://moscowcinema.com/ru/promos/") for p in self.res.promos))

    def test_year_rollover(self):
        d = kinomoscow._resolve_date(2, 1, date(2026, 12, 30))
        self.assertEqual(d, date(2027, 1, 2))


class CinemaStarParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("cinemastar_home.html")
        cls.state = cinemastar.parse_home_state(cls.html)

    def test_state(self):
        st = self.state
        self.assertEqual(st.update_url, "https://cinemastar.am/ru/livewire/update")
        self.assertEqual([c[0] for c in st.cinemas], [12236, 13493, 16789])
        self.assertIn("Далма", st.cinemas[0][1])
        self.assertEqual(st.selected_cinema, 12236)
        self.assertEqual(st.selected_date, date(2026, 10, 11))
        self.assertEqual(len(st.dates), 11)
        self.assertTrue(st.csrf)
        self.assertEqual(json.loads(st.snapshot)["data"]["selectedCinema"], 12236)

    def test_sessions(self):
        ss = cinemastar.parse_screenings(self.html, date(2026, 10, 11), "cinemastar:12236")
        self.assertEqual(len(ss), 12)  # каждая строка есть и в мобильной, и в десктопной ссылке, но это одна строка
        first = ss[0]
        self.assertEqual(first.title_raw, "Сумерки. Сага. Новолуние (RUS)")
        self.assertEqual(first.price, 3000)
        self.assertEqual(first.fmt, "2D")
        self.assertEqual(first.hall, "Зал 4")
        self.assertEqual(first.language_label, "на русском")
        self.assertEqual(first.external_id, "180697")
        self.assertEqual(first.url, "https://pay.cinemastar.am/180697?cinema=dalma&lang=ru")
        self.assertEqual(first.starts_at.astimezone(TZ).strftime("%H:%M"), "19:35")
        self.assertTrue(first.poster_url.startswith("https://cdn.center.am/"))
        vip = [s for s in ss if s.hall_class == "vip"]
        self.assertEqual(len(vip), 2)
        self.assertTrue(all(s.price == 6000 for s in vip))

    def test_after_midnight_session_belongs_to_next_calendar_day(self):
        row = ('<table><tr><th><div>00:30</div><a href="https://cinemastar.am/ru/movie/x"><img class="lg:hidden" '
               'src="p.png" alt="x"></a><h2>Ночной (ENG)</h2></th><td>ЗАЛ 1</td><td><span>2d</span></td>'
               '<td>3000 др.</td><td><a href="https://pay.cinemastar.am/1?cinema=dalma">buy</a></td></tr></table>')
        s = cinemastar.parse_screenings(row, date(2026, 10, 11), "cinemastar:1")[0]
        self.assertEqual(s.starts_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"), "2026-10-12 00:30")
        self.assertEqual(s.language_label, "на английском")


class FakeResponse:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def fragment(title: str, ext_id: str, hhmm: str = "18:00", price: str = "3000") -> str:
    return (f'<div><table><tr><th><div>{hhmm}</div><a href="https://cinemastar.am/ru/movie/{ext_id}">'
            f'<img class="lg:hidden" src="p{ext_id}.png" alt="{title}"></a><h2>{title}</h2></th>'
            f'<td>ЗАЛ 1</td><td><span>2d</span></td><td>{price} др.</td>'
            f'<td><a href="https://pay.cinemastar.am/{ext_id}?cinema=x">buy</a></td></tr></table></div>')


class FakeLivewireHttp:
    """Имитирует поведение страницы: GET главной и POST /livewire/update с вызовами методов."""

    def __init__(self, home_html: str, fail_cinema: int | None = None):
        self.home_html = home_html
        self.cinema = 12236
        self.day = "2026-10-11"
        self.posts = []
        self.fail_cinema = fail_cinema

    def get(self, url, timeout=None):
        self.cinema, self.day = 12236, "2026-10-11"
        return FakeResponse(text=self.home_html)

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        comp = json["components"][0]
        for call in comp["calls"]:
            if call["method"] == "changeCinema":
                self.cinema = int(call["params"][0])
            elif call["method"] == "$set":
                self.day = call["params"][1]
        if self.fail_cinema == self.cinema:
            raise RuntimeError("boom")
        snapshot = {"data": {"selectedCinema": self.cinema, "selectedDate": f"{self.day} 00:00:00"}, "memo": {}}
        html = fragment(f"Film {self.cinema} {self.day}", f"{self.cinema}{self.day.replace('-', '')}")
        return FakeResponse(data={"components": [{"snapshot": __import__("json").dumps(snapshot),
                                                  "effects": {"html": html}}]})


class CinemaStarLivewireTest(unittest.TestCase):
    def test_fetch_all_cinemas_and_days(self):
        http = FakeLivewireHttp(read("cinemastar_home.html"))
        adapter = cinemastar.CinemaStarAdapter(
            days_ahead=3, client=cinemastar.CinemaStarClient(http=http, delay=0))
        res = adapter.fetch(NOW)
        self.assertEqual(res.errors, [])
        self.assertEqual(len(res.cinemas), 3)
        self.assertEqual(len(res.covered), 9)  # 3 филиала x 3 дня
        # 12 сеансов из исходной страницы + 8 вызовов Livewire по одному сеансу
        self.assertEqual(len(res.sessions), 12 + 8)
        self.assertEqual(len(http.posts), 8)
        # первый вызов для второго филиала: смена филиала и дата в одном запросе
        methods = [[c["method"] for c in p["components"][0]["calls"]] for p in http.posts]
        self.assertIn(["changeCinema", "$set"], methods)
        self.assertEqual(http.posts[0]["_token"], cinemastar.parse_home_state(read("cinemastar_home.html")).csrf)
        keys = {s.cinema_key for s in res.sessions}
        self.assertEqual(keys, {"cinemastar:12236", "cinemastar:13493", "cinemastar:16789"})

    def test_failure_in_one_cinema_keeps_the_rest(self):
        http = FakeLivewireHttp(read("cinemastar_home.html"), fail_cinema=13493)
        adapter = cinemastar.CinemaStarAdapter(
            days_ahead=2, client=cinemastar.CinemaStarClient(http=http, delay=0))
        res = adapter.fetch(NOW)
        self.assertEqual(len(res.errors), 1)
        self.assertIn("13493", res.errors[0])
        self.assertIn(("cinemastar:12236", date(2026, 10, 11)), res.covered)
        self.assertIn(("cinemastar:16789", date(2026, 10, 12)), res.covered)
        self.assertNotIn(("cinemastar:13493", date(2026, 10, 11)), res.covered)


if __name__ == "__main__":
    unittest.main()
