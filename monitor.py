#!/usr/bin/env python3
"""장중 급등락 모니터 — 미국 주식 / 한국 주식 / 해외선물. 시세는 전부 KIS OpenAPI.

장중에 짧은 주기로 스냅샷을 찍고, 통계적으로 이례적인 움직임만 골라 원인을
웹 검색으로 확인해 텔레그램으로 보낸다.

설계 원칙 (일일 귀인 블록과 동일):
  · 단순 등락률이 아니라 그 종목 자체의 변동성으로 표준화한 z 로 판단한다.
    변동성 큰 종목이 매일 걸리는 것을 막기 위함.
  · 거래량 동반을 함께 요구한다. 가격만 튀고 거래가 없으면 호가 공백일 뿐이다.
  · 근거 없는 서사를 만들지 않는다. 확인 안 되면 "확인된 촉발 사건 없음" 으로 둔다.

두 가지 탐지 경로:
  (A) 일중 누적 — 전일종가 대비 등락률이 그 종목 일간변동성 대비 이례적
  (B) 순간 급변 — 최근 N분 변동이 그 구간 기준으로 이례적  ← 촘촘한 폴링의 존재 이유
  (A) 만 보면 갭상승 후 종일 횡보하는 종목이 계속 걸린다. (B) 가 '지금 벌어지는 것'을 잡는다.

후보 종목은 전 종목을 훑어서가 아니라 KIS 등락률 순위·거래량급증 순위로 좁힌다.
거기에 항상 보는 관심종목과, 한 번이라도 순위에 들었던 종목(추적분)을 더한다.

실행:
  python monitor.py --market kr --loop-until 15:35 --interval 60
  python monitor.py --market us --loop-until 12:25 --interval 60
  python monitor.py --market us --universe full        # 단발, 순위를 더 깊게
  python monitor.py --market fut --loop-until 05:00 --interval 60   # 해외선물 (ET 기준)

선물은 순위 API 가 없어 볼 품목을 정해 두고 근월물을 매일 자동으로 찾는다.
쿨다운·이력은 월물이 아니라 품목(ES, GC …) 단위로 걸어 롤오버에 흔들리지 않게 한다.
"""
from __future__ import annotations
import argparse, json, os, re, statistics, time, datetime as dt
from collections import deque
from zoneinfo import ZoneInfo
import requests

KST, ET = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
STATE_DIR = os.environ.get("STATE_DIR", "state")
# 정규장 길이(분). 주식은 390분, 해외선물(CME Globex)은 일 23시간.
# 순간 변동 임계를 √(구간/세션) 로 환산할 때 쓰므로 시장마다 맞아야 한다.
SESSION_MIN = 390
SESSION_OF = {"kr": 390, "us": 390, "fut": 23 * 60}
MARKETS = ("kr", "us", "fut")


def env(k: str, d: str = "") -> str:
    return (os.environ.get(k) or d).strip()


def log(*a):
    print(dt.datetime.now(KST).strftime("[%H:%M:%S KST]"), *a, flush=True)


CFG = {
    "Z_THRESHOLD":   float(env("Z_THRESHOLD", "3.0")),    # 일간 변동성 대비 몇 σ 부터 알릴지
    "MIN_ABS_PCT":   float(env("MIN_ABS_PCT", "4.0")),    # z 와 별개로 최소 절대 등락률(%)
    "VOL_RATIO_MIN": float(env("VOL_RATIO_MIN", "1.8")),  # 같은 시각 기준 평소 대비 거래량 배수
    "MAX_ALERTS":    int(env("MAX_ALERTS", "4")),         # 1회 스캔당 알림 상한
    "MAX_PER_HOUR":  int(env("MAX_PER_HOUR", "8")),       # 시간당 상한 (루프 폭주 방어)
    "MAX_PER_DAY":   int(env("MAX_PER_DAY", "40")),       # 하루 상한 (API 비용 방어)
    "COOLDOWN_MIN":  int(env("COOLDOWN_MIN", "120")),     # 같은 종목 재알림 금지 시간(분)
    "SEARCH_PER_NAME": int(env("SEARCH_PER_NAME", "2")),
    "MODEL":         env("ATTRIB_MODEL", "claude-sonnet-5"),
    "MAX_TOKENS":    int(env("ATTRIB_MAX_TOKENS", "8000")),
    "BURST_MIN":     int(env("BURST_MIN", "5")),          # 순간 변동 측정 구간(분)
    "BURST_Z":       float(env("BURST_Z", "3.5")),        # 그 구간 기준 z 임계
    "BURST_MIN_PCT": float(env("BURST_MIN_PCT", "2.0")),  # 순간 변동 최소 절대폭(%)
    "RANK_N":        int(env("RANK_N", "20")),            # 순위에서 가져올 종목 수 (방향·거래소별)
    "SURGE_N":       int(env("SURGE_N", "10")),           # 거래량급증 순위에서 가져올 수
    "TRACK_MAX":     int(env("TRACK_MAX", "40")),         # 순위에서 빠진 뒤에도 계속 볼 종목 수
    "VOL_RANG":      env("VOL_RANG", "3"),                # 해외 순위 거래량 조건 (3=1만주 이상)
    "WORKERS":       int(env("WORKERS", "4")),            # 개별 현재가 동시 조회 수
    "FUT_ROLL_DAYS": int(env("FUT_ROLL_DAYS", "4")),      # 잔존일수가 이보다 적으면 다음 월물로
    # 공개 저장소의 Actions 로그는 누구나 읽을 수 있다. 무엇을 보고 있는지 감추려면 1.
    "QUIET_LOG":     env("QUIET_LOG", "0") == "1",

    # ── 모의투자 반자동 체결 ──────────────────────────────────────────
    # 시세는 실전 키로 받고 주문만 모의 도메인으로 보낸다 (paper.py 참고).
    # PAPER_TRADING=1 이고 --market kr 일 때만 버튼이 붙는다. 미국은 자는
    # 시간이라 승인이 불가능하므로 전수 로깅만 한다.
    "PAPER":            env("PAPER_TRADING", "0") == "1",
    "PAPER_ORDER_KRW":  int(env("PAPER_ORDER_KRW", "10000000")),
    "APPROVE_TTL_MIN":  int(env("APPROVE_TTL_MIN", "15")),
    "HOLD_DAYS":        int(env("HOLD_DAYS", "5")),      # 시간청산: T+N 영업일
    "STOP_SD":          float(env("STOP_SD", "1.5")),    # 손절: 진입가 -N×일간σ
    # 익절: 진입가 +N%. 0 이면 끄고 시간청산·손절만 쓴다. 어느 수준이 맞는지는
    # 아직 모르므로, backfill 이 여러 수준을 동시에 시뮬레이션해 근거를 만든다.
    "TAKE_PROFIT_PCT":  float(env("TAKE_PROFIT_PCT", "5.0")),
    # 상한가·하한가는 반대 호가가 없어 실전에서 체결이 안 된다. 승인해도
    # 주문을 내지 않는다. 국내 가격제한폭이 ±30% 라 29.5 를 기준으로 둔다.
    "PRICE_LIMIT_PCT":  float(env("PRICE_LIMIT_PCT", "29.5")),
    # 주문 접수 후 잔고에 잡히기까지의 유예(초). 이 안에 잔고에 없어도
    # 체결 대기로 보고 장부를 지우지 않는다.
    "SETTLE_GRACE_SEC": float(env("SETTLE_GRACE_SEC", "600")),
    "MAX_POSITIONS":    int(env("MAX_POSITIONS", "5")),
    # 왕복 거래비용(%) — 증권거래세 0.15% + 수수료 + 급변 종목 슬리피지 가정.
    # 모의투자는 슬리피지 없이 체결되므로 빼 줘야 실전에 가까운 수익률이 된다.
    "ROUND_TRIP_PCT":   float(env("ROUND_TRIP_PCT", "0.55")),
}

# KIS 거래량급증 순위의 MINX 코드 (0:1분전 … 9:120분전)
MINX = {1: "0", 2: "1", 3: "2", 5: "3", 10: "4", 15: "5", 20: "6", 30: "7", 60: "8", 120: "9"}

