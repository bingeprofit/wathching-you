#!/usr/bin/env python3
"""익절 수준 스캔 — 상승 급변 알림을 '알림 시각 가격에 샀다면' 몇 % 익절이 가장 나았나.

  python tp_scan.py --kr state_kr/signals.jsonl --us state/signals.jsonl [--send]

backfill.sim_exit 규칙 그대로 (라이브 모의청산과 같은 규칙):
  · D0(알림일)은 종가만, 익절은 D+1 부터 고가로 판정, 손절은 D0 종가부터 저가로
  · 같은 날 익절·손절 둘 다 닿으면 손절로 (부풀리지 않는 쪽)
  · 손절 = 진입가 -1.5×일간σ, 시간청산 T+5
  · 비용 차감: 한국 0.55% · 미국 0.25% (왕복)
익절 0 = 익절 없이 손절·T+5 만. 결과는 텔레그램 차트 한 장 (종목명 없음).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics

from backfill import sim_exit

GRID = [0, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30]
COST = {"kr": 0.55, "us": 0.25}
STOP_SD = float(os.environ.get("STOP_SD", "1.5"))
HOLD = int(os.environ.get("HOLD_DAYS", "5"))


def load(path: str) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("void") or float(r.get("pct") or 0) <= 0 or not r.get("px"):
            continue
        if (r.get("days_after") or 0) < 1:      # 하루라도 지나야 익절 판정이 시작된다
            continue
        rows.append(r)
    return rows


def paths(rows: list[dict], api, market: str) -> list[tuple[dict, list[dict]]]:
    cache: dict[str, list[dict]] = {}
    out = []
    for r in rows:
        tk = r["ticker"]
        if tk not in cache:
            try:
                cache[tk] = (api.daily(tk, 150) if market == "kr"
                             else api.daily_os(r.get("excd") or "NAS", tk)) or []
            except Exception:
                cache[tk] = []
        seq = sorted((b for b in cache[tk] if b.get("close") and b.get("date")), key=lambda b: str(b["date"]))
        d0 = str(r.get("date") or "")
        after = [b for b in seq if str(b["date"]) >= d0]
        if after and str(after[0]["date"]) == d0:
            out.append((r, after))
    return out


def scan(pp: list[tuple[dict, list[dict]]], market: str, stop: bool = True) -> list[dict]:
    res = []
    for tp in GRID:
        v, hit = [], 0
        for r, after in pp:
            sd = float(r.get("sd_daily") or 0)
            sl = STOP_SD * sd * 100 if (stop and sd > 0) else 0.0
            o = sim_exit(float(r["px"]), after, tp, sl, HOLD)
            if not o:
                continue
            v.append(o["ret"] - COST[market])
            hit += o["why"] == "익절"
        if v:
            res.append({"tp": tp, "n": len(v), "mean": statistics.fmean(v), "med": statistics.median(v),
                        "win": sum(x > 0 for x in v) / len(v) * 100, "hit": hit / len(v) * 100})
    return res


def chart(series: dict, out: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager as fm
    p = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"
    if os.path.exists(p):
        fm.fontManager.addfont(p)
        matplotlib.rcParams["font.family"] = fm.FontProperties(fname=p).get_name()
    matplotlib.rcParams.update({"axes.unicode_minus": False, "axes.spines.top": False,
                                "axes.spines.right": False, "axes.grid": True, "grid.color": "#e6e5e0",
                                "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb"})
    import matplotlib.pyplot as plt
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12.5, 5.2))
    xs = list(range(len(GRID)))
    lab = ["없음" if g == 0 else f"+{g}%" for g in GRID]
    for name, (res, c, ls) in series.items():
        if not res:
            continue
        m = {x["tp"]: x for x in res}
        y = [m[g]["mean"] if g in m else None for g in GRID]
        w = [m[g]["win"] if g in m else None for g in GRID]
        a1.plot(xs, y, color=c, ls=ls, lw=2, marker="o", ms=4, label=f"{name} (n={res[0]['n']})")
        a2.plot(xs, w, color=c, ls=ls, lw=2, marker="o", ms=4, label=name)
        best = max(res, key=lambda x: x["mean"])
        bi = GRID.index(best["tp"])
        a1.annotate(f"최고 {lab[bi]} {best['mean']:+.2f}%", (bi, best["mean"]), xytext=(0, 9),
                    textcoords="offset points", ha="center", fontsize=9, color=c, fontweight="bold")
    for a in (a1, a2):
        a.set_xticks(xs, lab, fontsize=8.5)
        a.set_xlabel("익절 수준 (진입가 대비)")
    a1.axhline(0, color="#8a8984", lw=0.9)
    a1.set_ylabel("평균 수익률 (%, 비용 차감)")
    a1.set_title("평균 수익률 — 익절 수준별", loc="left", fontweight="bold")
    a1.legend(frameon=False, fontsize=9)
    a2.axhline(50, color="#8a8984", lw=0.9, ls="--")
    a2.set_ylabel("승률 (%)")
    a2.set_title("승률 — 익절 수준별", loc="left", fontweight="bold")
    fig.suptitle("상승 급변 알림 · 익절 수준 스캔 (손절 -1.5σ · T+5 시간청산 · 익절은 다음 날부터)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kr", default="state_kr/signals.jsonl")
    ap.add_argument("--us", default="state/signals.jsonl")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    from kis import KIS
    api = KIS(os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"],
              state_dir=os.path.dirname(a.kr) or "state", log=lambda *x: None)
    series, notes = {}, []
    for mk, path, c in (("kr", a.kr, "#eb6834"), ("us", a.us, "#2a78d6")):
        rows = load(path)
        pp = paths(rows, api, mk)
        name = {"kr": "한국", "us": "미국"}[mk]
        res = scan(pp, mk)
        series[name] = (res, c, "-")
        if mk == "kr":
            hq = [(r, b) for r, b in pp if (r.get("q") or 0) >= 4]
            series["한국 ★4~5"] = (scan(hq, mk), c, ":")
        if mk == "us":
            big = [(r, b) for r, b in pp if (r.get("mcap") or 0) >= 2e9]
            series["미국 시총 $20억 이상"] = (scan(big, mk), c, ":")
        for nm in [k for k in series if k.startswith(name)]:
            res2 = series[nm][0]
            if not res2:
                continue
            best = max(res2, key=lambda x: x["mean"])
            line = " · ".join(f"{('없음' if x['tp'] == 0 else '+%g%%' % x['tp'])} {x['mean']:+.2f}%/{x['win']:.0f}%"
                              for x in res2)
            print(f"::notice title={nm} n={res2[0]['n']} 최고 익절 {best['tp']}%::{line}", flush=True)
            notes.append(f"{nm} (n={res2[0]['n']}): 최고 "
                         f"{'익절 없음' if best['tp'] == 0 else '+%g%%' % best['tp']} → 평균 {best['mean']:+.2f}% · "
                         f"승률 {best['win']:.0f}% · 익절 도달 {best['hit']:.0f}%")
    out = chart(series, "tp_scan.png")
    if a.send and os.environ.get("TELEGRAM_TOKEN"):
        import requests
        with open(out, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendPhoto", timeout=60,
                              data={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                                    "caption": "📐 익절 수준 스캔\n" + "\n".join(notes)}, files={"photo": f})
        print(f"전송 HTTP {r.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
