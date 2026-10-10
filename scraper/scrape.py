"""Скачивает расписание всех групп бакалавриата и сохраняет в data/.

  data/groups.json          список групп
  data/groups/<код>.json    расписание группы (шаблон на 2 недели)
  data/report.json          что не удалось разобрать (для отладки)
  data/status.json          когда данные менялись в последний раз

Защита от порчи данных: если страница не скачалась или разобралась хуже,
чем раньше (пар стало заметно меньше), остаётся предыдущий файл.
"""
import json, os, sys, time, datetime, urllib.request
from pathlib import Path
from esstu_parser import parse_index, parse_group, BASE

DATA = Path(__file__).resolve().parent.parent / "data"
UA = "Mozilla/5.0 (compatible; student-schedule-project)"
DELAY = float(os.environ.get("DELAY", "0.4"))   # пауза между запросами, сек


def fetch(url: str) -> bytes:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def main() -> int:
    index = parse_index(fetch(BASE + "raspisan.htm"))
    if len(index) < 50:                             # страница сломана — ничего не трогаем
        print(f"ОШИБКА: на главной найдено только {len(index)} групп, прерываю")
        return 1

    (DATA / "groups").mkdir(parents=True, exist_ok=True)
    changed, failed, kept_old = [], [], []
    unparsed, inferred, equip_seen = [], 0, {}

    for i, g in enumerate(index, 1):
        path = DATA / "groups" / f"{g['code']}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        try:
            new = parse_group(fetch(g["url"]), g["code"])
            new["name"] = g["name"]
        except Exception as e:                      # noqa: BLE001
            failed.append({"code": g["code"], "name": g["name"], "error": repr(e)})
            print(f"[{i}/{len(index)}] {g['code']} {g['name']}: ОШИБКА {e!r}")
            continue
        finally:
            time.sleep(DELAY)

        if old and len(old["lessons"]) >= 4 and len(new["lessons"]) < len(old["lessons"]) / 2:
            kept_old.append({"code": g["code"], "name": g["name"],
                             "old": len(old["lessons"]), "new": len(new["lessons"])})
            new = old                                # подозрительно — оставляем старое

        for l in new["lessons"]:
            if l.get("unparsed"):
                unparsed.append({"code": g["code"], "group": g["name"], "week": l["week"],
                                 "day": l["day"], "slot": l["slot"], "raw": l["raw"]})
            inferred += bool(l.get("subgroup_inferred"))
            if l.get("equip"):
                equip_seen[l["equip"]] = equip_seen.get(l["equip"], 0) + 1

        if old is None or dump(old) != dump(new):
            path.write_text(dump(new), encoding="utf-8")
            changed.append(g["code"])

    ok = len(index) - len(failed)
    report = {"groups_total": len(index), "groups_ok": ok, "failed": failed,
              "kept_old_data": kept_old, "unparsed_cells": unparsed,
              "subgroups_guessed_by_order": inferred, "equipment_codes": equip_seen}

    # пишем общие файлы только если что-то поменялось, чтобы не плодить пустые коммиты
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
