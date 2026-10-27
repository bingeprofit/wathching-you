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


STOPS = [1.0, 1.5, 2.0, 3.0, 0.0]
SDMED: dict = {}                           # 시장별 일간σ 중앙값(%) — 'σ 몇 배' 가 몇 % 인지 보여 주려고          # 손절 = 진입가 -k×일간σ (0 = 손절 없음)


def scan(pp: list[tuple[dict, list[dict]]], market: str, stop: bool = True, k: float = STOP_SD) -> list[dict]:
    res = []
    for tp in GRID:
        v, hit = [], 0
        for r, after in pp:
            sd = float(r.get("sd_daily") or 0)
            sl = k * sd * 100 if (stop and k > 0 and sd > 0) else 0.0
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


RULES = [("이전 +5% · -1.5σ", 5, 1.5), ("지금 +15% · -1.5σ", 15, 1.5), ("+15% · -1σ", 15, 1.0),
         ("+15% · -2σ", 15, 2.0), ("+15% · -3σ", 15, 3.0), ("+15% · 손절 없음", 15, 0.0)]


def one(pp, market: str, tp: float, k: float) -> dict | None:
    r = {x["tp"]: x for x in scan(pp, market, True, k)}
    return r.get(tp)


def paper_actual(book_path: str) -> dict:
    """모의계좌 실제 청산 — 진입 때 익절선(target/entry)으로 규칙을 나눈다."""
    out: dict = {}
    try:
        bk = json.load(open(book_path))
    except Exception:
        return out
    for c in bk.get("closed") or []:
        e, t, net = float(c.get("entry") or 0), float(c.get("target") or 0), c.get("ret_net_pct")
        if not e or net is None:
            continue
        lab = f"+{round((t / e - 1) * 100):g}%" if t else "익절 없음"
        out.setdefault(lab, []).append(float(net))
    return out


