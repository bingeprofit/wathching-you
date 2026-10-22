#!/usr/bin/env python3
"""한국·미국 상승 급변 신호 성과 비교 차트 — 신호 성과 집계(signal-review) 끝에 텔레그램으로.

  python review_chart.py --kr state_kr/signals.jsonl --us state/signals.jsonl [--send] [--out review_cmp.png]

그림 4칸 (모두 비용 차감 수익률, 알림 시각 가격 기준):
  ① 승률 — T+1 종가 · T+5 종가 · 규칙 청산(익절 +5% · 손절 -1.5σ · T+5)
  ② T+5 수익률 분포 — 점(알림 하나) + 중앙값·사분위
  ③ 나눠 보기 — 한국은 품질 별점, 미국은 시총 $20억 기준
  ④ 누적 평균 T+5 — 알림 날짜 순서대로 쌓은 평균 (표본이 늘며 어디로 수렴하나)

공개 저장소라 종목명은 그림·로그 어디에도 넣지 않는다 (텔레그램 비공개 대화로만 보냄).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import datetime as dt

import requests

KR_C, US_C = "#eb6834", "#2a78d6"            # 한국 주황 · 미국 파랑 (Market Daily 계열 색과 같은 쪽)
INK, INK2, MUTE, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#fcfcfb"
CLIP = 40.0                                  # 분포 칸에서 ±40% 밖은 가장자리에 붙여 표시


def load(path: str) -> list[dict]:
    rows = []
    if not path or not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("void") or float(r.get("pct") or 0) <= 0:
            continue                          # 상승 급변만
        rows.append(r)
    return rows


def rule_net(r: dict):
    s = (r.get("sim") or {}).get("tp5")
    return s.get("net") if isinstance(s, dict) else None


def win(v: list[float]):
    return (sum(1 for x in v if x > 0) / len(v) * 100) if v else None


def setup():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager as fm
    for p in ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",):
        if os.path.exists(p):
            fm.fontManager.addfont(p)
            b = p.replace("NanumGothic.ttf", "NanumGothicBold.ttf")
            if os.path.exists(b):
                fm.fontManager.addfont(b)
            matplotlib.rcParams["font.family"] = fm.FontProperties(fname=p).get_name()
    matplotlib.rcParams.update({
        "axes.unicode_minus": False, "figure.facecolor": SURF, "axes.facecolor": SURF,
        "savefig.facecolor": SURF, "text.color": INK, "axes.labelcolor": INK2,
        "xtick.color": INK2, "ytick.color": INK2, "axes.edgecolor": "#d4d3cd",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
        "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
        "font.size": 10, "text.parse_math": False})
    import matplotlib.pyplot as plt
    return plt


def chart(kr: list[dict], us: list[dict], out: str) -> tuple[str, str]:
    plt = setup()
    M = [("한국", kr, KR_C), ("미국", us, US_C)]
    fig, axs = plt.subplots(2, 2, figsize=(12.5, 8.6))
    a1, a2, a3, a4 = axs[0, 0], axs[0, 1], axs[1, 0], axs[1, 1]

    # ① 승률
    cats = [("T+1 종가", "r_t1_net"), ("T+5 종가", "r_t5_net"), ("규칙 청산", None)]
    w = 0.36
    for i, (nm, rows, c) in enumerate(M):
        for j, (lab, k) in enumerate(cats):
            v = [rule_net(r) if k is None else r.get(k) for r in rows]
            v = [x for x in v if x is not None]
            wr = win(v)
            x = j + (i - 0.5) * w
            if wr is None:
                a1.text(x, 2, "–", ha="center", color=MUTE)
                continue
            a1.bar(x, wr, w * 0.92, color=c, label=nm if j == 0 else None)
            a1.text(x, wr + 1.5, f"{wr:.0f}%\nn={len(v)}", ha="center", va="bottom", fontsize=8.5, color=INK2)
    a1.axhline(50, color=MUTE, lw=0.9, ls="--")
    a1.set_xticks(range(len(cats)), [c[0] for c in cats])
    a1.set_ylim(0, 105)
    a1.set_ylabel("승률 (%)")
    a1.set_title("① 승률 — 비용 차감", loc="left", fontsize=11, fontweight="bold")
    a1.legend(loc="upper right", frameon=False)

    # ② T+5 분포
    import random
    rnd = random.Random(7)
    for i, (nm, rows, c) in enumerate(M):
        v = [r["r_t5_net"] for r in rows if r.get("r_t5_net") is not None]
        if not v:
            a2.text(i, 0, "표본 없음", ha="center", color=MUTE)
            continue
        vc = [max(-CLIP, min(CLIP, x)) for x in v]
        a2.scatter([i + rnd.uniform(-0.18, 0.18) for _ in vc], vc, s=16, color=c, alpha=0.45,
                   edgecolors=SURF, linewidths=0.6)
        q = statistics.quantiles(v, n=4) if len(v) >= 4 else [min(v), statistics.median(v), max(v)]
        med = statistics.median(v)
        a2.plot([i - 0.28, i + 0.28], [med, med], color=INK, lw=2)
        a2.plot([i, i], [max(-CLIP, q[0]), min(CLIP, q[-1])], color=INK, lw=1)
        a2.text(i + 0.32, med, f"중앙 {med:+.1f}%\n평균 {statistics.fmean(v):+.1f}%", va="center", fontsize=9,
                linespacing=1.5, bbox=dict(facecolor=SURF, edgecolor="none", pad=1.5))
    a2.axhline(0, color=MUTE, lw=0.9)
    a2.set_xticks([0, 1], ["한국", "미국"])
    a2.set_xlim(-0.6, 1.9)
    a2.set_ylabel(f"T+5 수익률 (%, ±{CLIP:.0f}% 밖은 끝에 표시)")
    a2.set_title("② T+5 수익률 분포 — 점 하나가 알림 하나", loc="left", fontsize=11, fontweight="bold")

    # ③ 나눠 보기 (평균 T+5, 막대 옆에 승률·표본)
    groups = []
    for lab, lo, hi in (("★4~5", 4, 5), ("★2~3", 2, 3), ("★0~1", 0, 1)):
        g = [r["r_t5_net"] for r in kr if r.get("r_t5_net") is not None and r.get("q") is not None
             and lo <= int(r["q"]) <= hi]
        groups.append((f"한국 {lab}", g, KR_C))
    for lab, f in (("미국 시총 $20억 이상", lambda m: m and m >= 2e9), ("미국 시총 $20억 미만", lambda m: not m or m < 2e9)):
        g = [r["r_t5_net"] for r in us if r.get("r_t5_net") is not None and f(r.get("mcap"))]
        groups.append((lab, g, US_C))
    ys = list(range(len(groups)))[::-1]
    for y, (lab, g, c) in zip(ys, groups):
        if not g:
            a3.text(0, y, "  표본 없음", va="center", color=MUTE, fontsize=9)
            continue
        m = statistics.fmean(g)
        a3.barh(y, m, 0.62, color=c, alpha=1.0 if len(g) >= 10 else 0.45)
        a3.text(max(m, 0) + 0.4, y, f"{m:+.1f}% · 승률 {win(g):.0f}% · n={len(g)}",
                va="center", ha="left", fontsize=8.5, color=INK2)
    a3.axvline(0, color=MUTE, lw=0.9)
    a3.set_yticks(ys, [g[0] for g in groups])
    a3.set_xlabel("평균 T+5 수익률 (%, 비용 차감) — 흐린 막대 = 표본 10건 미만")
    a3.set_title("③ 나눠 보기 — 한국 품질 별점 · 미국 시총", loc="left", fontsize=11, fontweight="bold")
    lim = max([abs(statistics.fmean(g[1])) for g in groups if g[1]] + [3])
    a3.set_xlim(min(-lim * 1.1, -1), lim * 2.6)

    # ④ 누적 평균 T+5 (알림 날짜순)
    import matplotlib.dates as mdates
    for nm, rows, c in M:
        v = sorted((r.get("date") or "", r["r_t5_net"]) for r in rows if r.get("r_t5_net") is not None)
        if not v:
            continue
        xs, ys2, s, k = [], [], 0.0, 0
        for d, x in v:                       # 같은 날 알림은 묶어서 그날 끝의 누적 평균 한 점
            s += x; k += 1
            t = dt.datetime.strptime(d, "%Y%m%d")
            if xs and xs[-1] == t:
                ys2[-1] = s / k
            else:
                xs.append(t); ys2.append(s / k)
        a4.plot(xs, ys2, color=c, lw=2, marker="o", ms=3, label=f"{nm} {ys2[-1]:+.2f}% (n={len(v)})")
        a4.annotate(f"{ys2[-1]:+.1f}%", (xs[-1], ys2[-1]), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=9, color=c, fontweight="bold")
    a4.axhline(0, color=MUTE, lw=0.9)
    a4.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=7))
    a4.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    a4.set_ylabel("누적 평균 T+5 (%)")
    a4.set_title("④ 누적 평균 T+5 — 알림 날짜 순", loc="left", fontsize=11, fontweight="bold")
    a4.legend(loc="best", frameon=False, fontsize=9)

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    fig.suptitle(f"상승 급변 신호 성과 — 한국 vs 미국  ({today:%m/%d} 기준, 알림 시각 가격 매수 가정)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.text(0.01, 0.005, "비용: 한국 왕복 0.55% · 미국 0.25% 차감 · 규칙 청산 = 익절 +5% / 손절 -1.5×일간σ / T+5 시간청산 "
             "(장중 고저로 시뮬레이션) · 상승 급변(+) 알림만", fontsize=8, color=MUTE)
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    fig.savefig(out, dpi=130)
    plt.close(fig)

    def one(nm, rows):
        v = [r["r_t5_net"] for r in rows if r.get("r_t5_net") is not None]
        return f"{nm} T+5 평균 {statistics.fmean(v):+.1f}% · 승률 {win(v):.0f}% (n={len(v)})" if v else f"{nm} T+5 표본 없음"
    return out, f"📊 상승 급변 성과 비교\n{one('한국', kr)}\n{one('미국', us)}"


def send(path: str, cap: str) -> None:
    tok, cid = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and cid):
        print("(전송 안 함)", path, cap)
        return
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto", timeout=60,
                          data={"chat_id": cid, "caption": cap[:1000]}, files={"photo": f})
    print(f"비교 차트 전송 HTTP {r.status_code}")
    if r.status_code != 200:
        raise SystemExit(f"텔레그램 거절 HTTP {r.status_code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kr", default="state_kr/signals.jsonl")
    ap.add_argument("--us", default="state/signals.jsonl")
    ap.add_argument("--out", default="review_cmp.png")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    kr, us = load(a.kr), load(a.us)
    print(f"상승 알림 한국 {len(kr)} · 미국 {len(us)}")
    if not kr and not us:
        return 0
    path, cap = chart(kr, us, a.out)
    if a.send:
        send(path, cap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
