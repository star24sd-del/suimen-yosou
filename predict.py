#!/usr/bin/env python3
"""公式の番組表から3連単の買い目を算出し、サイト用JSONを書き出す。標準ライブラリだけで動く。

使い方:
  python3 predict.py --sample                                   # ダミーデータで動作確認
  python3 predict.py --date 2026-10-30                          # 番組表だけで予想(平均STは仮の値)
  python3 predict.py --date 2026-10-30 --results results.csv    # 過去成績のコース適性・風波・平均STも使う
結果は docs/data/YYYY-MM-DD.json に出力されます。番組表は data/ に保存され、あれば再利用します。
"""
from __future__ import annotations  # Python 3.9 でも動くようにする

import argparse
import itertools
import json
import math
import random
from datetime import date, datetime
from pathlib import Path

COURSE_W = 6.0   # コース適性(2連対率の差, 例: +0.15)の重み。バックテストで調整する
WEATHER_W = 1.0  # 会場ごとの風・波の影響(対数比)の重み。バックテストで調整する
DEFAULT_ST = 0.16  # 平均STが分からないときの仮の値
LANE_BONUS = [1.3, 0.5, 0.25, 0.0, -0.25, -0.5]  # 枠ごとの有利さ(1号艇が最も有利)
MARKS = ["◎", "○", "▲", "△"]
OUT_DIR = Path("docs/data")


def score(b: dict) -> float:
    """選手・機力・枠から強さを数値化する。ここを育てるのが予想の本体。"""
    st = b.get("st") or DEFAULT_ST
    base = b["win"] * 0.9 + b["motor"] * 0.04 - st * 9 + LANE_BONUS[b["lane"] - 1]
    return base + COURSE_W * b.get("course_adv", 0.0) + WEATHER_W * b.get("weather_adj", 0.0)


def predict(boats: list[dict], top_n: int = 5) -> dict:
    exps = [math.exp(score(b)) for b in boats]
    total = sum(exps)
    for b, e in zip(boats, exps):
        b["p"] = e / total
    ranked = sorted(boats, key=score, reverse=True)
    marks = {b["lane"]: MARKS[i] for i, b in enumerate(ranked[: len(MARKS)])}

    combos = []
    for a, b, c in itertools.permutations(boats, 3):
        p = a["p"] * (b["p"] / (1 - a["p"])) * (c["p"] / (1 - a["p"] - b["p"]))
        combos.append({"lanes": [a["lane"], b["lane"], c["lane"]], "p": round(p, 4)})
    combos.sort(key=lambda x: x["p"], reverse=True)
    return {"marks": marks, "bets": combos[:top_n]}


# ---------- データの読み込み ----------

def load_sample() -> list[dict]:
    rnd = random.Random(1)
    races = []
    for venue in ["桐生", "住之江"]:
        for n in (1, 2):
            boats = [
                {"lane": i + 1, "name": f"選手{i + 1}", "cls": "A2",
                 "win": round(rnd.uniform(3.6, 8.0), 2),
                 "motor": round(rnd.uniform(22, 52), 1),
                 "st": round(rnd.uniform(0.12, 0.20), 2)}
                for i in range(6)
            ]
            races.append({"venue": venue, "race": n, "boats": boats})
    return races


def load_official(d: date, data_dir: str = "data") -> list[dict]:
    from boatrace_data import fetch_one, parse_program, read_text

    path = fetch_one(d, "B", data_dir)  # 無ければ公式から取得する
    races = []
    for r in parse_program(read_text(path)):
        boats = [{
            "lane": b["lane"], "racer_id": b["racer_id"], "name": b["name"], "cls": b["cls"],
            "win": b["win"], "motor": b["motor"], "st": None,
        } for b in r["boats"]]
        races.append({"venue": r["venue"], "race": r["race"], "deadline": r["deadline"], "boats": boats})
    return races


# ---------- 出力 ----------

def run(races: list[dict], d: date, results_path: str | None = None) -> Path:
    enrich = None
    if results_path:
        from features import build_enricher  # 過去成績CSVからコース適性・風波・平均STを付与

        enrich = build_enricher(results_path)
    out = []
    for r in races:
        if len(r["boats"]) != 6:  # 欠場などで6艇未満のレースは今回は除外
            continue
        if enrich:
            enrich(r, d)
        for b in r["boats"]:
            if b.get("st") is None:
                b["st"] = DEFAULT_ST
        out.append({**r, **predict(r["boats"])})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{d.isoformat()}.json"
    payload = {"date": d.isoformat(), "generated_at": datetime.now().isoformat(timespec="seconds"), "races": out}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--data", default="data", help="番組表の保存先フォルダ")
    ap.add_argument("--results", metavar="results.csv", help="過去の競走成績CSV(boatrace_data.py build で作る)")
    a = ap.parse_args()

    if a.sample:
        print("wrote", run(load_sample(), date.today(), a.results))
    elif a.date:
        d = date.fromisoformat(a.date)
        print("wrote", run(load_official(d, a.data), d, a.results))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
