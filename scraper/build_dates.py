# raskladyvaet shablon "1-ya / 2-ya nedelya" iz data/groups/<kod>.json po konkretnym datam
# i skladyvaet v data/schedule/<kod>.json
#
# pravila:
#   - dni DO segodnyashnego zamorozheny: berem iz proshlogo fayla kak est, sayt ih ne menyaet
#   - segodnya i budushchie dni kazhdyy raz pereschityvayutsya iz svezhego shablona
#   - pri pervom zapuske proshlye dni vosstanavlivayutsya po tekushchemu shablonu (tracking_started)
#   - schitaem tolko semestr, v kotoryy segodnya popadaet (inache podstavili by staroe raspisanie)
#   - popravki iz overrides.json nakladyvayutsya na vse dni, vklyuchaya zamorozhennye
#   - esli bazovoe raspisanie budushchego dnya izmenilos - zapisyvaem datu v changes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
KEEP = ("slot", "kind", "title", "teacher", "subgroup", "subgroup_inferred", "option",
        "complex", "rooms", "building", "room", "remote", "equip", "raw")


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def today_local(cfg) -> dt.date:
    env = os.environ.get("TODAY")                   # dlya testov: TODAY=2026-10-10
    if env:
        return _d(env)
    shift = dt.timedelta(hours=cfg.get("utc_offset_hours", 8))
    return (dt.datetime.now(dt.timezone.utc) + shift).date()


def sem_of(cfg, day):
    for s in cfg["semesters"]:
        if _d(s["week1_monday"]) <= day <= _d(s["last_day"]):
            return s
    return cfg["semesters"][0]


def week_type(sem, day) -> int:
    idx = (day - _d(sem["week1_monday"])).days // 7
    return 1 + (idx + sem.get("week1_type", 1) - 1) % 2


def _slim(l):
    # v dnevnoy fayl idet tolko nuzhnoe; raw ostavlyaem tolko u slozhnyh kletok
    out = {k: l[k] for k in KEEP if l.get(k) not in (None, False, [])}
    if not l.get("complex"):
        out.pop("raw", None)
    return out


def _match(rule, code, name, day, wt, l):
    if rule.get("group", "*") not in ("*", code, name):
        return False
    if rule.get("from") and day < _d(rule["from"]):
        return False
    if rule.get("to") and day > _d(rule["to"]):
        return False
    for k, v in rule.get("match", {}).items():
        if k == "weekday":
            ok = day.isoweekday() == v
        elif k == "week_type":
            ok = wt == v
        elif k == "title_contains":
            ok = v in (l.get("title") or "")
        else:
            ok = l.get(k) == v
        if not ok:
            return False
    return True


def apply_overrides(lessons, day, wt, code, name, rules, hits):
    out = []
    for l in lessons:
        l, dropped = dict(l), False
        for i, r in enumerate(rules):
            if not _match(r, code, name, day, wt, l):
                continue
            hits[i] = hits.get(i, 0) + 1
            if r.get("drop"):
                dropped = True
                break
            l.update(r.get("set", {}))
        if not dropped:
            out.append({k: v for k, v in l.items() if v is not None})
    return out


def build_group(code, tmpl, old, cfg, rules, today, hits):
    name = tmpl.get("name") or tmpl.get("group")
    old = old or {}
    old_days, old_hash = old.get("days", {}), old.get("base_hash", {})
    days, base_hash, changes = {}, {}, {}

    # 1) proshlye dni - zamorozheny, tolko pereprimenyaem popravki (oni idempotentny)
    for ds, ls in old_days.items():
        day = _d(ds)
        if day < today:
            wt = week_type(sem_of(cfg, day), day)
            days[ds] = apply_overrides(ls, day, wt, code, name, rules, hits)
            base_hash[ds] = old_hash.get(ds)

    # 2) aktivnyy semestr: segodnya i dalshe (a pri pervom zapuske i proshlye) - iz shablona
    for sem in cfg["semesters"]:
        first, last = _d(sem["first_day"]), _d(sem["last_day"])
        if not first <= today <= last:
            continue
        day = first
        while day <= last:
            ds = day.isoformat()
            if day.isoweekday() != 7 and ds not in days and ds not in cfg.get("days_off", {}):
                wt = week_type(sem, day)
                base = sorted((_slim(l) for l in tmpl["lessons"]
                               if l["week"] == wt and l["day"] == day.isoweekday()),
                              key=lambda x: (x["slot"], x.get("subgroup") or 0, x.get("title") or ""))
                h = hashlib.sha1(_dump(base).encode()).hexdigest()[:8]
                days[ds] = apply_overrides(base, day, wt, code, name, rules, hits)
                base_hash[ds] = h
                if day >= today and ds in old_hash and old_hash[ds] != h:
                    changes[ds] = today.isoformat()     # shablon dlya etogo dnya pomenyalsya
            day += dt.timedelta(days=1)

    # starye pometki "izmeneno" derzhim, poka den ne ushel v proshloe
    for ds, when in old.get("changes", {}).items():
        if _d(ds) >= today and ds not in changes:
            changes[ds] = when

    return {"code": code, "group": name, "site_updated": tmpl.get("site_updated"),
            "tracking_started": old.get("tracking_started", today.isoformat()),
            "days": days, "base_hash": base_hash, "changes": changes}


def build_all(today=None, data_dir=DATA):
    cfg = _load(HERE / "calendar.json", None)
    rules = _load(HERE / "overrides.json", [])
    today = today or today_local(cfg)
    out_dir = Path(data_dir) / "schedule"
    out_dir.mkdir(parents=True, exist_ok=True)

    hits, written, n_days, n_changes = {}, [], 0, 0
    for path in sorted((Path(data_dir) / "groups").glob("*.json")):
        tmpl = json.loads(path.read_text(encoding="utf-8"))
        code = path.stem
        target = out_dir / f"{code}.json"
        old = _load(target, None)
        new = build_group(code, tmpl, old, cfg, rules, today, hits)
        n_days += len(new["days"])
        n_changes += sum(1 for ds in new["changes"] if new["changes"][ds] == today.isoformat())
        if old is None or _dump(old) != _dump(new):
            target.write_text(_dump(new), encoding="utf-8")
            written.append(code)

    unused = [i for i in range(len(rules)) if not hits.get(i)]
    return {"today": today.isoformat(), "groups_written": len(written), "written": written,
            "days_total": n_days, "changed_today": n_changes,
            "overrides_total": len(rules), "overrides_without_match": unused}


if __name__ == "__main__":
    s = build_all()
    print(f"Расписание по датам: файлов обновлено {s['groups_written']}, дней {s['days_total']}, "
          f"изменений сегодня {s['changed_today']}, правил поправок без совпадений {s['overrides_without_match']}")