# 미국에서 순위와 무관하게 항상 보는 기본 목록. 대형주는 8% 급락해도 등락률 순위
# 상위에는 못 드는 날이 많아, 순위만 보면 정작 중요한 것을 놓친다.
US_DEFAULT_WATCH = (
    "SPY,QQQ,IWM,DIA,TLT,HYG,GLD,SLV,USO,UNG,XLE,XLK,XLV,XLF,XLI,XLY,XLP,XLU,XLB,"
    "SMH,SOXX,ARKK,"
    # 유료 CME 선물을 대신하는 무료 대용물. 선물만큼 정밀하진 않아도
    # "오늘 이 원자재가 크게 움직였다"는 신호로는 충분하다.
    "CPER,CORN,WEAT,SOYB,DBA,IEF,UUP,"
    "AAPL,MSFT,NVDA,GOOGL,AMZN,META,AVGO,TSLA,LLY,JPM,V,MA,XOM,UNH,COST,WMT,NFLX,"
    "AMD,MU,INTC,QCOM,TSM,ASML,AMAT,LRCX,KLAC,ARM,SMCI,PLTR,COIN,MSTR,"
    # 메모리 — 커버리지 핵심. 낸드·HDD 까지 함께 본다
    "SNDK,STX,WDC,"
    # 네트워크·광통신·아날로그
    "MRVL,ANET,LITE,CIEN,TSEM,SMTC,SWKS,"
    # 소프트웨어·보안
    "CRM,SNOW,U,DOCU,OKTA,TEAM,NTNX,CRWD,PANW,"
    # 헬스케어
    "JNJ,MRK,ABBV,NVS,MRNA,NTRA,HUM,HALO,ROIV,RVMD,IQV,AVTR,CORT,PSNL"
)

SYSTEM = """당신은 장중 급등락 원인을 즉시 파악해 트레이더에게 보고하는 분석가다.

[가장 중요한 규칙]
웹 검색으로 확인한, 날짜·시각이 특정되는 사실만 원인으로 제시한다.
확인되는 사건이 없으면 cause 에 정확히 "확인된 촉발 사건 없음" 이라고 쓴다.
장중 급등락은 원인이 아직 보도되지 않은 경우가 흔하므로 이것은 정상적인 결과다.
없는 사건을 지어내는 것이 이 작업에서 가장 큰 오류다.

[금지]
- "매수세 유입", "투자심리 개선", "수급 개선" 같은 동어반복 금지. 주가가 올랐다는 사실은 원인이 아니다.
- 목표주가·매매의견·향후 전망 금지. 원인 설명만 한다.
- 확인하지 못한 실적·계약·규제 내용을 추정해 쓰지 마라.

[출력] 아래 JSON 객체 하나만. 코드블록이나 설명 문장 금지.
{"items":[{"ticker":"...","cause":"...","context":"...",
"sources":[{"title":"...","url":"...","date":"YYYY-MM-DD"}],
"confidence":"high"|"medium"|"low"}]}
cause 는 1~2문장, context 는 그 종목의 사업·테마 배경 1문장. 한국어로 쓴다.

[JSON 을 깨뜨리지 않기 위한 규칙]
문자열 값 안에 큰따옴표(")를 절대 쓰지 마라. 인용이 필요하면 작은따옴표를 써라.
줄바꿈도 넣지 마라. 각 값은 한 줄로 이어 쓴다."""


# ── 상태 (GitHub Actions 캐시로 실행 간 유지) ─────────────────────────────
def load_state(market: str) -> dict:
    # 종목명 캐시는 날짜와 무관하게 누적된다 (이름은 날마다 바뀌지 않는다).
    # 알림 상태는 자정에 리셋되지만 여기에 얹혀 가면 같이 날아가므로 별도 파일.
    if not _NAMES:
        load_names()
    p = os.path.join(STATE_DIR, f"alerts_{market}.json")
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    try:
        d = json.load(open(p))
        if d.get("date") == today:
            d.setdefault("sent", {}); d.setdefault("log", [])
            return d
    except Exception:
        pass
    return {"date": today, "sent": {}, "log": []}


