#!/usr/bin/env python3
"""公式サイト(mbrace.or.jp)の番組表(B)・競走成績(K)の取得と読み取り。標準ライブラリだけで動く。

使い方:
  # 過去の競走成績を日付の範囲で取得(1日1ファイル。1秒ずつ間隔をあけます)
  python3 boatrace_data.py fetch --start 2026-04-01 --end 2026-10-30 --out data
  # 取得した成績を results.csv にまとめる(features.py が読む形式)
  python3 boatrace_data.py build --data data --out results.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from lzh import read_lzh

BASE = "https://www1.mbrace.or.jp/od2"
VENUES = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖",
    7: "蒲郡", 8: "常滑", 9: "津", 10: "三国", 11: "びわこ", 12: "住之江",
    13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島", 18: "徳山",
    19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}


# ---------- 取得 ----------

def file_name(d: date, kind: str) -> str:
    return f"{kind.lower()}{d:%y%m%d}.lzh"


def fetch_one(d: date, kind: str, data_dir: str) -> Path:
    """ファイルが無ければ公式から取得して保存する。取得には curl を使う(Mac/Windows 10以降に標準で入っている)。"""
    name = file_name(d, kind)
    dest = Path(data_dir) / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/{kind}/{d:%Y%m}/{name}"
    tmp = dest.with_suffix(".part")
    r = subprocess.run(["curl", "-sSf", "--max-time", "60", "-o", str(tmp), url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise FileNotFoundError(f"{url} を取得できませんでした: {r.stderr.strip() or r.returncode}")
    tmp.rename(dest)
    return dest


def read_text(path) -> str:
    parts = []
    for _, data in read_lzh(str(path)):
        parts.append(data.decode("cp932", errors="replace").replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(parts)


# ---------- 番組表(B) ----------

_BOAT_B = re.compile(
    r"^([1-6]) (\d{4})(.{4})(\d{2})(.{2})(\d{2})(A1|A2|B1|B2)\s*"
    r"(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+)\s+(\d+\.\d+)\s+(\d+)\s+(\d+\.\d+)"
)


def parse_program(text: str) -> list[dict]:
    """番組表のテキストを、レースごとの出走表にする。"""
    races, venue, cur = [], None, None
    for raw in text.split("\n"):
        m = re.match(r"^(\d\d)BBGN", raw)
        if m:
            venue, cur = int(m.group(1)), None
            continue
        if re.match(r"^\d\dBEND", raw):
            venue, cur = None, None
            continue
        if venue is None:
            continue
        line = unicodedata.normalize("NFKC", raw)
        h = re.match(r"^\s*(\d{1,2})R\s+(.*?)\s+H(\d+)m", line)
        if h:
            dl = re.search(r"締切予定(\d{1,2}):(\d{2})", line)
            cur = {
                "venue_code": venue, "venue": VENUES.get(venue, str(venue)),
                "race": int(h.group(1)), "title": h.group(2).strip(),
                "distance": int(h.group(3)),
                "deadline": f"{int(dl.group(1)):02d}:{dl.group(2)}" if dl else None,
                "boats": [],
            }
            races.append(cur)
            continue
        b = _BOAT_B.match(raw)
        if b and cur is not None:
            g = b.groups()
            cur["boats"].append({
                "lane": int(g[0]), "racer_id": g[1], "name": g[2].replace("　", "").strip(),
                "age": int(g[3]), "branch": g[4], "weight": int(g[5]), "cls": g[6],
                "win": float(g[7]), "win2": float(g[8]),
                "local_win": float(g[9]), "local_win2": float(g[10]),
                "motor_no": int(g[11]), "motor": float(g[12]),
                "boat_no": int(g[13]), "boat2": float(g[14]),
            })
    return races


# ---------- 競走成績(K) ----------

_BOAT_K = re.compile(r"^\s+(\S+)\s+([1-6])\s+(\d{4})\s+(.+?)\s+(\d+)\s+(\d+)\s+(\S+)\s+([1-6])\s+(\S+)")


def _num(s: str):
    return float(s) if re.match(r"^\d?\.\d+$", s) else None


def parse_results(text: str) -> list[dict]:
    """競走成績のテキストを、レースごとの結果(天候・風・波・着順・払戻)にする。"""
    races, venue, cur, in_pay = [], None, None, False
    pay: dict[int, tuple] = {}
    vraces: list[dict] = []
    for raw in text.split("\n"):
        m = re.match(r"^(\d\d)KBGN", raw)
        if m:
            venue, cur, in_pay, pay, vraces = int(m.group(1)), None, False, {}, []
            continue
        if re.match(r"^\d\dKEND", raw):
            for r in vraces:
                if r["race"] in pay:
                    r["trifecta"], r["payout"] = pay[r["race"]]
            venue, cur = None, None
            continue
        if venue is None:
            continue
        line = unicodedata.normalize("NFKC", raw)
        if "[払戻金]" in line:
            in_pay = True
            continue
        h = re.match(r"^\s*(\d{1,2})R\s+(.*?)\s+H(\d+)m", line)
        if h:
            in_pay = False
            wx = re.search(r"H\d+m\s+(\S+)\s+風", line)
            wd = re.search(r"風\s+(\D*?)\s*(\d+)m", line)
            wv = re.search(r"波\s*(\d+)cm", line)
            cur = {
                "venue_code": venue, "venue": VENUES.get(venue, str(venue)),
                "race": int(h.group(1)), "title": h.group(2).strip(),
                "weather": wx.group(1) if wx else "",
                "wind_dir": wd.group(1).strip() if wd else "",
                "wind_speed": int(wd.group(2)) if wd else 0,
                "wave_cm": int(wv.group(1)) if wv else 0,
                "boats": [], "trifecta": None, "payout": None,
            }
            races.append(cur)
            vraces.append(cur)
            continue
        if in_pay:
            p = re.match(r"^\s*(\d{1,2})R\s+(\d)-(\d)-(\d)\s+(\d+)", line)
            if p:
                pay[int(p.group(1))] = (f"{p.group(2)}-{p.group(3)}-{p.group(4)}", int(p.group(5)))
            continue
        b = _BOAT_K.match(line)
        if b and cur is not None:
            g = b.groups()
            cur["boats"].append({
                "finish": int(g[0]) if g[0].isdigit() else 0, "finish_raw": g[0],
                "lane": int(g[1]), "racer_id": g[2], "name": g[3].replace(" ", ""),
                "motor_no": int(g[4]), "boat_no": int(g[5]),
                "ex": _num(g[6]), "course": int(g[7]), "st": _num(g[8]),
            })
    return races


CSV_COLUMNS = ["date", "venue", "race", "racer_id", "course", "finish",
               "wind_speed", "wave_cm", "st", "lane"]


def result_rows(d: date, races: list[dict]):
    for r in races:
        for b in r["boats"]:
            yield [d.isoformat(), r["venue"], r["race"], b["racer_id"], b["course"], b["finish"],
                   r["wind_speed"], r["wave_cm"], "" if b["st"] is None else b["st"], b["lane"]]


# ---------- コマンド ----------

def _daterange(a: date, b: date):
    while a <= b:
        yield a
        a += timedelta(days=1)


def cmd_fetch(a) -> None:
    got = skipped = failed = 0
    for d in _daterange(date.fromisoformat(a.start), date.fromisoformat(a.end)):
        for kind in a.kinds.split(","):
            p = Path(a.out) / file_name(d, kind)
            if p.exists() and p.stat().st_size > 0:
                skipped += 1
                continue
            try:
                fetch_one(d, kind, a.out)
                got += 1
                print("取得", p.name)
            except FileNotFoundError as e:
                failed += 1
                print("取得できず", p.name, "-", e)
            time.sleep(a.sleep)  # 公式サーバーに負荷をかけないよう、間隔をあける
    print(f"完了: 新規 {got} / 取得済みのためスキップ {skipped} / 取得できず {failed}")


def cmd_build(a) -> None:
    files = sorted(Path(a.data).glob("k??????.lzh"))
    n_races = n_rows = 0
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for p in files:
            n = p.stem
            d = date(2000 + int(n[1:3]), int(n[3:5]), int(n[5:7]))
            races = parse_results(read_text(p))
            rows = list(result_rows(d, races))
            w.writerows(rows)
            n_races += len(races)
            n_rows += len(rows)
    print(f"{a.out}: {len(files)}日分, {n_races}レース, {n_rows}行")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--start", required=True, metavar="YYYY-MM-DD")
    f.add_argument("--end", required=True, metavar="YYYY-MM-DD")
    f.add_argument("--out", default="data")
    f.add_argument("--kinds", default="K", help="K=競走成績, B=番組表(カンマ区切り)")
    f.add_argument("--sleep", type=float, default=1.0)
    b = sub.add_parser("build")
    b.add_argument("--data", default="data")
    b.add_argument("--out", default="results.csv")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    main()
