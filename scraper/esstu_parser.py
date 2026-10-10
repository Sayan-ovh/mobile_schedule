# parser raspisaniya portal.esstu.ru (bakalavriat)
#
# parse_index(html_bytes)  -> spisok grupp [{code, name, column, url}]
# parse_group(html_bytes)  -> raspisanie odnoy gruppy (shablon na 2 nedeli)
#
# stranitsy na sayte v windows-1251, poetomu na vhod idut syrye bayty.
# v kazhdoy pare ostavlyaem raw (ishodnyy tekst): esli razbor oshibsya,
# mozhno posmotret chto tam bylo i ne poteryat dannye
import re
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

try:                       # html5lib stroit derevo kak brauzer (s <tbody>)
    import html5lib        # noqa: F401
    _PARSER = "html5lib"
except ImportError:
    _PARSER = "html.parser"

BASE = "https://portal.esstu.ru/bakalavriat/"
KINDS = {"лек": "lecture", "лаб": "lab", "пр": "practice"}
EQUIP = {"д/к": "display", "и/д": "board", "л": "л"}   # kod "el" (kirillitsa) - chto eto znachit poka ne znayu, ostavil kak est
EMPTY = re.compile(r"^[\s_\-–—]*$")

# primery: a.25-214 d/k   |   a.9-Sport1   |   a.dist./ob.   |   a.25-115 l
ROOM = re.compile(r"а\.\s*(\S+)(?:\s+(д/к|и/д|л)(?=\s|$))?")
# FAMILIYA I.O. (i neobyazatelnyy " -" posle nee)
TEACHER = re.compile(r"([А-ЯЁ]{2,}(?:-[А-ЯЁ]{2,})?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)\s*-?\s*$")
SUBGROUP = re.compile(r"\s*-\s*(\d)\s*п/г\s*$")


def _text(html: bytes) -> BeautifulSoup:
    return BeautifulSoup(html.decode("cp1251", errors="replace"), _PARSER)


def _rows(table):
    # v syrom html s servera <tbody> net, v sohranennom brauzerom - est
    body = table.find("tbody") or table
    return body.find_all("tr", recursive=False)


def _site_date(last_modified):
    # data iz zagolovka Last-Modified (na sayte ee risuet skript na stranitse)
    if not last_modified:
        return None
    try:
        d = parsedate_to_datetime(last_modified).astimezone(timezone(timedelta(hours=8)))
    except (TypeError, ValueError):
        return None
    return d.strftime("%d.%m.%Y")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- spisok grupp
def parse_index(html: bytes):
    soup = _text(html)
    groups, seen = [], set()
    for tr in soup.find("table").find_all("tr"):
        for col, td in enumerate(tr.find_all("td", recursive=False), 1):
            for a in td.find_all("a", href=True):
                name = _clean(a.get_text(" ", strip=True))
                code = a["href"].rsplit("/", 1)[-1].replace(".htm", "")
                if name and code not in seen:
                    seen.add(code)
                    groups.append({"code": code, "name": name, "column": col,
                                   "url": f"{BASE}{code}.htm"})
    return groups


# ---------------------------------------------------------------- odna kletka
def _room(token: str, equip: str | None):
    if token.lower().startswith("дист"):
        return {"building": None, "room": "дист.", "remote": True,
                "equip": EQUIP.get(equip)}
    b, _, r = token.partition("-")
    return {"building": b if r else None, "room": r or b, "remote": False,
            "equip": EQUIP.get(equip)}


def parse_cell(text: str):
    # vozvrashchaet spisok par iz odnoy kletki (obychno 1, pri podgruppah 2)
    text = _clean(text)
    if EMPTY.match(text):
        return []

    kind = "other"
    m = re.match(r"^(лек|лаб|пр)\.\s*(.*)$", text)
    body = text
    if m:
        kind, body = KINDS[m.group(1)], m.group(2)

    out, pos, prev_title = [], 0, None
    rooms = list(ROOM.finditer(body))
    for idx, rm in enumerate(rooms):
        seg = body[pos:rm.start()].strip()
        pos = rm.end()
        teacher = None
        tm = TEACHER.search(seg)
        if tm:
            teacher = re.sub(r"\s+", " ", tm.group(1))
            seg = seg[:tm.start()].strip()
        sub, explicit = None, False
        sm = SUBGROUP.search(seg)
        if sm:
            sub, explicit, seg = int(sm.group(1)), True, seg[:sm.start()].strip()
        title = seg.rstrip(" -") or prev_title
        prev_title = title
        out.append({"kind": kind, "title": title, "teacher": teacher,
                    "subgroup": sub, "subgroup_explicit": explicit,
                    **_room(rm.group(1), rm.group(2)), "raw": text})

    if not out:                      # format ne raspoznan - ne teryaem paru, kladem kak est
        return [{"kind": kind, "title": body, "teacher": None, "subgroup": None,
                 "subgroup_explicit": False, "building": None, "room": None,
                 "remote": False, "equip": None, "raw": text, "unparsed": True}]

    # dve podgruppy bez pometok: sleva napravo = 1-ya, 2-ya (ugadyvaem po poryadku)
    # vnimanie: poryadok ne vsegda verny (u 564-2 putalsya), nuzhny ruchnye popravki
    if len(out) == 2 and not any(o["subgroup_explicit"] for o in out):
        out[0]["subgroup"], out[1]["subgroup"] = 1, 2
        out[0]["subgroup_inferred"] = out[1]["subgroup_inferred"] = True
    return out


# ---------------------------------------------------------------- stranitsa gruppy
def parse_group(html: bytes, code: str | None = None, last_modified: str | None = None):
    soup = _text(html)
    page = _clean(soup.get_text(" ", strip=True))
    gm = re.search(r"учебной группы:\s*(.+?)\s*Пары", page)
    um = re.search(r"Расписание обновлено\s*(\d\d\.\d\d\.\d{4})", page)

    rows = _rows(soup.find("table"))
    times = [_clean(td.get_text()) for td in rows[1].find_all("td", recursive=False)[1:7]]
    day_rows = rows[2:14]            # 6 dney 1-y nedeli + 6 dney 2-y nedeli

    lessons = []
    for i, tr in enumerate(day_rows):
        week, day = i // 6 + 1, i % 6 + 1
        cells = tr.find_all("td", recursive=False)
        for slot in range(1, 7):
            for les in parse_cell(cells[slot].get_text(" ", strip=True)):
                lessons.append({"week": week, "day": day, "slot": slot, **les})

    return {"code": code, "group": gm.group(1) if gm else None,
            "site_updated": um.group(1) if um else _site_date(last_modified),
            "last_modified": last_modified,
            "slot_times": times, "lessons": lessons}