def save_state(market: str, st: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        json.dump(st, open(os.path.join(STATE_DIR, f"alerts_{market}.json"), "w"))
    except Exception as e:
        log(f"상태 저장 실패: {str(e)[:80]}")
    save_names()


def on_cooldown(st: dict, key: str) -> bool:
    t = st["sent"].get(key)
    return bool(t) and (time.time() - t) < CFG["COOLDOWN_MIN"] * 60


def budget_left(st: dict, ignore_hourly: bool = False) -> int:
    """이번 스캔에서 보낼 수 있는 알림 수. 시간당·일일 상한을 함께 적용한다.
    1분 폴링에서는 상한이 없으면 변동성 큰 날 API 비용이 그대로 늘어난다.

    ignore_hourly 는 수동 테스트용이다. 설정 중에 몇 번 돌려보면 시간당 상한에
    걸려 '알림 0건'만 나오는데, 그러면 정작 확인하려던 것을 확인할 수 없다.
    일일 상한은 그대로 지키므로 비용은 여전히 묶여 있다."""
    now = time.time()
    st["log"] = [t for t in st.get("log", []) if now - t < 86400]
    hour = sum(1 for t in st["log"] if now - t < 3600)
    caps = [CFG["MAX_ALERTS"], CFG["MAX_PER_DAY"] - len(st["log"])]
    if not ignore_hourly:
        caps.append(CFG["MAX_PER_HOUR"] - hour)
    return max(0, min(caps))


# ── 장 시간 ──────────────────────────────────────────────────────────────
def market_open(market: str) -> bool:
    if market == "fut":
        # CME Globex: 일 18:00 ET 개장 ~ 금 17:00 ET 폐장, 매일 17:00~18:00 ET 정비.
        now = dt.datetime.now(ET)
        wd, hm = now.weekday(), now.hour * 60 + now.minute
        if wd == 5:                                    # 토요일 종일 휴장
            return False
        if wd == 6:                                    # 일요일은 18:00 ET 부터
            return hm >= 18 * 60
        if wd == 4 and hm >= 17 * 60:                  # 금요일 17:00 ET 이후 휴장
            return False
        return not (17 * 60 <= hm < 18 * 60)           # 평일 정비시간 제외
    now = dt.datetime.now(ET if market == "us" else KST)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= hm <= (16 * 60) if market == "us" else (9 * 60) <= hm <= (15 * 60 + 30)


def elapsed_frac(market: str) -> float:
    """정규장 경과 비율. 거래량을 '같은 시각의 평소'와 견주기 위한 것.

    해외선물은 18:00 ET 에 하루가 넘어가는 23시간 세션이라 '경과 비율'이 자정을
    걸쳐 꼬이기 쉽다. 잘못 계산하느니 일중 누적 거래량비는 쓰지 않는다 (0 을 돌려
    NaN 이 되게 한다). 근월물은 유동성이 충분해 '호가 공백' 걱정이 적고, 순간 구간
    거래량비는 세션 길이만 있으면 되므로 그쪽은 그대로 작동한다."""
    if market == "fut":
        return 0.0
    now = dt.datetime.now(ET if market == "us" else KST)
    open_min = (9 * 60 + 30) if market == "us" else 9 * 60
    return min(max((now.hour * 60 + now.minute - open_min) / SESSION_OF[market], 0.02), 1.0)


# ── 체결 이력 (KIS 는 스냅샷만 주므로 순간변동은 직접 쌓아 구한다) ──────────
_HIST: dict[str, deque] = {}


def push_hist(ticker: str, px: float, vol: float | None = None, ts: float | None = None):
    if not px or px <= 0:
        return
    _HIST.setdefault(ticker, deque(maxlen=400)).append((ts or time.time(), px, vol))


def save_hist(market: str, keep: int = 8):
    """체결 이력을 실행 간에 넘긴다. 30분 단발 스캔은 프로세스가 매번 새로 뜨므로
    이력을 저장해 두지 않으면 순간 변동 경로가 아예 작동하지 않는다."""
    os.makedirs(STATE_DIR, exist_ok=True)
    d = {"date": dt.datetime.now(KST).strftime("%Y-%m-%d"),
         "h": {k: list(v)[-keep:] for k, v in _HIST.items() if k.startswith(f"{market}:")}}
    try:
        json.dump(d, open(os.path.join(STATE_DIR, f"hist_{market}.json"), "w"))
    except Exception as e:
        log(f"이력 저장 실패: {str(e)[:80]}")


def load_hist(market: str):
    p = os.path.join(STATE_DIR, f"hist_{market}.json")
    try:
        d = json.load(open(p))
    except Exception:
        return
    if d.get("date") != dt.datetime.now(KST).strftime("%Y-%m-%d"):
        return                             # 어제 이력으로 오늘의 순간변동을 재면 안 된다
    for k, pts in (d.get("h") or {}).items():
        _HIST[k] = deque([tuple(x) for x in pts], maxlen=400)


def hist_burst(ticker: str, minutes: int):
    """최근 `minutes` 분 변동률·구간거래량·실제 구간길이(분).

    폴링 간격이 일정하지 않으므로 기준점은 요청 구간보다 조금 더 과거일 수 있다.
    그 오차를 임계값 쪽에서 흡수하도록 '실제' 구간길이를 함께 돌려준다.
    이력이 부족하면 (None, None, None)."""
    d = _HIST.get(ticker)
    if not d or len(d) < 2:
        return None, None, None
    t_end, p_end, v_end = d[-1]
    cut = t_end - minutes * 60
    base = None
    for t, p, v in d:                      # cut 이전 마지막 점을 기준으로 삼는다
        if t <= cut:
            base = (t, p, v)
        else:
            break
    if base is None or base[1] <= 0 or base[0] >= t_end:
        return None, None, None
    ret = p_end / base[1] - 1
    dv = (v_end - base[2]) if (v_end is not None and base[2] is not None and v_end >= base[2]) else None
    return ret, dv, (t_end - base[0]) / 60.0


# ── KIS ──────────────────────────────────────────────────────────────────
_KIS = {"api": None}


def kis_api():
    if _KIS["api"] is None:
        from kis import KIS
        k, s = env("KIS_APP_KEY"), env("KIS_APP_SECRET")
        if not (k and s):
            return None
        _KIS["api"] = KIS(k, s, paper=env("KIS_PAPER", "0") == "1",
                          state_dir=STATE_DIR, log=log)
    return _KIS["api"]


# ── 참조 데이터 (전일종가·일간변동성·평균거래량) ───────────────────────────
_REF: dict[str, dict | None] = {}      # "kr:005930" / "us:NVDA" → {...}


def ref_of(api, market: str, code: str, excd: str | None = None,
           symbol: str | None = None) -> dict | None:
    """최근 20영업일 일간수익률 표준편차와 평균거래량. 하루에 한 번만 받으면 된다."""
    key = f"{market}:{code}"
    if key in _REF:
        return _REF[key]
    if market == "kr":
        rows = api.daily(code)
    elif market == "fut":
        rows = api.fut_daily(symbol or code, excd or "CME")
    else:
        rows = api.daily_os(excd or "NAS", code)
    rows = sorted((r for r in rows if r.get("close", 0) > 0),
                  key=lambda r: str(r.get("date") or ""), reverse=True)
    today = dt.datetime.now(KST if market == "kr" else ET).strftime("%Y%m%d")
    if rows and str(rows[0].get("date")) == today:
        rows = rows[1:]                # 오늘 미완성 봉을 넣으면 지금 잡으려는 급등이
    rows = rows[:21]                   # 제 자신의 σ 를 키워 z 를 눌러버린다 (순환 참조)
    if len(rows) < 10:
        _REF[key] = None
        return None
    px = [r["close"] for r in rows]
    vs = [r.get("volume") or 0.0 for r in rows]
    rets = [px[i] / px[i + 1] - 1 for i in range(len(px) - 1)]   # 최신순이라 역순 비율
    sd = statistics.pstdev(rets)
    _REF[key] = {"sd": sd if sd > 0 else None, "prev": px[0],
                 "avg_vol": sum(vs) / len(vs) if vs else 0.0}
    return _REF[key]


def build_rows(api, market: str, cand: dict) -> list[dict]:
    """후보 스냅샷 → 판정용 행. 이력을 쌓고 z·거래량비·순간변동을 채운다."""
    frac, bm, smin = elapsed_frac(market), CFG["BURST_MIN"], SESSION_OF[market]
    rows = []
    for code, m in cand.items():
        last = float(m.get("last") or 0.0)
        vol = float(m.get("volume") or 0.0)
        push_hist(f"{market}:{code}", last, vol)
        rf = ref_of(api, market, code, m.get("excd") or m.get("exch"),
                    m.get("symbol")) or {}
        sd, av = rf.get("sd"), rf.get("avg_vol") or 0.0
        pct = float(m.get("chg_pct") or 0.0)
        # 이름은 캐시를 거친다. 국내는 순위에서 빠지면 현재가 응답에 이름이
        # 없어, 이걸 안 하면 알림에 종목코드만 찍힌다.
        row = {"ticker": code,
               "name": (name_of(code, m.get("name")) if market == "kr"
                        else (m.get("name") or code)),
               "excd": m.get("excd"),
               "last": last, "pct": pct, "sd_daily": sd, "session_min": smin,
               "value": float(m.get("value") or 0.0),
               "z": (pct / 100) / sd if sd else 0.0,
               "vol_ratio": (vol / max(av * frac, 1.0)) if (av and frac > 0) else float("nan")}
        b, bv, span = hist_burst(f"{market}:{code}", bm)
        if b is not None:
            row["burst"], row["burst_min"] = b, span
            if bv is not None and av and span > 0:
                # avg_vol / 세션길이 = 분당 평소 거래량. 세션 경과 비율은 필요 없다.
                row["burst_vol_ratio"] = bv / max(av * span / smin, 1.0)
        rows.append(row)
    return rows


def fetch_many(fn, items: list) -> list:
    """개별 현재가를 동시에 받는다. 서울-깃허브 왕복이 건당 0.2초쯤이라 순차로 돌면
    60초 안에 수십 종목을 못 본다. 유량제한은 KIS 래퍼가 락으로 지키므로 안전하다."""
    if not items:
        return []
    if CFG["WORKERS"] <= 1:
        return [r for r in (fn(x) for x in items) if r]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=CFG["WORKERS"]) as ex:
        return [r for r in ex.map(fn, items) if r]


_TRACK: dict[str, dict[str, float]] = {"kr": {}, "us": {}, "fut": {}}

# ── 종목명 캐시 ──────────────────────────────────────────────────────────
# 이름은 엔드포인트마다 실려 오기도 하고 안 오기도 한다. 국내 등락률 순위에는
# 붙어 오지만, 순위에서 빠진 뒤 현재가로만 재조회되는 종목은 이름이 없다.
# 그래서 알림에 종목코드만 찍히는 일이 생긴다 — 한 번 알아낸 이름은 보관한다.
_NAMES: dict[str, str] = {}
_NAMES_DIRTY = [False]


def _names_path() -> str:
    return os.path.join(STATE_DIR, "kr_names.json")


def load_names() -> None:
    try:
        d = json.load(open(_names_path()))
        if isinstance(d, dict):
            _NAMES.update({str(k): str(v) for k, v in d.items() if v})
    except Exception:
        pass


def save_names() -> None:
    if not _NAMES_DIRTY[0]:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        # 무한히 자라지 않게 상한을 둔다. 종목명은 건당 수십 바이트라
        # 넉넉히 잡아도 캐시가 가볍다.
        items = list(_NAMES.items())[-4000:]
        json.dump(dict(items), open(_names_path(), "w"), ensure_ascii=False)
        _NAMES_DIRTY[0] = False
    except Exception as e:
        log(f"종목명 캐시 저장 실패 (무시하고 진행): {type(e).__name__}")


def remember_name(code: str, name: str | None) -> None:
    """이름을 알아냈으면 보관. 코드와 같은 값은 이름이 아니므로 버린다."""
    n = str(name or "").strip()
    if not n or n == code or n == code.lstrip("0"):
        return
    if _NAMES.get(code) != n:
        _NAMES[code] = n
        _NAMES_DIRTY[0] = True


def name_of(code: str, fresh: str | None = None) -> str:
    """표시용 이름. 방금 받은 것 → 캐시 → (없으면) 코드."""
    remember_name(code, fresh)
    return _NAMES.get(code) or str(fresh or "").strip() or code


def fill_names(api, cand: dict, budget: int = 6) -> None:
    """이름이 비어 있는 종목을 종목 마스터로 채운다.

    한 회차에 budget 개까지만 — 이름 때문에 시세 조회 한도를 쓰면 본말전도다.
    한 번 채우면 캐시에 남으므로, 새 종목이 등장한 회차에만 실제로 호출된다."""
    todo = [c for c, m in cand.items()
            if not (m.get("name") or _NAMES.get(c))][:budget]
    if not todo or not hasattr(api, "stock_name"):
        return
    got = 0
    for c in todo:
        try:
            nm = api.stock_name(c)
        except Exception as e:
            log(f"종목명 조회 실패 [{c}]: {type(e).__name__} {str(e)[:60]}")
            break                       # 한 번 막히면 이 회차는 더 시도하지 않는다
        if nm:
            remember_name(c, nm); got += 1
    if got:
        log(f"종목명 {got}건 보충 (캐시 {len(_NAMES)}건)")


