# skachivaet raspisanie vseh grupp bakalavriata i skladyvaet v data/
#
#   data/groups.json          spisok grupp
#   data/groups/<kod>.json    raspisanie gruppy (shablon na 2 nedeli)
#   data/report.json          chto ne udalos razobrat (dlya otladki)
#   data/status.json          kogda dannye menyalis v posledniy raz
#
# zashchita ot porchi dannyh: esli stranitsa ne skachalas ili razobralas
# zametno huzhe chem ranshe (par stalo namnogo menshe) - ostavlyaem staryy fayl
import json, os, re, sys, time, datetime, traceback, urllib.request
from pathlib import Path
from esstu_parser import parse_index, parse_group, BASE
from build_dates import build_all

DATA = Path(__file__).resolve().parent.parent / "data"
UA = "Mozilla/5.0 (compatible; student-schedule-project)"
DELAY = float(os.environ.get("DELAY", "0.4"))   # pauza mezhdu zaprosami, sek


def fetch(url: str):
    # vozvrashchaet (telo stranitsy, zagolovok Last-Modified)
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(), r.headers.get("Last-Modified")
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep((5, 15, 45, 0)[attempt])      # chem dalshe tem dolshe zhdem
    raise last


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def audit(lessons):
    # ishchem podozritelnye pary: razbor ne upal, no rezultat mozhet byt krivym
    # vozvrashchaet spisok (prichina, ishodnyy tekst kletki)
    out = []
    for l in lessons:
        if l.get("complex"):
            out.append(("complex", l["raw"]))           # yazykovaya kasha - ne razobrali, tolko raw
            continue
        title = l.get("title") or ""
        exempt = re.match(r"(Военная кафедра|Физическая культура|ЭК по ФКС)", title)
        if l["kind"] != "other" and not l.get("teacher") and not exempt:
            out.append(("no_teacher", l["raw"]))
        if len(title) < 3:
            out.append(("no_title", l["raw"]))
        elif re.search(r"п/г|(?<!\w)а\.", title) or re.fullmatch(r"[\d\W]+", title):
            out.append(("dirty_title", l["raw"]))       # v nazvanie popal musor
        if l["kind"] == "other":
            out.append(("kind_other", l["raw"]))        # net lek/lab/pr - smotrim kakie byvayut
        if l.get("subgroup_inferred"):
            out.append(("guessed_subgroups", l["raw"])) # podgruppy ugadany po poryadku
        if l.get("option"):
            out.append(("elective_options", l["raw"]))  # dva raznyh predmeta po vyboru
        if l.get("room") and not l.get("remote") and not re.fullmatch(r"[\dА-Яа-яA-Za-z.\-]+", l["room"]):
            out.append(("odd_room", l["raw"]))
    return out


