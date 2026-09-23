#!/usr/bin/env python3
"""알림 이후 수익률을 채워 넣고, 신호에 엣지가 있는지 집계한다.

왜 따로 도는가
  장중에는 '앞으로 어떻게 될지' 를 알 수 없다. 하루가 지나야 T+1 이 생긴다.
  그래서 장 끝난 뒤 하루 한 번 돌면서 signals.jsonl 의 빈 칸을 채운다.

왜 전수인가
  반자동으로 체결된 것만 보면 그것은 DH님이 고른 표본이다. 고르지 않은 알림이
  어떻게 됐는지를 모르면 "이 신호에 엣지가 있나" 에는 영영 답할 수 없고,
  "내 판단이 도움이 됐나" 에도 비교군이 없어 답할 수 없다.

집계의 마지막 줄이 이 시스템의 존재 이유다 — 승인한 것들의 평균에서 전체
알림 평균을 뺀 값. 음수면 재량이 신호를 깎아먹고 있다는 뜻이고, 그것도
알아야 하는 정보다.

실행:
  python backfill.py                 # 채우고 집계 출력
  python backfill.py --send          # 집계를 텔레그램으로, 원본도 파일로 첨부
"""
from __future__ import annotations
import argparse, json, os, statistics, datetime as dt
from zoneinfo import ZoneInfo
import requests

KST, ET = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
STATE_DIR = os.environ.get("STATE_DIR", "state")
SIG = os.path.join(STATE_DIR, "signals.jsonl")
# 왕복 거래비용(%). 증권거래세 0.15% + 수수료 + 급변 종목 슬리피지 가정.
COST = float((os.environ.get("ROUND_TRIP_PCT") or "0.55").strip())
NO_EVENT = "확인된 촉발 사건 없음"
# 시뮬레이션할 익절 수준(%). 0 은 "익절 없음" — 시간청산·손절만 쓰는 기준선이다.
# 어느 수준이 맞는지 미리 고를 수 없으니, 전부 돌려 두고 데이터가 고르게 한다.
TP_GRID = [float(x) for x in (os.environ.get("TP_GRID") or "0,3,5,8,12").split(",")]
STOP_SD = float((os.environ.get("STOP_SD") or "1.5").strip())
HOLD_DAYS = int((os.environ.get("HOLD_DAYS") or "5").strip())


def sim_exit(entry: float, bars: list[dict], tp_pct: float,
             sl_pct: float, hold: int) -> dict | None:
    """일봉 경로로 익절·손절·시간청산을 재현한다. bars[0] 이 알림 당일(D0).

    D0 은 **종가만** 보고, **익절은 D+1 부터** 본다. 진입이 장중이라 그날
    고가·저가에는 진입 전 구간이 섞여 있고, 일봉으로는 당일 장중 경로를
    복원할 수 없다. 라이브 청산도 같은 규칙을 쓴다 — 한쪽만 당일 익절하면
    두 결과를 나란히 놓을 수 없고, 그 비교가 이 기록의 존재 이유다.
    손절은 D0 종가부터 본다(리스크 관리는 미루지 않는다).

    같은 날 고가·저가가 익절선과 손절선을 모두 건드리면 순서를 알 수 없다.
    **손절이 먼저 닿았다고 본다** — 역시 부풀리지 않는 쪽이다."""
    if entry <= 0 or not bars:
        return None
    tp = entry * (1 + tp_pct / 100) if tp_pct > 0 else None
    sl = entry * (1 - sl_pct / 100) if sl_pct > 0 else None

    for i, b in enumerate(bars[:hold + 1]):
        c = float(b.get("close") or 0)
        if i == 0:
            hi = lo = c                      # D0 은 종가만
        else:
            hi = float(b.get("high") or c) or c
            lo = float(b.get("low") or c) or c
        if not c:
            continue
        hit_sl = bool(sl and lo and lo <= sl)
        # 익절은 D+1 부터. 라이브도 같은 규칙이라 둘을 나란히 비교할 수 있다.
        hit_tp = bool(tp and hi and hi >= tp and i >= 1)
        if hit_sl:
            return {"ret": round(-sl_pct, 3), "day": i,
                    "why": "손절+익절 동일봉" if hit_tp else "손절"}
        if hit_tp:
            return {"ret": round(tp_pct, 3), "day": i, "why": "익절"}

    last = bars[min(hold, len(bars) - 1)]
    lc = float(last.get("close") or 0)
    if lc <= 0:
        return None
    return {"ret": round((lc / entry - 1) * 100, 3),
            "day": min(hold, len(bars) - 1), "why": "시간청산"}