def _sticky(market: str, cand: dict, depth: int) -> list[str]:
    """한 번이라도 눈에 띈 종목은 순위에서 빠진 뒤에도 계속 본다.
    그래야 순위에 갓 진입한 종목도 다음 스캔부터는 순간급변을 판정할 수 있다."""
    now_t = time.time()
    for c in cand:
        _TRACK[market][c] = now_t
    return sorted((c for c in _TRACK[market] if c not in cand),
                  key=lambda c: -_TRACK[market][c])[:depth]


def watchlist(market: str) -> list[str]:
    if market == "kr":
        return [x.strip().zfill(6) for x in env("KR_WATCHLIST").split(",") if x.strip()]
    if market == "fut":
        return [x.strip().upper() for x in env("FUT_ROOTS", FUT_DEFAULT_ROOTS).split(",") if x.strip()]
    return [x.strip().upper() for x in env("US_WATCHLIST", US_DEFAULT_WATCH).split(",") if x.strip()]


# ── 해외선물 ─────────────────────────────────────────────────────────────
# 선물은 순위 API 가 없다. 볼 품목을 정해 두고 근월물을 매일 자동으로 찾는다.
# CME 계열(ES·NQ·CL·GC·ZC …)은 서브거래소마다 월 $228.8 — 네 곳 다 쓰면 월 $915 다.
# 이 모니터가 쓸 만한 금액이 아니라서 **시세료 0원인 ICE 거래소 품목만** 기본으로 둔다.
# 빠지는 것(지수·금리·곡물·구리·천연가스)은 미국 ETF 로 대신 본다 — 해외주식 시세는 무료다.
# CME 를 신청하셨다면 FUT_ROOTS 에 ES,NQ,ZN 처럼 넣으면 그대로 동작한다.
FUT_DEFAULT_ROOTS = "DX,BRN,WBS,MEM,SB,KC,CC"
FUT_LABEL = {
    # ICE — 시세료 0원
    "DX": "달러인덱스", "BRN": "브렌트유", "WBS": "WTI원유", "GAS": "가스오일",
    "MEM": "MSCI신흥국", "FTS": "FTSE100", "SB": "설탕", "KC": "커피", "CC": "코코아",
    "CT": "면화", "OJ": "오렌지주스", "YG": "미니금", "YS": "미니은", "LG": "영국국채",
    # CME 계열 — 유료 신청 시에만
    "ES": "S&P500", "NQ": "나스닥100", "RTY": "러셀2000", "YM": "다우",
    "CL": "WTI원유", "NG": "천연가스", "RB": "휘발유", "HO": "난방유", "BZ": "브렌트",
    "GC": "금", "SI": "은", "HG": "구리", "PL": "백금", "PD": "팔라듐",
    "ZC": "옥수수", "ZS": "대두", "ZW": "밀", "ZL": "대두유", "ZM": "대두박",
    "ZN": "미국10년물", "ZB": "미국30년물", "ZF": "미국5년물", "ZT": "미국2년물",
    "6E": "유로", "6J": "엔", "6A": "호주달러", "6B": "파운드", "KRW": "원화"}
MONTH_CODE = "FGHJKMNQUVXZ"          # 1~12월 선물 월물코드


def fut_candidates(root: str, months: int = 14) -> list[str]:
    """이번 달부터 앞으로 N개월치 종목코드 후보 (ROOT + 월물코드 + 2자리연도).
    품목마다 상장월이 달라 표를 들고 있기보다, 가까운 월부터 실제로 조회해 본다."""
    now = dt.datetime.now(ET)
    out = []
    for i in range(months):
        m = now.month + i
        y, m = now.year + (m - 1) // 12, (m - 1) % 12 + 1
        out.append(f"{root}{MONTH_CODE[m - 1]}{y % 100:02d}")
    return out


def fut_symbols(api) -> dict[str, dict]:
    """{root: {symbol, exch}}. 하루에 한 번만 찾고 state/ 에 캐시한다.
    롤오버로 종목이 바뀌면 그 품목의 체결 이력을 버린다 — 월물이 달라지면 가격
    수준이 점프해서, 이력을 이어 두면 롤을 '순간 급변'으로 오인한다."""
    p = os.path.join(STATE_DIR, "fut_symbols.json")
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    try:
        c = json.load(open(p))
    except Exception:
        c = {}
    prev = dict(c.get("map") or {})
    if c.get("date") == today:
        if prev:
            return prev
        if c.get("failed"):
            # 오늘 이미 권한/코드 문제로 실패한 것이 확인됐다. 60초 루프에서 매 분
            # 다시 두드리면 하루 수천 번을 헛돌고 KIS 유량만 잡아먹는다.
            return {}
    out, roll = {}, []
    roots = watchlist("fut")
    for n, root in enumerate(roots):
        cands = fut_candidates(root)
        for sym in cands:
            q = api.fut_price(sym)
            if q and q.get("remain_days", 0) >= CFG["FUT_ROLL_DAYS"]:
                out[root] = {"symbol": sym, "exch": q.get("exch") or "CME"}
                if prev.get(root, {}).get("symbol") not in (None, sym):
                    roll.append(root)
                break
        # 첫 품목이 한 달도 안 잡히면 나머지 12개를 더 두드려 봐야 결과는 같다.
        # 180번을 헛돌며 2분을 쓰는 대신, 바로 멈추고 원인을 물어본다.
        if n == 0 and not out:
            log(f"'{root}' 근월물을 하나도 못 찾았습니다 — 나머지 품목 조회를 건너뛰고 원인을 확인합니다")
            probe = cands[:4] + (fut_candidates(roots[1])[:4] if len(roots) > 1 else [])
            api.fut_probe(probe)
            os.makedirs(STATE_DIR, exist_ok=True)
            try:                      # 오늘은 더 시도하지 않는다고 기록
                json.dump({"date": today, "map": {}, "failed": True}, open(p, "w"))
            except Exception:
                pass
            return {}
    for root in roll:                  # 롤오버된 품목은 이력·참조데이터를 초기화
        _HIST.pop(f"fut:{root}", None)
        _REF.pop(f"fut:{root}", None)
    if roll:
        log(f"선물 롤오버 {len(roll)}개 품목 → 해당 이력 초기화")
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        json.dump({"date": today, "map": out}, open(p, "w"))
    except Exception:
        pass
    log(f"선물 근월물 {len(out)}/{len(watchlist('fut'))}개 확정")
    return out


_DELAY_PROBED = [False]
_FUT_WARNED = [False]       # 권한 경고는 실행당 한 번만


def probe_delay(q: dict):
    """선물 시세가 실시간인지 지연인지 직접 잰다. 문서에 명시가 없어 추측하지 않는다.
    proc_date/proc_time 이 한국시간인지 현지시간인지도 불명이라 양쪽으로 재 본다."""
    if _DELAY_PROBED[0] or not (q.get("proc_date") and q.get("proc_time")):
        return
    _DELAY_PROBED[0] = True
    raw = f"{q['proc_date']} {q['proc_time']}"
    try:
        t = dt.datetime.strptime(q["proc_date"] + q["proc_time"].zfill(6), "%Y%m%d%H%M%S")
    except ValueError:
        log(f"선물 최종처리일시 형식 불명: {raw}"); return
    best = min((("KST", (dt.datetime.now(KST) - t.replace(tzinfo=KST)).total_seconds() / 60),
                ("ET", (dt.datetime.now(ET) - t.replace(tzinfo=ET)).total_seconds() / 60)),
               key=lambda x: abs(x[1]))
    log(f"선물 시세 지연 측정: 최종처리 {raw} → {best[0]} 기준 {best[1]:+.0f}분. "
        f"{'실시간으로 보입니다' if abs(best[1]) <= 3 else '지연 시세입니다 — 순간급변 탐지는 그만큼 늦습니다'}")