def main() -> int:
    if "--build-only" in sys.argv:                  # tolko pereschet po datam, sayt ne trogaem
        st = build_all(rebase="--rebase" in sys.argv)
        print(f"Расписание по датам: файлов обновлено {st['groups_written']}, дней {st['days_total']}, "
              f"правил поправок без совпадений {st['overrides_without_match']}")
        return 0
    try:
        index = parse_index(fetch(BASE + "raspisan.htm")[0])
    except Exception as e:                          # noqa: BLE001
        print(f"Не удалось открыть главную страницу портала: {e!r}")
        print("Данные в репозитории не тронуты, можно просто запустить позже.")
        return 1
    if len(index) < 50:                             # stranitsa slomana - nichego ne trogaem
        print(f"ОШИБКА: на главной найдено только {len(index)} групп, прерываю")
        return 1

    (DATA / "groups").mkdir(parents=True, exist_ok=True)
    changed, failed, kept_old = [], [], []
    unparsed, inferred, equip_seen = [], 0, {}
    equip_examples = {}                             # kod oborudovaniya -> neskolko primerov kletok
    streak, ok_count = 0, 0                         # oshibok podryad / uspeshno razobrannyh grupp
    sus = {}                                        # prichina -> {tekst kletki: [skolko grupp, primer]}

    for i, g in enumerate(index, 1):
        path = DATA / "groups" / f"{g['code']}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        try:
            body, last_mod = fetch(g["url"])
            new = parse_group(body, g["code"], last_mod)
            new["name"] = g["name"]
        except Exception as e:                      # noqa: BLE001
            streak += 1
            failed.append({"code": g["code"], "name": g["name"], "error": repr(e)})
            print(f"[{i}/{len(index)}] {g['code']} {g['name']}: ОШИБКА {e!r}")
            if len(failed) <= 3:
                traceback.print_exc()                # podrobnosti tolko dlya pervyh oshibok
            if streak >= 8:                          # sayt lezhit - ne dolbim ego zrya
                print("8 ошибок подряд, останавливаюсь: сайт, похоже, недоступен.")
                break
            continue
        finally:
            time.sleep(DELAY)
        streak = 0
        ok_count += 1

        if old and len(old["lessons"]) >= 4 and len(new["lessons"]) < len(old["lessons"]) / 2:
            kept_old.append({"code": g["code"], "name": g["name"],
                             "old": len(old["lessons"]), "new": len(new["lessons"])})
            new = old                                # podozritelno - ostavlyaem staroe

        for l in new["lessons"]:
            if l.get("unparsed"):
                unparsed.append({"code": g["code"], "group": g["name"], "week": l["week"],
                                 "day": l["day"], "slot": l["slot"], "raw": l["raw"]})
            inferred += bool(l.get("subgroup_inferred"))
            if l.get("equip"):
                equip_seen[l["equip"]] = equip_seen.get(l["equip"], 0) + 1
                ex = equip_examples.setdefault(l["equip"], [])
                if len(ex) < 3 and l["raw"] not in ex:
                    ex.append(l["raw"])

        for why, raw in set(audit(new["lessons"])):
            rec = sus.setdefault(why, {}).setdefault(raw, [0, g["name"]])
            rec[0] += 1

        if old is None or dump(old) != dump(new):
            path.write_text(dump(new), encoding="utf-8")
            changed.append(g["code"])

    ok = ok_count
    if ok < 0.8 * len(index):                       # progon neudachnyy - report i status ne perezapisyvaem
        print(f"Разобрано только {ok} из {len(index)} групп, общий отчёт не обновляю.")
        return 1
    build_stats = build_all(rebase="--rebase" in sys.argv)   # rasklad po datam, poka dannye svezhie
    changed.extend(f"schedule/{c}" for c in build_stats.pop("written"))
    build_stats.pop("today")                        # data menyaetsya kazhdyy den - v otchet ne kladem
    report = {"groups_total": len(index), "groups_ok": ok, "failed": failed, "not_checked": len(index) - ok - len(failed),
              "kept_old_data": kept_old, "unparsed_cells": unparsed,
              "subgroups_guessed_by_order": inferred, "equipment_codes": equip_seen, "equipment_examples": equip_examples,
              "suspicious": {why: {"distinct": len(d), "top": [
                  {"raw": raw, "groups": v[0], "example": v[1]}
                  for raw, v in sorted(d.items(), key=lambda kv: (-kv[1][0], kv[0]))[:40]]}
                  for why, d in sorted(sus.items())}}

    report["build"] = build_stats
    # obschie fayly pishem tolko esli chto-to pomenyalos, chtoby ne plodit pustye kommity
    for name, obj in (("groups.json", index), ("report.json", report)):
        p = DATA / name
        if not p.exists() or p.read_text(encoding="utf-8") != dump(obj):
            p.write_text(dump(obj), encoding="utf-8"); changed.append(name)
    if changed or not (DATA / "status.json").exists():
        (DATA / "status.json").write_text(dump({
            "changed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "changed_files": len(changed)}), encoding="utf-8")

    print(f"Готово: групп {ok}/{len(index)}, изменилось файлов {len(changed)}, "
          f"нераспознанных клеток {len(unparsed)}, ошибок {len(failed)}")
    return 0 if ok >= 0.8 * len(index) else 1


if __name__ == "__main__":
    sys.exit(main())
