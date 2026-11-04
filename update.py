#!/usr/bin/env python3
"""毎日の更新: 公式データの取得 -> 予想の計算 -> 収支の集計 -> サイト用JSONの書き出し。標準ライブラリだけで動く。

使い方:
  python3 update.py                          # 通常の更新(昨日の成績を取り込み、昨日と今日を作り直す)
  python3 update.py --backfill 2026-09-01    # 指定日から昨日までを取得して作る(初回向け)
  python3 update.py --backfill 2026-10-30 --no-fetch   # 取得せず、data/ にあるファイルだけで作る
出力: docs/data/YYYY-MM-DD.json と docs/data/index.json
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import boatrace_data as bd
import predict as pr
from features import build_enricher

DATA = "data"
OUT = Path("docs/data")
RESULTS = "data/results.csv"
JST = timezone(timedelta(hours=9))
PLAN_NAMES = ("本線", "押さえ")


# ---------- 取得 ----------

def valid_lzh(path: Path) -> bool:
    """LZHの先頭(方式名 -lh?-)があるか。エラーページなどを保存しないための確認。"""
    try:
        head = path.read_bytes()[:8]
    except OSError:
        return False
    return len(head) >= 7 and head[2:5] == b"-lh" and head[6:7] == b"-"


def try_fetch(d: date, kind: str, sleep: float) -> bool:
    p = Path(DATA) / bd.file_name(d, kind)
    if p.exists() and valid_lzh(p):
        return True
    try:
        bd.fetch_one(d, kind, DATA)
    except FileNotFoundError:
        print("なし  ", p.name)
        return False
    if not valid_lzh(p):
        p.unlink(missing_ok=True)
        print("不正  ", p.name)
        return False
    print("取得  ", p.name)
    time.sleep(sleep)  # 公式サーバーに負荷をかけないよう間隔をあける
    return True


# ---------- 1日分の作成 ----------

def combos_of(axis: int, opp: list[int]) -> list[tuple[int, int, int]]:
    return [(axis, x, y) for x in opp for y in opp if x != y]


def build_day(d: date, enrich) -> dict | None:
    bpath = Path(DATA) / bd.file_name(d, "B")
    if not bpath.exists():
        return None
    prog = bd.parse_program(bd.read_text(bpath))
    kpath = Path(DATA) / bd.file_name(d, "K")
    results = {}
    if kpath.exists():
        results = {(r["venue"], r["race"]): r for r in bd.parse_results(bd.read_text(kpath))}

    races = []
    for p in sorted(prog, key=lambda r: (r["venue_code"], r["race"])):
        if len(p["boats"]) != 6:
            continue
        boats = [{"lane": b["lane"], "racer_id": b["racer_id"], "name": b["name"], "cls": b["cls"],
                  "win": b["win"], "motor": b["motor"], "st": None} for b in p["boats"]]
        race = {"venue": p["venue"], "race": p["race"], "boats": boats}
        if enrich:
            enrich(race, d)  # コース適性と平均STを過去成績から付与(当日以降のデータは使わない)
        for b in boats:
            if b.get("st") is None:
                b["st"] = pr.DEFAULT_ST
        pred = pr.predict(boats)
        marks = {int(k): v for k, v in pred["marks"].items()}
        by_mark = {v: k for k, v in marks.items()}
        pm = {b["lane"]: b["p"] for b in boats}

        plans = []
        if all(m in by_mark for m in "◎○▲△"):
            plans = [
                {"name": "本線", "axis": by_mark["◎"], "opp": [by_mark["○"], by_mark["▲"], by_mark["△"]]},
                {"name": "押さえ", "axis": by_mark["○"], "opp": [by_mark["◎"], by_mark["▲"]]},
            ]
        for pl in plans:
            cs = combos_of(pl["axis"], pl["opp"])
            pl["combos"] = ["-".join(map(str, c)) for c in cs]
            pl["prob"] = round(sum(pm[a] * pm[b] / (1 - pm[a]) * pm[c] / (1 - pm[a] - pm[b]) for a, b, c in cs), 4)

        row = {
            "venue": p["venue"], "race": p["race"], "title": p["title"], "deadline": p["deadline"],
            "boats": [{"lane": b["lane"], "name": b["name"], "cls": b["cls"], "win": b["win"],
                       "motor": b["motor"], "p": round(b["p"], 4), "mark": marks.get(b["lane"], "")} for b in boats],
            "top": [{"l": c["lanes"], "p": c["p"]} for c in pred["bets"]],
            "plans": plans,
        }
        r = results.get((p["venue"], p["race"]))
        if r:
            full = len(r["boats"]) == 6  # 欠場などで6艇そろわないレースは収支に入れない
            row["result"] = {
                "weather": r["weather"], "wind_dir": r["wind_dir"], "wind": r["wind_speed"], "wave": r["wave_cm"],
                "trifecta": r["trifecta"], "payout": r["payout"], "full": full,
                "order": [{"finish": b["finish_raw"], "lane": b["lane"], "name": b["name"], "ex": b["ex"],
                           "course": b["course"], "st": b["st"]} for b in r["boats"]],
            }
            for pl in plans:
                if full and r["trifecta"]:
                    pl["stake"] = 100 * len(pl["combos"])
                    pl["hit"] = r["trifecta"] in pl["combos"]
                    pl["return"] = r["payout"] if pl["hit"] else 0
                else:
                    pl["stake"] = pl["return"] = None
        races.append(row)

    return {"date": d.isoformat(), "generated_at": datetime.now(JST).isoformat(timespec="minutes"), "races": races}


def write_day(d: date, enrich) -> bool:
    day = build_day(d, enrich)
    if day is None:
        return False
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{d.isoformat()}.json").write_text(json.dumps(day, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("作成  ", f"{d.isoformat()}.json", f"{len(day['races'])}レース")
    return True


# ---------- 一覧(index.json) ----------

def write_index() -> None:
    dates, stats = [], {}
    for f in sorted(OUT.glob("????-??-??.json")):
        day = json.loads(f.read_text(encoding="utf-8"))
        dates.append(day["date"])
        agg = {n: [0, 0, 0, 0] for n in PLAN_NAMES}  # レース数, 投資, 払戻, 的中
        for r in day["races"]:
            for pl in r["plans"]:
                if pl.get("stake") is not None:
                    a = agg[pl["name"]]
                    a[0] += 1
                    a[1] += pl["stake"]
                    a[2] += pl["return"]
                    a[3] += 1 if pl["hit"] else 0
        if agg["本線"][0] > 0:
            stats[day["date"]] = agg
    index = {"dates": dates, "stats": stats, "updated": datetime.now(JST).isoformat(timespec="minutes")}
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("作成   index.json", f"{len(dates)}日分")


# ---------- メイン ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", metavar="YYYY-MM-DD", help="この日から昨日までを取得して作る")
    ap.add_argument("--no-fetch", action="store_true", help="取得せず data/ にあるファイルだけを使う")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args()

    today = datetime.now(JST).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    start = date.fromisoformat(a.backfill) if a.backfill else yesterday
    days = [start + timedelta(days=i) for i in range((yesterday - start).days + 1)]

    if not a.no_fetch:
        for d in days:
            try_fetch(d, "B", a.sleep)
            try_fetch(d, "K", a.sleep)
        try_fetch(today, "B", a.sleep)  # 今日の番組表(結果はまだ無い)
        try_fetch(tomorrow, "B", a.sleep)  # 明日の番組表(前日の夕方に公開。まだ無ければ飛ばす)

    Path(DATA).mkdir(exist_ok=True)
    bd.cmd_build(SimpleNamespace(data=DATA, out=RESULTS))  # 取得済みの成績を results.csv にまとめる
    enrich = build_enricher(RESULTS) if Path(RESULTS).exists() and Path(RESULTS).stat().st_size > 0 else None

    for d in days + [today, tomorrow]:
        write_day(d, enrich)
    write_index()


if __name__ == "__main__":
    main()