def scan_fut(universe: str = "focus") -> list[dict]:
    api = kis_api()
    if api is None:
        log("KIS_APP_KEY/SECRET 미설정 → 선물 스캔 생략"); return []
    syms = fut_symbols(api)
    if not syms:
        if not _FUT_WARNED[0]:        # 루프에서 매 분 같은 줄을 찍지 않는다
            _FUT_WARNED[0] = True
            log("선물 근월물을 찾지 못했습니다 — 해외선물 시세 이용 권한을 확인하세요. "
                "위 'KIS 오류' 줄의 msg_cd 가 원인입니다 "
                "(EGW00550 = CME 거래소 시세 신청이 안 된 계좌)")
        return []

    def one(item):
        root, info = item
        q = api.fut_price(info["symbol"])
        if not q:
            return None
        # 쿨다운·이력은 월물이 아니라 '품목' 단위로 건다. 롤오버로 초기화되지 않게.
        q["code"], q["exch"] = root, info["exch"]
        q["name"] = FUT_LABEL.get(root, root)
        q["symbol"] = info["symbol"]
        return q

    quotes = fetch_many(one, sorted(syms.items()))
    if quotes:
        probe_delay(quotes[0])
    return build_rows(api, "fut", {q["code"]: q for q in quotes})


def scan_kr(universe: str = "focus") -> list[dict]:
    api = kis_api()
    if api is None:
        log("KIS_APP_KEY/SECRET 미설정 → 한국 스캔 생략"); return []
    mult = 2 if universe == "full" else 1
    cand = {}
    for ud in ("0", "1"):                  # 0=상승률, 1=하락률
        for m in api.movers(ud, limit=CFG["RANK_N"] * mult):
            cand.setdefault(m["code"], m)
    # 순위 응답에는 이름이 붙어 온다. 여기서 거둬 두면, 이 종목이 나중에
    # 순위에서 빠져 현재가로만 조회될 때도 이름을 잃지 않는다.
    for c, m in cand.items():
        remember_name(c, m.get("name"))
    need = [c for c in watchlist("kr") + _sticky("kr", cand, CFG["TRACK_MAX"] * mult)
            if c not in cand]
    for p in fetch_many(api.price, need):
        cand[p["code"]] = p
    if not cand:
        log("KIS 등락률 순위 응답 없음 — 관심종목 설정이나 API 권한을 확인하세요")
        return []
    fill_names(api, cand)               # 그래도 비어 있으면 종목 마스터로 보충
    return build_rows(api, "kr", cand)


def _excd_cache() -> dict:
    p = os.path.join(STATE_DIR, "us_excd.json")
    try:
        return json.load(open(p))
    except Exception:
        return {}