def log(*a):
    print(dt.datetime.now(KST).strftime("[%H:%M:%S KST]"), *a, flush=True)


def load_rows() -> list[dict]:
    rows = []
    if not os.path.exists(SIG):
        return rows
    with open(SIG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # 잡이 중간에 끊겨 잘린 줄 — 버리고 계속
    return rows


def save_rows(rows: list[dict]) -> None:
    tmp = SIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, SIG)          # 쓰다 죽어도 원본이 남게


def fill(rows: list[dict]) -> int:
    """미완성 행에 D0 종가·T+1·T+5 수익률을 채운다. 채운 행 수를 돌려준다."""
    try:
        from kis import KIS
    except ImportError:
        log("kis.py 를 찾을 수 없습니다"); return 0
    key, sec = os.environ.get("KIS_APP_KEY", ""), os.environ.get("KIS_APP_SECRET", "")
    if not (key and sec):
        log("KIS 키 미설정 → 수익률 채우기 생략"); return 0
    api = KIS(key, sec, state_dir=STATE_DIR, log=log)

    todo = [r for r in rows if (r.get("r_t5") is None or not r.get("sim"))
            and r.get("px")]
    if not todo:
        log("채울 행 없음"); return 0

    # 종목별로 일봉을 한 번만 받는다. 알림 하나당 한 번 받으면 같은 종목이
    # 하루에 수십 번 나와 유량을 다 쓴다.
    bars: dict[str, list[dict]] = {}
    filled = 0
    for r in todo:
        mk, tk = r.get("market"), r.get("ticker")
        if mk == "fut":
            continue
        k = f"{mk}:{tk}"
        if k not in bars:
            try:
                bars[k] = (api.daily(tk) if mk == "kr"
                           else api.daily_os(r.get("excd") or "NAS", tk)) or []
            except Exception as e:
                log(f"{k} 일봉 실패: {type(e).__name__} {str(e)[:80]}")
                bars[k] = []
        seq = sorted((b for b in bars[k] if b.get("close")),
                     key=lambda b: str(b.get("date") or ""))
        d0 = str(r.get("date") or "")
        after = [b for b in seq if str(b.get("date") or "") >= d0]
        if not after:
            continue
        px = float(r["px"])
        if px <= 0:
            continue

        def ret(i: int):
            if i >= len(after):
                return None
            return round((float(after[i]["close"]) / px - 1) * 100, 3)

        # after[0] 이 알림 당일이다. 아직 그날 봉이 확정 안 됐으면 건너뛴다.
        if str(after[0].get("date")) != d0:
            continue
        r["r_close"], r["r_t1"], r["r_t5"] = ret(0), ret(1), ret(5)
        if r["r_t5"] is not None:
            r["r_t5_net"] = round(r["r_t5"] - COST, 3)
            r["r_t1_net"] = round(r["r_t1"] - COST, 3) if r["r_t1"] is not None else None
            # 익절 수준별로 "그 규칙이었으면 어땠을까" 를 같이 남긴다.
            # 손절선은 실제 운용과 같은 -STOP_SD×σ 를 쓴다.
            sd = float(r.get("sd_daily") or 0.0)
            sl_pct = STOP_SD * sd * 100 if sd > 0 else 0.0
            sim = {}
            for tp in TP_GRID:
                out = sim_exit(px, after, tp, sl_pct, HOLD_DAYS)
                if out:
                    out["net"] = round(out["ret"] - COST, 3)
                    sim[f"tp{tp:g}"] = out
            if sim:
                r["sim"] = sim
                r["sl_pct"] = round(sl_pct, 3)
            filled += 1
    return filled