def rules_chart(pp_by: dict, paper: dict, out: str) -> tuple[str, str]:
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, 3, figsize=(14.5, 5.2), gridspec_kw={"width_ratios": [1, 1, 0.8]})
    cap = []
    for ax, (mk, nm, c) in zip(axs, (("kr", "한국", "#eb6834"), ("us", "미국", "#2a78d6"))):
        pp = pp_by.get(mk) or []
        ys, labs = [], []
        for i, (lab, tp, k) in enumerate(RULES):
            x = one(pp, mk, tp, k) if pp else None
            labs.append(lab)
            ys.append(x)
        yy = list(range(len(RULES)))[::-1]
        for y, x, (lab, tp, k) in zip(yy, ys, RULES):
            if not x:
                continue
            col = c if lab.startswith("지금") else ("#b9b8b2" if lab.startswith("이전") else c)
            ax.barh(y, x["mean"], 0.6, color=col, alpha=1.0 if lab.startswith(("지금", "이전")) else 0.45)
            ax.text(max(x["mean"], 0) + 0.15, y, f"{x['mean']:+.2f}% · 승률 {x['win']:.0f}% · 익절 {x['hit']:.0f}%",
                    va="center", fontsize=8.5, color="#52514e")
        ax.axvline(0, color="#8a8984", lw=0.9)
        ax.set_yticks(yy, labs, fontsize=9)
        sdm = SDMED.get(f"{nm} 상승 급변") or 0
        ax.set_title(f"{nm} 상승 급변 (n={len(pp)}) · 1σ ≈ {sdm:.1f}%", loc="left", fontsize=11, fontweight="bold")
        ax.set_xlabel("평균 수익률 (%, 비용 차감) · 알림 시각 가격 매수 가정")
        lo = min([x["mean"] for x in ys if x] + [0]); hi = max([x["mean"] for x in ys if x] + [0])
        ax.set_xlim(lo - 0.5, hi + (hi - lo) * 1.2 + 1.5)
        o, n_ = ys[0], ys[1]
        if o and n_:
            cap.append(f"{nm}: 이전 +5% {o['mean']:+.2f}%(승률 {o['win']:.0f}%) → 지금 +15% {n_['mean']:+.2f}%(승률 {n_['win']:.0f}%)")
    a3 = axs[2]
    if paper:
        items = sorted(paper.items(), key=lambda kv: float(kv[0].strip("+%")) if kv[0][0] == "+" else 999)
        for i, (lab, v) in enumerate(items):
            m = statistics.fmean(v)
            a3.bar(i, m, 0.6, color="#eb6834" if lab == "+15%" else "#b9b8b2")
            a3.text(i, m, f" {m:+.2f}% · 승률 {sum(x > 0 for x in v) / len(v) * 100:.0f}% · n={len(v)}",
                    ha="center", va="bottom" if m >= 0 else "top", fontsize=8)
        ms = [statistics.fmean(v) for _, v in items] + [0]
        span = max(ms) - min(ms) or 1
        a3.set_ylim(min(ms) - span * 0.35, max(ms) + span * 0.35)
        a3.set_xticks(range(len(items)), [f"익절 {k}" for k, _ in items])
        a3.tick_params(axis="x", pad=2)
        a3.axhline(0, color="#8a8984", lw=0.9)
        cap.append("모의계좌 실제 청산: " + " · ".join(f"익절 {k} {statistics.fmean(v):+.2f}%(n={len(v)})" for k, v in items))
    else:
        a3.text(0.5, 0.5, "청산된 모의 거래 없음", ha="center", va="center", transform=a3.transAxes, color="#8a8984")
    a3.set_title("한국 모의계좌 실제 청산 (규칙별)", loc="left", fontsize=11, fontweight="bold")
    a3.set_ylabel("평균 순수익률 (%)")
    fig.suptitle("익절·손절 규칙 비교 — 이전(+5%) vs 지금(+15%) · 손절 폭별", x=0.01, ha="left",
                 fontsize=12.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out, "📐 익절·손절 규칙 비교\n" + "\n".join(cap)


def heat(grids: dict, out: str) -> str:
    """시장별 (손절 × 익절) 평균 수익률 표. 현재 규칙(익절 +5% · 손절 1.5σ)은 테두리, 최고 칸은 ★."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm
    names = list(grids)
    fig, axs = plt.subplots(len(names), 1, figsize=(12.5, 2.3 + 2.1 * len(names)))
    axs = axs if len(names) > 1 else [axs]
    lab = ["없음" if g == 0 else f"+{g}%" for g in GRID]
    for ax, nm in zip(axs, names):
        M, n = grids[nm]
        vals = [v for row in M for v in row if v is not None]
        if not vals:
            continue
        lim = max(abs(min(vals)), abs(max(vals)), 0.5)
        Z = [[(v if v is not None else 0) for v in row] for row in M]
        ax.imshow(Z, cmap="RdBu_r", norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
        bi, bj = max(((i, j) for i in range(len(M)) for j in range(len(GRID)) if M[i][j] is not None),
                     key=lambda ij: M[ij[0]][ij[1]])
        for i in range(len(M)):
            for j in range(len(GRID)):
                v = M[i][j]
                if v is None:
                    continue
                ax.text(j, i, ("★" if (i, j) == (bi, bj) else "") + f"{v:+.1f}", ha="center", va="center",
                        fontsize=8, color="white" if abs(v) > lim * 0.55 else "#0b0b0b",
                        fontweight="bold" if (i, j) == (bi, bj) else "normal")
        ci = STOPS.index(1.5)
        for g, ls in ((5, ":"), (15, "-")):        # 이전 규칙 +5% (점선) · 지금 규칙 +15% (실선)
            cj = GRID.index(g)
            ax.add_patch(plt.Rectangle((cj - 0.5, ci - 0.5), 1, 1, fill=False, ec="#0b0b0b", lw=2, ls=ls))
        ax.set_xticks(range(len(GRID)), lab, fontsize=8.5)
        sdm = SDMED.get(nm) or 0
        ax.set_yticks(range(len(STOPS)), [("손절 없음" if k == 0 else f"손절 -{k:g}σ (≈-{k * sdm:.1f}%)") for k in STOPS],
                      fontsize=8.5)
        ax.grid(False)
        ax.set_title(f"{nm} (n={n}) — 평균 수익률 %, 비용 차감 · ★ 최고 · 실선 = 지금 규칙(+15% · -1.5σ) · 점선 = 이전(+5%)",
                     loc="left", fontsize=10, fontweight="bold")
    axs[-1].set_xlabel("익절 수준 (진입가 대비, 다음 날부터 고가로 판정) · 시간청산 T+5")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kr", default="state_kr/signals.jsonl")
    ap.add_argument("--us", default="state/signals.jsonl")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--daily", action="store_true", help="매일 집계용: 규칙 비교 + 히트맵만")
    a = ap.parse_args()
    from kis import KIS
    api = KIS(os.environ["KIS_APP_KEY"], os.environ["KIS_APP_SECRET"],
              state_dir=os.path.dirname(a.kr) or "state", log=lambda *x: None)
    series, notes, PP = {}, [], {}
    for mk, path, c in (("kr", a.kr, "#eb6834"), ("us", a.us, "#2a78d6")):
        rows = load(path)
        pp = paths(rows, api, mk)
        PP[mk] = pp
        sds = [float(r.get("sd_daily") or 0) * 100 for r, _ in pp if r.get("sd_daily")]
        SDMED[{"kr": "한국", "us": "미국"}[mk] + " 상승 급변"] = statistics.median(sds) if sds else 0
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
    grids = {}
    for mk, nm in (("kr", "한국 상승 급변"), ("us", "미국 상승 급변")):
        pp = PP.get(mk) or []
        if not pp:
            continue
        M = []
        for k in STOPS:
            res = {x["tp"]: x for x in scan(pp, mk, True, k)}
            M.append([res[g]["mean"] if g in res else None for g in GRID])
            if res:
                b = max(res.values(), key=lambda x: x["mean"])
                print(f"::notice title={nm} 손절 {k:g}σ::최고 익절 {b['tp']}% 평균 {b['mean']:+.2f}% 승률 {b['win']:.0f}% "
                      f"(n={b['n']}) · +5% {res[5]['mean']:+.2f}%/{res[5]['win']:.0f}%", flush=True)
        grids[nm] = (M, len(pp))
    hp = heat(grids, "tp_heat.png") if grids else None
    rp, rcap = rules_chart(PP, paper_actual(os.path.join(os.path.dirname(a.kr) or "state", "paper_book.json")),
                           "tp_rules.png")
    print(rcap)
    if a.send and os.environ.get("TELEGRAM_TOKEN"):
        import requests
        with open(rp, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendPhoto", timeout=60,
                          data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "caption": rcap[:1000]}, files={"photo": f})
    if a.send and os.environ.get("TELEGRAM_TOKEN") and not a.daily:
        import requests
        with open(out, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendPhoto", timeout=60,
                              data={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                                    "caption": "📐 익절 수준 스캔\n" + "\n".join(notes)}, files={"photo": f})
        print(f"전송 HTTP {r.status_code}")
    if a.send and os.environ.get("TELEGRAM_TOKEN"):
        import requests
        if hp:
            with open(hp, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendPhoto", timeout=60,
                              data={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                                    "caption": "📐 손절 × 익절 조합별 평균 수익률 (비용 차감)"}, files={"photo": f})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