def _save_excd(c: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        json.dump(c, open(os.path.join(STATE_DIR, "us_excd.json"), "w"))
    except Exception:
        pass


def scan_us(universe: str = "focus") -> list[dict]:
    from kis import US_EXCHANGES
    api = kis_api()
    if api is None:
        log("KIS_APP_KEY/SECRET 미설정 → 미국 스캔 생략"); return []
    mult = 2 if universe == "full" else 1
    minx = MINX.get(CFG["BURST_MIN"], "3")
    cand = {}
    for ex in US_EXCHANGES:
        for g in ("1", "0"):               # 해외는 1=상승율, 0=하락율 (국내와 반대)
            for m in api.movers_os(ex, g, limit=CFG["RANK_N"] * mult,
                                   vol_rang=CFG["VOL_RANG"]):
                cand.setdefault(m["code"], m)
        # KIS 가 '최근 N분 거래량이 급증한 종목'을 직접 골라준다. 순간급변 후보로 쓴다.
        for m in api.volume_surge_os(ex, minx=minx, limit=CFG["SURGE_N"] * mult,
                                     vol_rang=CFG["VOL_RANG"]):
            cand.setdefault(m["code"], m)
    cache = _excd_cache()
    need = [s for s in watchlist("us") + _sticky("us", cand, CFG["TRACK_MAX"] * mult)
            if s not in cand]
    # 거래소 탐색은 종목당 한 번뿐이지만 최대 3회 호출이라, 캐시에 없는 것만 먼저 순차로.
    for s in [s for s in need if s not in cache]:
        api.resolve_excd(s, cache)
    _save_excd(cache)
    for p in fetch_many(lambda s: api.price_os(cache[s], s),
                        [s for s in need if cache.get(s)]):
        cand[p["code"]] = p
    if not cand:
        log("해외 순위·현재가 응답 없음 — 해외주식 시세 이용 권한을 확인하세요")
        return []
    return build_rows(api, "us", cand)


# ── 선별 ─────────────────────────────────────────────────────────────────
def pick(rows: list[dict], st: dict, limit: int) -> list[dict]:
    """두 경로 중 하나라도 걸리면 알린다.
      (A) 일중 누적: 절대 등락률 + 일간변동성 대비 z + 거래량 동반
      (B) 순간 급변: 최근 N분 변동이 그 구간 기준으로 이례적 + 그 구간 거래량 동반"""
    out = []
    for r in rows:
        if on_cooldown(st, r["ticker"]):
            continue
        def ok(vr):
            return (not (vr is not None and vr == vr)) or vr >= CFG["VOL_RATIO_MIN"]

        z = r.get("z") or 0.0
        hit_day = (abs(r.get("pct", 0)) >= CFG["MIN_ABS_PCT"]
                   and abs(z) >= CFG["Z_THRESHOLD"] and ok(r.get("vol_ratio")))

        hit_burst = False
        b, sd_d = r.get("burst"), r.get("sd_daily")
        if b is not None and sd_d and abs(b) * 100 >= CFG["BURST_MIN_PCT"]:
            # 일간변동성을 '실제로 측정된' 구간 길이로 환산한다. 폴링이 밀려 구간이
            # 넓어지면 기대 변동폭도 함께 커져야 하고, 아니면 임계가 헐거워진다.
            # 세션 길이는 시장마다 다르다 (주식 390분, 해외선물 23시간).
            # 선물에 390분을 쓰면 임계가 1.9배 헐거워져 오탐이 쏟아진다.
            bmin = r.get("burst_min") or CFG["BURST_MIN"]
            sd_b = sd_d * (bmin / (r.get("session_min") or SESSION_MIN)) ** 0.5
            if sd_b > 0:
                r["z_burst"] = b / sd_b
                bvr = r.get("burst_vol_ratio")
                hit_burst = abs(r["z_burst"]) >= CFG["BURST_Z"] and ok(
                    bvr if bvr is not None else r.get("vol_ratio"))

        if hit_day or hit_burst:
            r["trigger"] = ("순간급변" if hit_burst and not hit_day
                            else ("급변+누적" if hit_burst else "누적"))
            out.append(r)
    # 지금 벌어지고 있는 것(순간급변)을 먼저, 각 그룹 안에서는 이례적인 순서로.
    # 순간 z 와 일간 z 는 같은 척도가 아니므로 섞어서 크기 비교하지 않는다.
    out.sort(key=lambda x: (0 if "급변" in x.get("trigger", "") else 1,
                            -abs((x.get("z_burst") if "급변" in x.get("trigger", "")
                                  else x.get("z")) or 0)))
    return out[:limit]


# ── 귀인 ─────────────────────────────────────────────────────────────────
def _attrib_client():
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        log("anthropic 미설치 → 귀인 생략"); return None
    ws = env("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(api_key=key,
                               default_headers={"anthropic-workspace-id": ws} if ws else None)


def attribute(picks: list[dict], market: str) -> dict:
    cli = _attrib_client()
    if cli is None or not picks:
        return {}
    now = dt.datetime.now(KST if market == "kr" else ET)
    mk = {"us": "미국 증시", "kr": "한국 증시", "fut": "해외선물(CME)"}[market]
    lines = [f"{mk} — 현재 {now:%Y-%m-%d %H:%M} ({'KST' if market=='kr' else 'ET'})", "",
             "아래 종목이 평소 변동성 대비 이례적으로 크게 움직이고 있다.",
             "각각의 원인을 웹 검색으로 확인하라. 오늘 날짜 기준 최신 뉴스를 우선한다.",
             "'순간급변' 표시가 있으면 바로 몇 분 사이에 움직인 것이므로",
             "방금 나온 속보·공시를 먼저 찾아라.", ""]
    if market == "fut":
        lines.insert(2, "종목은 CME 해외선물 근월물이다. 원자재·지수·금리·통화 선물이므로 "
                        "개별 기업 뉴스가 아니라 매크로 지표, 재고·수급 발표, 지정학, "
                        "중앙은행 발언 쪽에서 원인을 찾아라.")
    for r in picks:
        nm = r.get("name") or r["ticker"]
        lab = r["ticker"] if nm == r["ticker"] else f"{nm} ({r['ticker']})"
        vr = r.get("vol_ratio")
        vtxt = f", 거래량 평소의 {vr:.1f}배" if (vr is not None and vr == vr and vr) else ""
        b = r.get("burst")
        btxt = (f", 최근 {r.get('burst_min') or CFG['BURST_MIN']:.0f}분 {b*100:+.1f}%"
                if b is not None else "")
        lines.append(f"- {lab}  {r['pct']:+.1f}%  (일간변동성 대비 {r.get('z',0):+.1f}σ"
                     f"{btxt}{vtxt}) [{r.get('trigger','')}]")
    lines += ["", f'각 종목을 items 에 하나씩, ticker 는 "{picks[0]["ticker"]}" 형식 그대로 쓴다.',
              "확인되는 사건이 없으면 cause 를 \"확인된 촉발 사건 없음\" 으로 둔다."]
    try:
        msg = cli.messages.create(
            model=CFG["MODEL"], max_tokens=CFG["MAX_TOKENS"],
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": min(CFG["SEARCH_PER_NAME"] * len(picks), 12)}])
    except Exception as e:
        m = str(e)
        log(f"귀인 API 실패: {m[:400]}")
        if "anthropic-workspace-id" in m:
            log("  → 조직 범위 키입니다. ANTHROPIC_WORKSPACE_ID 를 설정하세요.")
        return {}
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"[<(]\s*/?\s*cite\b[^>)]*[>)]?", "", text).replace("</cite>", "")
    u = msg.usage
    log(f"귀인: 검색 {getattr(getattr(u,'server_tool_use',None),'web_search_requests',0) or 0}회, "
        f"토큰 in {getattr(u,'input_tokens',0):,} / out {getattr(u,'output_tokens',0):,}")
    items = parse_items(text, stop=getattr(msg, "stop_reason", None))
    return {i.get("ticker"): i for i in items if i.get("ticker")}


def _repair_json(raw: str) -> str:
    """모델이 흔히 내는 사소한 흠을 고친다. 내용은 건드리지 않는다."""
    s = re.sub(r",\s*([}\]])", r"\1", raw)          # 뒤따르는 쉼표
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    # 문자열 안에 그대로 들어온 줄바꿈 (JSON 에서는 불법)
    out, inside, esc = [], False, False
    for ch in s:
        if esc:
            out.append(ch); esc = False; continue
        if ch == "\\":
            out.append(ch); esc = True; continue
        if ch == '"':
            inside = not inside
        if inside and ch in "\r\n":
            out.append(" "); continue
        out.append(ch)
    return "".join(out)


def _salvage_items(raw: str) -> list[dict]:
    """JSON 으로 못 읽을 때 항목을 하나씩 긁어낸다.

    원인 분석을 다 받아 놓고 따옴표 하나 때문에 4건을 통째로 버리는 것보다,
    읽히는 만큼이라도 건지는 편이 낫다. 알림은 이미 나가기로 정해진 상태다."""
    out, marks = [], [m.start() for m in re.finditer(r'"ticker"\s*:', raw)]
    for i, st in enumerate(marks):
        seg = raw[st: marks[i + 1] if i + 1 < len(marks) else len(raw)]
        t = re.search(r'"ticker"\s*:\s*"([^"]+)"', seg)
        if not t:
            continue
        def grab(key):
            g = re.search(rf'"{key}"\s*:\s*"(.*?)"\s*(?:,\s*"|\}}|\]|$)', seg, re.S)
            return re.sub(r"\s+", " ", g.group(1)).strip() if g else ""
        it = {"ticker": t.group(1), "cause": grab("cause"), "context": grab("context"),
              "confidence": (re.search(r'"confidence"\s*:\s*"(\w+)"', seg) or [None, ""])[1]}
        it["sources"] = [{"url": u} for u in re.findall(r'"url"\s*:\s*"(https?://[^"]+)"', seg)[:2]]
        if it["cause"]:
            out.append(it)
    return out


def parse_items(text: str, stop=None) -> list[dict]:
    """모델 출력에서 items 를 꺼낸다. 깨끗한 JSON → 수선 → 부분 회수 순으로 시도."""
    t = re.sub(r"```(?:json)?", "", text).strip()
    mm = re.search(r"\{.*\}", t, re.S)
    if not mm:
        log(f"귀인 응답에 JSON 없음 (stop={stop}) 앞부분: {t[:120]!r}")
        return []
    raw = mm.group(0)
    for cand in (raw, _repair_json(raw)):
        try:
            got = json.loads(cand).get("items", [])
            if isinstance(got, list):
                return got
        except json.JSONDecodeError as e:
            err, pos = e, e.pos
    salvaged = _salvage_items(raw)
    # 무엇 때문에 깨졌는지 보이도록 문제 지점 주변을 남긴다
    log(f"귀인 JSON 파싱 실패: {err} — 부분 회수 {len(salvaged)}건. "
        f"문제 부근: {raw[max(0, pos - 60):pos + 60]!r}")
    return salvaged


# ── 전수 로깅 ────────────────────────────────────────────────────────────
# 승인 여부와 무관하게 **모든 알림**을 남긴다. 반자동으로 체결된 것만 보면
# 그것은 고른 표본이라, "이 신호에 엣지가 있나" 에는 답할 수 없다. 나중에
# backfill.py 가 여기에 T+1·T+5 수익률을 채워 넣는다.
def _num(x):
    """NaN/inf 는 JSON 으로 못 쓴다 (json.dumps 가 표준 위반 문자열을 뱉는다)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def log_signals(picks: list[dict], attrib: dict, market: str) -> None:
    now = dt.datetime.now(KST if market == "kr" else ET)
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(os.path.join(STATE_DIR, "signals.jsonl"), "a", encoding="utf-8") as f:
            for r in picks:
                a = attrib.get(r["ticker"], {})
                f.write(json.dumps({
                    "ts": int(time.time()), "date": now.strftime("%Y%m%d"),
                    "hhmm": now.strftime("%H:%M"), "market": market,
                    "ticker": r["ticker"], "name": r.get("name"), "excd": r.get("excd"),
                    "px": _num(r.get("last")), "pct": _num(r.get("pct")),
                    "z": _num(r.get("z")), "sd_daily": _num(r.get("sd_daily")),
                    "burst": _num(r.get("burst")), "burst_min": _num(r.get("burst_min")),
                    "vol_ratio": _num(r.get("vol_ratio")),
                    # 거래대금(원/달러). 소형주를 사후에 걸러내려면 이게 있어야 한다.
                    "value": _num(r.get("value")),
                    "trigger": r.get("trigger"), "cause": a.get("cause"),
                    "confidence": a.get("confidence"),
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"신호 로깅 실패: {str(e)[:80]}")


# ── 모의투자 반자동 ──────────────────────────────────────────────────────
_PAPER: dict = {}


def paper_on(market: str) -> bool:
    """한국장에서만 버튼을 단다. 미국 알림이 오는 시간엔 주무시므로 승인이
    불가능하고, 만료만 쌓여 데이터가 지저분해진다."""
    return bool(CFG["PAPER"]) and market == "kr"


def _paper_mod():
    """paper.py 를 불러온다. 없으면 None.

    파일을 손으로 올리다 보면 monitor.py 만 갱신하고 paper.py 를 빠뜨리는 일이
    생긴다. 그때 예외가 나면 send() 앞에서 터져서 **알림이 통째로 사라진다.**
    반자동은 부가 기능이고 알림이 본체다. 없으면 없는 대로 돌아야 한다."""
    if "mod" in _PAPER:
        return _PAPER["mod"]
    try:
        import paper as P
    except Exception as e:
        log(f"paper.py 를 불러오지 못해 반자동 체결을 끕니다 ({type(e).__name__}) "
            "— 알림과 로깅은 그대로 돕니다")
        P = None
    _PAPER["mod"] = P
    return P


def paper_ctx():
    """(PaperKIS, 장부). 준비가 안 됐으면 (None, None)."""
    if "pk" in _PAPER:
        return _PAPER["pk"], _PAPER["bk"]
    P = _paper_mod()
    if P is None:
        _PAPER["pk"] = _PAPER["bk"] = None
        return None, None
    key, sec = env("KIS_PAPER_APP_KEY"), env("KIS_PAPER_APP_SECRET")
    acct = env("KIS_PAPER_ACCOUNT")
    if not (key and sec and acct):
        log("모의투자 키/계좌 미설정 → 반자동 체결 비활성 (알림·로깅은 그대로)")
        _PAPER["pk"] = _PAPER["bk"] = None
        return None, None
    try:
        pk = P.PaperKIS(key, sec, acct, state_dir=STATE_DIR, log=log)
        _PAPER["pk"], _PAPER["bk"] = pk, P.load_book(STATE_DIR)
    except Exception as e:
        log(f"모의투자 초기화 실패 → 반자동 비활성: {type(e).__name__} {str(e)[:100]}")
        _PAPER["pk"] = _PAPER["bk"] = None
    return _PAPER["pk"], _PAPER["bk"]


def paper_pump(market: str) -> None:
    """승인 버튼 수거 + 자동 청산. 루프 매 회차에 부른다.

    webhook 없이 getUpdates 로 돌기 때문에 서버를 따로 띄울 필요가 없다.
    대신 승인이 한 박자 늦는다 — 알림, 버튼, 다음 스캔."""
    if not paper_on(market):
        return
    try:
        pk, bk = paper_ctx()
    except Exception as e:
        log(f"반자동 초기화 실패: {type(e).__name__} {str(e)[:100]}")
        return
    P = _paper_mod()
    if not pk or P is None:
        return
    api = kis_api()
    price_fn = (lambda c: api.price(c)) if api else (lambda c: None)
    tok, cid = env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")
    try:
        P.collect(tok, bk, P.make_approver(pk, bk, price_fn, CFG, log), log)
        for m in P.check_exits(pk, bk, price_fn, CFG, log):
            log(m.replace("*", ""))
            if tok and cid:
                P.tg(tok, "sendMessage", chat_id=cid, text=m, parse_mode="Markdown")
    except Exception as e:
        log(f"반자동 처리 실패: {type(e).__name__} {str(e)[:150]}")
    P.save_book(STATE_DIR, bk, log)


# ── 알림 ─────────────────────────────────────────────────────────────────
def send(picks: list[dict], attrib: dict, market: str, markup: str = ""):
    tok, cid = env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")
    txt = render(picks, attrib, market)
    if not (tok and cid):
        log("텔레그램 미설정 → 출력만"); print(txt); return
    chunks = [txt[i:i + 3900] for i in range(0, len(txt), 3900)] or [txt]
    for n, c in enumerate(chunks):
        # 키보드는 마지막 조각에만 붙인다. 조각마다 붙이면 같은 종목 버튼이
        # 여러 벌 생겨서 어느 것을 눌렀는지가 흐려진다.
        d = {"chat_id": cid, "text": c, "parse_mode": "Markdown",
             "disable_web_page_preview": True}
        if markup and n == len(chunks) - 1:
            d["reply_markup"] = markup
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              timeout=30, data=d)
            if not r.ok:
                d.pop("parse_mode", None)
                requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              timeout=30, data=d)
        except Exception as e:
            log(f"텔레그램 실패: {str(e)[:80]}")


def render(picks: list[dict], attrib: dict, market: str) -> str:
    now = dt.datetime.now(KST if market == "kr" else ET)
    tz = "KST" if market == "kr" else "ET"
    mark = {"high": "●●●", "medium": "●●○", "low": "●○○"}
    head = {"us": "US", "kr": "KR", "fut": "해외선물"}.get(market, market.upper())
    out = [f"⚡ *{head} 급변동* — {now:%m-%d %H:%M} {tz}", "_자동 생성·미검증_", ""]
    for r in picks:
        a = attrib.get(r["ticker"], {})
        nm = r.get("name") or r["ticker"]
        lab = r["ticker"] if nm == r["ticker"] else f"{nm} ({r['ticker']})"
        bits = [f"{r.get('z',0):+.1f}σ"]
        if r.get("burst") is not None:
            bits.append(f"{r.get('burst_min') or CFG['BURST_MIN']:.0f}분 {r['burst']*100:+.1f}%")
        vr = r.get("vol_ratio")
        if vr is not None and vr == vr and vr:
            bits.append(f"거래량 {vr:.1f}x")
        tg = f" `{r['trigger']}`" if r.get("trigger") else ""
        out.append(f"*{lab}*  {r['pct']:+.1f}%  ({' · '.join(bits)}){tg} "
                   f"{mark.get(str(a.get('confidence','')).lower(),'')}")
        # get(k, 기본값) 은 값이 빈 문자열일 때 기본값을 안 준다 → 빈 줄이 찍힌다
        out.append(f"  {a.get('cause') or '원인 미조회'}")
        if a.get("context"):
            out.append(f"  ↳ {a['context']}")
        for s in (a.get("sources") or [])[:2]:
            if s.get("url"):
                out.append(f"  {s['url']}")
        out.append("")
    return "\n".join(out)


# ── 실행 ─────────────────────────────────────────────────────────────────
_BUDGET_SEEN: dict = {}


def scan_once(market: str, st: dict, universe: str,
              ignore_hourly: bool = False) -> tuple[int, int]:
    """한 번 스캔하고 알릴 것이 있으면 보낸다. (보낸 건수, 스캔한 종목수)."""
    rows = {"us": scan_us, "kr": scan_kr, "fut": scan_fut}[market](universe)
    save_hist(market)
    if not rows:
        return 0, 0
    lim = budget_left(st, ignore_hourly)
    if lim <= 0:
        used = len(st.get("log", []))
        # 60초 루프에서 매 회차 같은 줄을 찍으면 로그가 이것만으로 채워져
        # 정작 봐야 할 오류가 묻힌다. 사용량이 바뀔 때만 한 번 알린다.
        if _BUDGET_SEEN.get("used") != used:
            _BUDGET_SEEN["used"] = used
            log(f"알림 예산 소진 (시간당 {CFG['MAX_PER_HOUR']} / 일 "
                f"{CFG['MAX_PER_DAY']}, 오늘 {used}건 사용) → 상한이 풀릴 때까지 대기. "
                "수동 테스트는 --force 로 시간당 상한을 건너뜁니다")
        return 0, len(rows)
    _BUDGET_SEEN.pop("used", None)
    picks = pick(rows, st, lim)
    if not picks:
        return 0, len(rows)
    detail = "" if CFG["QUIET_LOG"] else ": " + ", ".join(
        f"{p['ticker']}({p['pct']:+.1f}%,{p.get('trigger','')})" for p in picks)
    log(f"스냅샷 {len(rows)}종목 → 알림 {len(picks)}{detail}")
    attrib = attribute(picks, market)
    log_signals(picks, attrib, market)
    # 버튼을 붙이다 실패하더라도 알림 자체는 반드시 나가야 한다.
    # 반자동은 부가 기능이고, 급변을 알려 주는 것이 이 시스템의 본래 일이다.
    markup = ""
    if paper_on(market):
        try:
            pk, bk = paper_ctx()
            P = _paper_mod()
            if pk and P is not None:
                markup = P.keyboard(picks, P.offer(bk, picks, market))
                P.save_book(STATE_DIR, bk, log)
        except Exception as e:
            log(f"승인 버튼 생성 실패(알림은 그대로 발송): "
                f"{type(e).__name__} {str(e)[:100]}")
            markup = ""
    send(picks, attrib, market, markup)
    now = time.time()
    for p in picks:
        st["sent"][p["ticker"]] = now
        st.setdefault("log", []).append(now)
    save_state(market, st)
    return len(picks), len(rows)


def deadline_of(market: str, hhmm: str, roll: bool = False) -> dt.datetime:
    """시장 현지시각의 HH:MM.

    roll=True 면 그 시각이 이미 12시간 넘게 지났을 때 다음 날로 넘긴다. 해외선물은
    23:00 ET 에 시작해 다음 날 02:35 ET 까지 도는 구간이 있는데, 그대로 '오늘 02:35'
    로 읽으면 20시간 전이 되어 루프가 즉시 끝나 버리기 때문이다.
    --skip-if-before/after 가드는 roll 을 쓰지 않는다. 가드는 '오늘 이 시각을
    지났는가' 를 묻는 것이라, 다음 날로 밀면 판정이 뒤집힌다."""
    tz = KST if market == "kr" else ET
    now = dt.datetime.now(tz)
    h, m = (int(x) for x in hhmm.split(":"))
    d = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if roll and (now - d).total_seconds() > 12 * 3600:
        d += dt.timedelta(days=1)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=list(MARKETS), required=True)
    ap.add_argument("--force", action="store_true", help="장 시간 검사 건너뛰기 (테스트용)")
    ap.add_argument("--loop-until", help="HH:MM (시장 현지시각) 까지 반복 스캔")
    ap.add_argument("--interval", type=int, default=60, help="루프 간격(초)")
    ap.add_argument("--universe", choices=["focus", "full"], default="focus",
                    help="full 은 순위·추적 범위를 2배로 넓힌다 (단발 스캔용)")
    ap.add_argument("--skip-if-after", metavar="HH:MM",
                    help="시장 현지시각이 이 시각을 지났으면 즉시 종료 (서머타임 중복 크론 방어)")
    ap.add_argument("--skip-if-before", metavar="HH:MM",
                    help="시장 현지시각이 이 시각 전이면 즉시 종료 (앞 구간 잡과 겹치지 않게)")
    ap.add_argument("--max-minutes", type=int, default=340,
                    help="잡 최대 수명(분). GitHub Actions 6시간 한도 방어")
    ap.add_argument("--paper-probe", action="store_true",
                    help="모의투자 연결만 확인하고 종료 (주문 없음)")
    a = ap.parse_args()

    if a.paper_probe:
        paper_probe(); return

    tz = KST if a.market == "kr" else ET
    now = dt.datetime.now(tz)
    if a.skip_if_after and now > deadline_of(a.market, a.skip_if_after):
        log(f"{a.market.upper()} {now:%H:%M} — {a.skip_if_after} 이후 시작이라 건너뜀 "
            "(다른 크론이 담당하는 구간)"); return
    if a.skip_if_before and now < deadline_of(a.market, a.skip_if_before):
        log(f"{a.market.upper()} {now:%H:%M} — {a.skip_if_before} 이전이라 건너뜀 "
            "(아직 앞 구간 잡이 담당)"); return
    if not a.force and not market_open(a.market):
        log(f"{a.market.upper()} 장 시간 아님 ({now:%H:%M}) → 종료"); return

    st = load_state(a.market)
    load_hist(a.market)
    if not a.loop_until:
        sent, nrows = scan_once(a.market, st, a.universe, ignore_hourly=a.force)
        paper_pump(a.market)
        save_state(a.market, st)
        log(f"완료 — {nrows}종목 스캔, 알림 {sent}건"); return

    end = min(deadline_of(a.market, a.loop_until, roll=True),
              now + dt.timedelta(minutes=a.max_minutes))
    log(f"{a.market.upper()} 루프 시작 — {a.interval}초 간격, {end:%H:%M} 까지 "
        f"(범위 {a.universe})")
    n, sent, fail, nrows = 0, 0, 0, 0
    while dt.datetime.now(tz) < end:
        t0 = time.time()
        n += 1
        try:
            s1, nrows = scan_once(a.market, st, a.universe, ignore_hourly=a.force)
            sent += s1
            fail = 0
        except KeyboardInterrupt:
            break
        except Exception as e:
            fail += 1
            log(f"스캔 실패({fail}): {type(e).__name__} {str(e)[:150]}")
            if fail >= 5:
                log("연속 실패 5회 → 간격을 2배로 늘려 진정시킨다")
                time.sleep(a.interval)
                fail = 0
        # 스캔이 실패해도 승인 수거와 청산은 돌아야 한다. 보유 중인 포지션이
        # 스캔 오류 때문에 손절선을 넘겨 방치되는 것이 훨씬 나쁘다.
        paper_pump(a.market)
        took = time.time() - t0
        if took > a.interval and n % 20 == 1:
            log(f"주의: 스캔 1회에 {took:.0f}초 — 간격({a.interval}초)을 넘습니다. "
                "관심종목이나 TRACK_MAX 를 줄이거나 WORKERS 를 늘리세요")
        if n % 20 == 1:                     # 살아있음 표시 (1분 루프에서 매회 찍으면 로그가 넘친다)
            log(f"…{n}회차 {dt.datetime.now(tz):%H:%M} — {nrows}종목 관측, 누적 알림 {sent}건 "
                f"(스캔 {took:.0f}초)")
        if not a.force and not market_open(a.market):
            log("장 종료 감지 → 루프 종료"); break
        time.sleep(max(2.0, a.interval - (time.time() - t0)))
    save_state(a.market, st)
    tail = ""
    if paper_on(a.market):
        _, bk = paper_ctx()
        if bk is not None:
            tail = f", 보유 {len(bk.get('positions') or {})}종목"
    log(f"루프 종료 — {n}회 스캔, 알림 {sent}건{tail}")