# ── 집계 ─────────────────────────────────────────────────────────────────
def _stat(vals: list[float]) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "표본 없음"
    win = sum(1 for v in vals if v > 0) / len(vals) * 100
    med = statistics.median(vals)
    return (f"n={len(vals):>3}  평균 {statistics.fmean(vals):+.2f}%  "
            f"중앙 {med:+.2f}%  승률 {win:.0f}%")


def summarize(rows: list[dict]) -> str:
    done = [r for r in rows if r.get("r_t5") is not None]
    out = [f"📊 *신호 집계* — 누적 알림 {len(rows)}건, 수익률 확정 {len(done)}건",
           f"_T+5 영업일 종가 기준, 비용 {COST:.2f}% 차감 전 원수익률_", ""]
    if len(done) < 20:
        out.append(f"아직 표본이 적습니다 ({len(done)}건). 판단은 100건 이후에 하세요.")
        out.append("")
    if not done:
        return "\n".join(out)

    def cut(name, sel):
        sub = [r for r in done if sel(r)]
        if sub:
            out.append(f"*{name}*")
            out.append(f"  T+1  {_stat([r.get('r_t1') for r in sub])}")
            out.append(f"  T+5  {_stat([r.get('r_t5') for r in sub])}")

    cut("전체", lambda r: True)
    out.append("")
    cut("급등 알림 (추종 시 수익)", lambda r: (r.get("pct") or 0) > 0)
    cut("급락 알림 (반등 베팅 시 수익)", lambda r: (r.get("pct") or 0) < 0)
    out.append("")
    cut("순간급변 경로", lambda r: "순간" in str(r.get("trigger") or ""))
    cut("누적 경로만", lambda r: str(r.get("trigger") or "") == "누적")
    # 관심종목은 낮은 문턱으로 걸린 대형주다. 소형주 급등과 성과가 다를 것이라
    # 따로 본다. 재알림(같은 날 두 번째)도 첫 알림과 다를 수 있어 분리한다.
    cut("관심종목 경로", lambda r: str(r.get("trigger") or "") == "관심종목")
    cut("같은 날 재알림", lambda r: (r.get("alert_n") or 1) > 1)
    out.append("")
    # 이 갈래가 가장 흥미롭다 — 원인이 특정되지 않은 급변은 수급 충격일
    # 가능성이 높고, 그렇다면 되돌림이 나와야 한다. 가설의 1차 검증이다.
    cut("원인 미확인", lambda r: NO_EVENT in str(r.get("cause") or ""))
    cut("원인 특정됨", lambda r: r.get("cause") and NO_EVENT not in str(r.get("cause")))
    out.append("")
    cut("한국", lambda r: r.get("market") == "kr")
    cut("미국", lambda r: r.get("market") == "us")

    # ── 익절 수준 비교 ─────────────────────────────────────────────────
    # 이 표가 "몇 %에서 익절할 것인가" 에 답한다. 손절선은 실제 운용과 같은
    # -1.5σ 로 고정하고 익절만 바꿔 가며 같은 표본에 적용한 결과다.
    sims = [r for r in done if r.get("sim")]
    if sims:
        out += ["", "─" * 28, f"*익절 수준 비교* (n={len(sims)}, 손절 −{STOP_SD}σ 고정)",
                "_비용 차감 기준. 0% 는 익절 없이 T+5·손절만_", ""]
        rows = []
        for tp in TP_GRID:
            k = f"tp{tp:g}"
            v = [r["sim"][k] for r in sims if r.get("sim", {}).get(k)]
            if not v:
                continue
            nets = [x["net"] for x in v]
            hit = sum(1 for x in v if x["why"] == "익절") / len(v) * 100
            cut = sum(1 for x in v if x["why"].startswith("손절")) / len(v) * 100
            days = statistics.fmean([x["day"] for x in v])
            rows.append((statistics.fmean(nets), tp, len(v), hit, cut, days,
                         sum(1 for x in nets if x > 0) / len(nets) * 100))
        for avg, tp, n, hit, cut, days, win in rows:
            # 한글은 반각 둘 폭이라 %>7 같은 정렬이 어긋난다. 라벨을 모두
            # 같은 시각 폭(반각 8)으로 맞춰 둔다.
            tag = "익절없음" if tp == 0 else f"익절{f'+{tp:g}%':>4}"
            out.append(f"  `{tag}` 평균 {avg:+.2f}%  승률 {win:.0f}%  "
                       f"(익절 {hit:.0f}% · 손절 {cut:.0f}%)  보유 {days:.1f}일")
        if rows:
            best = max(rows)
            tag = "익절 없음" if best[1] == 0 else f"+{best[1]:g}% 익절"
            out.append(f"  → 현재 표본 최선: *{tag}* (평균 {best[0]:+.2f}%)")
            if len(sims) < 50:
                out.append("  _표본 50건 전에는 순위가 계속 바뀝니다._")

    # ── 재량의 기여 ────────────────────────────────────────────────────
    try:
        bk = json.load(open(os.path.join(STATE_DIR, "paper_book.json")))
        closed = [c for c in (bk.get("closed") or []) if c.get("ret_pct") is not None]
    except Exception:
        closed = []
    out += ["", "─" * 28, "*모의 체결 (승인한 것만)*"]
    if not closed:
        out.append("  아직 체결된 것이 없습니다.")
    else:
        rets = [c["ret_pct"] for c in closed]
        nets = [c.get("ret_net_pct") for c in closed]
        out.append(f"  실현  {_stat(rets)}")
        out.append(f"  비용차감  {_stat(nets)}")
        kr5 = [r.get("r_t5") for r in done if r.get("market") == "kr"]
        kr5 = [v for v in kr5 if v is not None]
        if kr5 and rets:
            gap = statistics.fmean(rets) - statistics.fmean(kr5)
            verdict = "재량이 더한 쪽" if gap > 0 else "재량이 깎은 쪽"
            out.append(f"  *승인표본 − 전체평균 = {gap:+.2f}%p* ({verdict})")
            out.append("  _표본이 30건은 넘어야 의미를 둘 수 있습니다._")
        byr: dict[str, list] = {}
        for c in closed:
            byr.setdefault(c.get("exit_reason") or "?", []).append(c["ret_pct"])
        for k, v in sorted(byr.items()):
            out.append(f"  {k}: {_stat(v)}")
    return "\n".join(out)


