#!/usr/bin/env python3
"""過去の競走成績から、次の2つの特徴量を計算する。

1. 選手ごとのコース別成績(得意・不得意コース)
2. 会場ごとの風・波の影響(その会場で、風や波が強いときにどの枠が有利・不利になるか)

入力: results.csv(1行 = 1艇の1レース分)
  date(YYYY-MM-DD), venue, race, racer_id, course(進入コース 1-6),
  finish(着順 1-6。失格・欠場などは空欄か0), wind_speed(m/s), wave_cm(cm)

公式の競走成績データをこの形のCSVに変換する部分は、--inspect の結果を見て別途実装する。
"""
from __future__ import annotations  # Python 3.9 でも動くようにする

import csv
import math
from collections import defaultdict
from datetime import date, timedelta


def read_results(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                fin = int(r["finish"] or 0)
            except ValueError:
                fin = 0
            if not 1 <= fin <= 6:  # 失格・欠場などは集計から除く
                continue
            rows.append({
                "date": date.fromisoformat(r["date"]),
                "venue": r["venue"],
                "racer_id": r["racer_id"],
                "course": int(r["course"]),
                "finish": fin,
                "wind": float(r.get("wind_speed") or 0),
                "wave": float(r.get("wave_cm") or 0),
                "st": float(r["st"]) if r.get("st") else None,
            })
    rows.sort(key=lambda x: x["date"])
    return rows


class CourseStats:
    """選手 x コースの2連対率が、全体の平均よりどれだけ高い/低いかを返す。

    出走数が少ないコースは平均に引き寄せる(prior_n が大きいほど強く引き寄せる)。
    予想する日より前の直近 days 日だけを使うので、未来のデータは混ざらない。
    """

    def __init__(self, rows: list[dict], prior_n: int = 15, days: int = 365):
        self.prior_n, self.days = prior_n, days
        self.by_racer = defaultdict(list)
        tot = defaultdict(lambda: [0, 0])
        for r in rows:
            self.by_racer[r["racer_id"]].append(r)
            tot[r["course"]][0] += 1
            tot[r["course"]][1] += r["finish"] <= 2
        self.base = {c: (k / n if n else 1 / 3) for c, (n, k) in tot.items()}

    def advantage(self, racer_id, course: int, asof: date) -> float:
        lo = asof - timedelta(days=self.days)
        n = k = 0
        for r in self.by_racer.get(racer_id, []):
            if r["course"] == course and lo <= r["date"] < asof:
                n += 1
                k += r["finish"] <= 2
        base = self.base.get(course, 1 / 3)
        return (k + self.prior_n * base) / (n + self.prior_n) - base


def wind_bucket(w: float) -> int:
    return 0 if w < 3 else 1 if w < 5 else 2


def wave_bucket(c: float) -> int:
    return 0 if c < 3 else 1 if c < 6 else 2


class WeatherStats:
    """会場 x 風 x 波の区分ごとに、各コースの1着率が、その会場の平常時より
    どれだけ上下するかを対数の比で返す。データが少ない区分は平常時に近づける。"""

    def __init__(self, rows: list[dict], prior_n: int = 40):
        self.prior_n = prior_n
        self.cell = defaultdict(lambda: [0, 0])
        self.venue = defaultdict(lambda: [0, 0])
        for r in rows:
            key = (r["venue"], wind_bucket(r["wind"]), wave_bucket(r["wave"]), r["course"])
            self.cell[key][0] += 1
            self.cell[key][1] += r["finish"] == 1
            vk = (r["venue"], r["course"])
            self.venue[vk][0] += 1
            self.venue[vk][1] += r["finish"] == 1

    def adjust(self, venue: str, wind: float, wave: float, lane: int) -> float:
        n_v, k_v = self.venue.get((venue, lane), (0, 0))
        if n_v == 0:
            return 0.0
        base = max(k_v / n_v, 1e-3)
        n, k = self.cell.get((venue, wind_bucket(wind), wave_bucket(wave), lane), (0, 0))
        return math.log(((k + self.prior_n * base) / (n + self.prior_n)) / base)


class StStats:
    """選手の平均スタートタイミング(直近 days 日)。出走数が少ない選手は全体平均に寄せる。"""

    def __init__(self, rows: list[dict], prior_n: int = 10, days: int = 365):
        self.prior_n, self.days = prior_n, days
        self.by = defaultdict(list)
        vals = []
        for r in rows:
            if r["st"] is not None:
                self.by[r["racer_id"]].append((r["date"], r["st"]))
                vals.append(r["st"])
        self.mean = sum(vals) / len(vals) if vals else 0.16

    def avg(self, racer_id, asof: date) -> float:
        lo = asof - timedelta(days=self.days)
        xs = [s for d, s in self.by.get(racer_id, []) if lo <= d < asof]
        return (sum(xs) + self.prior_n * self.mean) / (len(xs) + self.prior_n)


def build_enricher(results_path: str):
    """各艇に course_adv(コース適性)と weather_adj(風・波の影響)を書き込む関数を返す。

    race には任意で wind_speed / wave_cm(直前情報の実測値)を入れておく。
    無い場合は風・波の影響は 0 として扱う。
    進入コースは枠番どおり(前付けなし)と仮定している。
    """
    rows = read_results(results_path)
    cs, ws, ss = CourseStats(rows), WeatherStats(rows), StStats(rows)

    def enrich(race: dict, asof: date) -> None:
        wind, wave = race.get("wind_speed"), race.get("wave_cm")
        for b in race["boats"]:
            b["course_adv"] = cs.advantage(b.get("racer_id"), b["lane"], asof)
            b["st"] = round(ss.avg(b.get("racer_id"), asof), 3)  # 番組表には平均STが無いので、過去成績から求める
            b["weather_adj"] = (
                ws.adjust(race["venue"], wind, wave, b["lane"])
                if wind is not None and wave is not None else 0.0
            )

    return enrich