def paper_probe() -> None:
    """모의투자 연결 점검. 토큰·계좌·잔고까지만 확인하고 주문은 내지 않는다.
    첫 설정에서 무엇이 틀렸는지를 한 번에 보려는 용도다."""
    P = _paper_mod()
    if P is None:
        log("paper.py 가 저장소에 없습니다 — 파일을 올렸는지 확인하세요"); return
    key, sec = env("KIS_PAPER_APP_KEY"), env("KIS_PAPER_APP_SECRET")
    acct = env("KIS_PAPER_ACCOUNT")
    if not (key and sec and acct):
        miss = [k for k, v in (("KIS_PAPER_APP_KEY", key), ("KIS_PAPER_APP_SECRET", sec),
                               ("KIS_PAPER_ACCOUNT", acct)) if not v]
        log(f"모의투자 설정 없음 — 비어 있는 시크릿: {', '.join(miss)}"); return
    pk = P.PaperKIS(key, sec, acct, state_dir=STATE_DIR, log=log)
    # 마스킹하되 자릿수는 그대로 보여 준다. 별표를 고정 개수로 찍으면 자릿수가
    # 틀려도 정상처럼 보여서, 정작 원인인 오타를 못 찾는다.
    log(f"모의계좌 {pk.cano[:4]}{'*' * max(0, pk.acc_len - 4)} "
        f"({pk.acc_len}자리) / 상품코드 {pk.prod}")
    if getattr(pk, "padded", False):
        log(f"  {pk.acc_len}자리로 들어와 앞에 0 을 채워 {pk.cano} 로 시도합니다 "
            "(화면에서 선행 0 이 빠졌을 수 있습니다)")
    bad = pk.acct_problem()
    if bad:
        log(f"계좌번호 형식 오류 — {bad}")
        log("  KIS_PAPER_ACCOUNT 시크릿을 다시 확인하세요. 하이픈은 넣으셔도 됩니다.")
        return
    if not pk.token():
        log("토큰 발급 실패 → 위 오류 메시지를 확인하세요"); return
    log("토큰 발급 성공")
    okb, lines = pk.diagnose()
    for l in lines:
        log(l)
    if not okb:
        # 다른 엔드포인트로 같은 계좌를 물어본다. 둘 다 막히면 계좌가 이 앱키에
        # 안 붙어 있다는 뜻이고, 한쪽만 되면 잔고조회 파라미터 문제다.
        okc, info = pk.can_buy()
        log(f"교차확인 — 매수가능조회: {'성공' if okc else '실패'} {info}")
        if okc:
            log("  계좌는 살아 있습니다. 잔고조회 파라미터만 문제이니 알려 주세요.")
            return
        log("  매수가능조회도 같은 이유로 막힙니다 → 계좌가 이 앱키에 연결돼 있지 않습니다.")
        log("잔고조회가 어느 조합으로도 안 됩니다. 가능성이 높은 순서로:")
        log("  1) 앱키와 계좌가 다른 세트 — 국내 앱키에 해외 모의계좌를 넣으면")
        log("     토큰은 발급되는데 잔고조회만 거부됩니다 (OPSQ2000 이 그 신호입니다).")
        log("     KIS Developers 에서 이 앱키에 연결된 계좌번호를 확인하세요.")
        log("  2) 계좌번호 오타 — 앞뒤 공백, 자릿수, 상품코드를 다시 보세요.")
        log(f"     지금 입력값은 {len(pk.cano)}자리 + 상품코드 {pk.prod} 로 읽혔습니다.")
        log("  3) 국내주식 모의투자 신청이 안 된 계좌 — 해외만 신청된 경우입니다.")
        return
    bal = pk.balance()
    if bal is None:
        log("잔고조회 실패 → 계좌번호나 모의투자 신청 상태를 확인하세요"); return
    cash = pk.cash()
    log(f"잔고조회 성공 — 보유 {len(bal)}종목" +
        (f", 주문가능현금 {cash:,.0f}원" if cash is not None else ""))
    for c, v in list(bal.items())[:10]:
        log(f"  {c} {v['qty']:,}주 @ {v['avg']:,.0f} ({v['pnl_pct']:+.2f}%)")
    bk = P.load_book(STATE_DIR)
    log(f"장부 — 대기 {len(bk.get('pending') or {})}건, "
        f"보유 {len(bk.get('positions') or {})}종목, "
        f"청산이력 {len(bk.get('closed') or [])}건")
    log("연결 정상. 실제 주문은 텔레그램 버튼을 눌렀을 때만 나갑니다.")


if __name__ == "__main__":
    main()