def send_tg(text: str, attach: bool) -> None:
    tok, cid = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (tok and cid):
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=30,
                      data={"chat_id": cid, "text": text[:3900], "parse_mode": "Markdown"})
    except Exception as e:
        log(f"텔레그램 전송 실패: {str(e)[:80]}")
    # 원본은 공개 저장소에 커밋하거나 Actions 아티팩트로 올리면 관심종목이
    # 그대로 드러난다. 텔레그램 비공개 대화로 보내는 편이 안전하다.
    if attach and os.path.exists(SIG):
        try:
            with open(SIG, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{tok}/sendDocument", timeout=60,
                              data={"chat_id": cid}, files={"document": ("signals.jsonl", f)})
        except Exception as e:
            log(f"원본 첨부 실패: {str(e)[:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="집계를 텔레그램으로 보낸다")
    ap.add_argument("--attach", action="store_true", help="원본 jsonl 도 함께 첨부")
    a = ap.parse_args()

    rows = load_rows()
    if not rows:
        log(f"{SIG} 가 비어 있습니다 — 알림이 쌓이면 채워집니다"); return
    n = fill(rows)
    if n:
        save_rows(rows)
    log(f"수익률 채움 {n}건 / 전체 {len(rows)}건")
    text = summarize(rows)
    if a.send:
        send_tg(text, a.attach)
    else:
        print(text)


if __name__ == "__main__":
    main()
