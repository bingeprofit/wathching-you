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

    todo = [r for r in rows if r.get("r_t5") is None and r.get("px")]
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
    out.append("")
    # 이 갈래가 가장 흥미롭다 — 원인이 특정되지 않은 급변은 수급 충격일
    # 가능성이 높고, 그렇다면 되돌림이 나와야 한다. 가설의 1차 검증이다.
    cut("원인 미확인", lambda r: NO_EVENT in str(r.get("cause") or ""))
    cut("원인 특정됨", lambda r: r.get("cause") and NO_EVENT not in str(r.get("cause")))
    out.append("")
    cut("한국", lambda r: r.get("market") == "kr")
    cut("미국", lambda r: r.get("market") == "us")

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
