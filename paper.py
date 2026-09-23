#!/usr/bin/env python3
"""모의투자 반자동 체결 — 텔레그램 버튼으로 승인받아 KIS 모의계좌에 주문한다.

왜 시세와 주문을 떼어 놓았나
  모의투자 환경은 해외주식 시세·순위 API 가 제한되고 REST 호출 한도도 낮다.
  지금 스캔을 모의 키로 옮기면 미국 쪽이 통째로 막힌다. 그래서 **조회는 실전 키
  그대로, 주문만 모의 도메인**으로 보낸다. 돈은 안 나가고 배관은 실전과 같다.

왜 완전자동이 아닌가
  운용인력의 개인계좌 매매에는 준법감시인 사전승인이 붙는다. 알고리즘이 아직
  정하지도 않은 주문은 사전승인을 받을 수가 없다. 사람이 버튼을 눌러야 주문이
  나가는 구조만이 그 체계와 개념적으로 양립한다.
  부수 효과가 하나 더 있다 — 승인한 표본의 수익률에서 전체 알림 평균을 빼면
  재량이 알파를 더하는지 빼는지가 숫자로 나온다.

주문 TR 은 신 TR 을 쓴다. KIS 스펙에 "구TR은 사전고지 없이 막힐 수 있다" 고
명시돼 있어, 구 TR(VTTC0802U/0801U) 로 짜 두면 어느 날 조용히 주문이 안 나간다.
"""
from __future__ import annotations
import hashlib, json, os, re, statistics, time, datetime as dt
from zoneinfo import ZoneInfo
import requests

KST = ZoneInfo("Asia/Seoul")
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"

# 신 TR (모의투자는 앞에 V). 실전은 T 로 시작하지만 이 파일은 모의 전용이다.
TR_BUY, TR_SELL, TR_BAL = "VTTC0012U", "VTTC0011U", "VTTC8434R"
TR_CCLD = "VTTC8001R"                    # 주식일별주문체결조회 (주문이 들어갔는지 확인)
# 토큰이 죽었을 때 나오는 코드들. 캐시를 버리고 한 번 다시 받으면 된다.
TOKEN_DEAD = {"EGW00123", "EGW00121", "EGW00105"}

# ── 연결 재사용 ──────────────────────────────────────────────────────────
# kis.py 와 같은 이유. 호출마다 새 연결을 열면 KIS 가 어느 선에서 응답 없이
# 연결을 끊는다. 테스트가 requests 를 갈아끼운 경우에는 그대로 통과시킨다.
_SESSION = requests.Session()
_REAL = {"get": requests.get, "post": requests.post}


def http(method: str, url: str, **kw):
    fn = getattr(requests, method)
    if fn is _REAL.get(method):
        fn = getattr(_SESSION, method)
    return fn(url, **kw)


NET_ERRORS = (requests.exceptions.ConnectionError,
              requests.exceptions.Timeout,
              requests.exceptions.ChunkedEncodingError)


def _f(d: dict, k: str) -> float:
    try:
        return float(str(d.get(k, "") or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def weekdays_between(ymd: str, today: dt.date | None = None) -> int:
    """진입일로부터 지난 '주중' 일수. 공휴일을 모르므로 **실제 거래일이 아니다.**

    휴장일을 경과일로 세기 때문에 연휴가 끼면 보유기간을 실제보다 길게 본다.
    2026년 추석(9/24~25 휴장)에는 9/21 진입 포지션이 9/28 에 '5영업일'로 잡히는데
    실제로 장이 선 날은 3일뿐이다. 그러면 시간청산이 이틀 일찍 나가고,
    일봉으로 재현하는 backfill 과 숫자가 어긋나 비교 자체가 깨진다.

    그래서 이 함수는 **일봉을 못 받았을 때의 대비책**으로만 쓴다.
    정상 경로는 held_days() — 실제 일봉 개수를 센다."""
    try:
        d0 = dt.datetime.strptime(ymd, "%Y%m%d").date()
    except (TypeError, ValueError):
        return 0
    d1 = today or dt.datetime.now(KST).date()
    n, cur = 0, d0
    while cur < d1:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def held_days(code: str, entry_date: str, bars_fn=None, cache: dict | None = None,
              log=print) -> tuple[int, bool]:
    """진입일 이후 **실제로 장이 선 날** 수와, 일봉으로 셌는지 여부. (n, exact)

    달력을 들고 있지 않아도 되는 이유가 이것이다. 일봉은 장이 선 날에만
    생기므로, 진입일보다 뒤인 봉을 세면 그게 곧 거래일 수다. 설·추석·임시공휴일
    어느 것도 따로 알 필요가 없고, 내년에도 그대로 맞는다.

    일봉을 못 받으면 weekdays_between 으로 내려간다 — 그 경우 exact=False 라
    호출부가 '정확하지 않다' 는 것을 알고 처리할 수 있다."""
    today = dt.datetime.now(KST).strftime("%Y%m%d")
    if cache is not None:
        if cache.get("date") != today:       # 날짜가 바뀌면 통째로 버린다
            cache.clear(); cache["date"] = today
        hit = (cache.get("map") or {}).get(code)
        if hit is not None:
            return int(hit), True
    if bars_fn and entry_date:
        try:
            bars = bars_fn(code) or []
        except Exception as e:
            log(f"보유일 계산용 일봉 실패 [{code}]: {type(e).__name__} {str(e)[:60]}")
            bars = []
        dates = {str(b.get("date") or "") for b in bars}
        dates.discard("")
        # 진입일 자체는 세지 않는다. 그 날 종가에 샀으므로 보유 0일이다.
        if dates and max(dates) >= entry_date:
            n = sum(1 for d in dates if d > entry_date)
            if cache is not None:
                cache.setdefault("map", {})[code] = n
            return n, True
    return weekdays_between(entry_date), False


def sigma_from_bars(bars, today: str | None = None) -> float | None:
    """일간 σ — monitor.ref_of 와 **같은 정의**다.

    오늘 봉을 빼고 최근 21개 종가로 일간 수익률의 모표준편차를 낸다. 알림에
    실리는 σ 와 정의가 같아야 '진입가 -1.5σ' 라는 손절선이 같은 뜻이 된다.
    오늘 봉을 넣으면 지금 급등락한 폭이 σ 를 키워 손절선이 헐거워진다."""
    today = today or dt.datetime.now(KST).strftime("%Y%m%d")
    rows = sorted((b for b in (bars or []) if float(b.get("close") or 0) > 0),
                  key=lambda b: str(b.get("date") or ""), reverse=True)
    if rows and str(rows[0].get("date")) == today:
        rows = rows[1:]
    rows = rows[:21]
    if len(rows) < 10:
        return None
    px = [float(r["close"]) for r in rows]
    rets = [px[i] / px[i + 1] - 1 for i in range(len(px) - 1) if px[i + 1] > 0]
    if len(rets) < 5:
        return None
    sd = statistics.pstdev(rets)
    return sd if sd > 0 else None


def stop_for(entry: float, sd: float, cfg: dict) -> tuple[float, str]:
    """(손절가, 근거). σ 가 있으면 진입가 -STOP_SD×σ, 없으면 고정 %.

    예전에는 σ 가 없으면 손절가를 0 으로 두었다. 0 은 '손절 없음' 이라,
    그 포지션은 -20% 가 돼도 시간청산까지 그냥 들고 간다. 알림에 σ 가 비는
    일은 드물지 않다(일봉 부족, 조회 실패). 그래서 고정 % 로 반드시 선을 긋는다."""
    if not entry or entry <= 0:
        return 0.0, ""
    if sd and sd > 0:
        return entry * (1 - float(cfg.get("STOP_SD", 1.5)) * sd), "σ"
    fb = float(cfg.get("STOP_FALLBACK_PCT", 7.0) or 0.0)
    return (entry * (1 - fb / 100), f"고정 {fb:g}%") if fb > 0 else (0.0, "")


class PaperKIS:
    """모의투자 주문 전용 클라이언트. 조회용 kis.KIS 와 별개로 토큰을 든다."""

    def __init__(self, app_key: str, app_secret: str, account: str,
                 state_dir: str = "state", log=print, prod_cd: str = "01"):
        self.key, self.sec = app_key, app_secret
        # 계좌번호는 앞 8자리(CANO) + 상품코드 2자리(ACNT_PRDT_CD)로 나뉜다.
        # 하이픈·공백·괄호 등 숫자가 아닌 것은 전부 털어낸다. 시크릿에 붙여넣을 때
        # 눈에 안 보이는 문자가 섞여 들어오는 일이 잦다.
        digits = re.sub(r"\D", "", account or "")
        self.acc_len = len(digits)
        # 화면에 7자리로 보이는 계좌가 있다. 선행 0 이 표시에서 떨어진 경우라
        # 0 을 채워 8자리로 만들어 본다. 막아 버리면 시험조차 못 해서, 정작
        # 그 계좌가 맞는지 확인할 방법이 없어진다.
        self.padded = len(digits) in (7, 9) and len(digits) % 2 == 1
        if self.padded:
            digits = digits.zfill(8 if len(digits) == 7 else 10)
        self.cano, self.prod = digits[:8], (digits[8:10] or prod_cd)
        self.state_dir, self.log = state_dir, log
        self._tok, self._exp = "", 0.0
        self._seen: set = set()
        self._bal_ofl: str | None = None      # 통하는 OFL_YN 값 (첫 조회에서 확정)
        self.last_err = ""                    # 마지막 주문 실패 사유 (텔레그램 회신용)

    # ── 토큰 ────────────────────────────────────────────────────────────
    def _token_path(self) -> str:
        return os.path.join(self.state_dir, "kis_paper_token.json")

    def _key_fp(self) -> str:
        """앱키 지문. 토큰은 앱키에 묶여 있어서, 키가 바뀌면 캐시된 토큰은
        무효가 된다. 그걸 모르고 계속 쓰면 EGW00123(만료된 token)이 나는데,
        메시지가 '만료' 라 원인을 엉뚱한 데서 찾게 된다."""
        return hashlib.sha256(self.key.encode()).hexdigest()[:16]

    def _clear_token(self) -> None:
        self._tok, self._exp = "", 0.0
        try:
            os.remove(self._token_path())
        except OSError:
            pass

    def token(self, _retry: int = 0) -> str:
        if self._tok and time.time() < self._exp - 600:
            return self._tok
        try:
            c = json.load(open(self._token_path()))
            if c.get("fp") != self._key_fp():
                self.log("앱키가 바뀌었습니다 — 캐시된 모의 토큰을 버리고 새로 받습니다")
                raise ValueError("key changed")
            if time.time() < c.get("exp", 0) - 600:
                self._tok, self._exp = c["tok"], c["exp"]
                return self._tok
        except Exception:
            pass
        try:
            r = http("post", f"{PAPER_BASE}/oauth2/tokenP", timeout=20,
                              json={"grant_type": "client_credentials",
                                    "appkey": self.key, "appsecret": self.sec})
            j = r.json()
        except Exception as e:
            self.log(f"모의 토큰 발급 통신 실패: {type(e).__name__} {str(e)[:120]}")
            return ""
        tok = j.get("access_token")
        if not tok:
            code = str(j.get("error_code") or j.get("msg_cd") or "")
            msg = str(j.get("error_description") or j.get("msg1") or r.text)[:160]
            # EGW00133 = 1분에 1회 제한. 키 오류는 기다려도 소용없으니 즉시 포기한다.
            if code == "EGW00133" and _retry < 2:
                self.log(f"모의 토큰 1분 제한 — 70초 뒤 재시도 ({_retry + 1}/2)")
                time.sleep(70)
                return self.token(_retry + 1)
            self.log(f"모의 토큰 발급 실패 {code}: {msg}")
            self.log("  → 모의투자 전용 앱키인지 확인하세요. 실전 키는 모의 도메인에서 거부됩니다.")
            return ""
        self._tok = tok
        self._exp = time.time() + int(j.get("expires_in") or 86400)
        os.makedirs(self.state_dir, exist_ok=True)
        try:
            json.dump({"tok": self._tok, "exp": self._exp, "fp": self._key_fp()},
                      open(self._token_path(), "w"))
        except Exception:
            pass
        self.log("모의투자 토큰 신규 발급")
        return self._tok

    def acct_problem(self) -> str:
        """계좌번호 자릿수가 이상하면 그 이유를 돌려준다. 정상이면 빈 문자열.

        7자리나 9자리가 들어와도 KIS 는 '계좌번호가 틀렸다' 고만 답하지, 몇 자리가
        들어왔는지는 말해 주지 않는다. 그래서 보내기 전에 여기서 먼저 잡는다."""
        if self.acc_len == 0:
            return "계좌번호가 비어 있습니다"
        if self.acc_len in (7, 9):
            return ""          # 0 을 채워 진행한다 (경고는 probe 가 따로 찍는다)
        if self.acc_len not in (8, 10):
            return (f"계좌번호 숫자가 {self.acc_len}자리입니다 — "
                    "8자리(계좌) 또는 10자리(계좌8+상품코드2)여야 합니다")
        return ""

    def ready(self) -> bool:
        return bool(self.key and self.sec and self.cano and self.token())

    # ── 공통 ────────────────────────────────────────────────────────────
    def _hashkey(self, body: dict) -> str:
        """POST 주문은 본문 해시를 함께 보내야 한다. 실패해도 주문 자체는
        시도한다 — 해시 없이도 받아 주는 구간이 있어서, 여기서 멈추면
        '왜 주문이 안 나가는지' 를 모르게 된다."""
        try:
            r = http("post", f"{PAPER_BASE}/uapi/hashkey", timeout=10, json=body,
                              headers={"content-type": "application/json; charset=utf-8",
                                       "appkey": self.key, "appsecret": self.sec})
            return r.json().get("HASH", "")
        except Exception as e:
            self.log(f"hashkey 실패(주문은 계속): {type(e).__name__} {str(e)[:80]}")
            return ""

    def _headers(self, tr_id: str, extra: dict | None = None) -> dict:
        h = {"authorization": f"Bearer {self.token()}", "appkey": self.key,
             "appsecret": self.sec, "tr_id": tr_id, "custtype": "P",
             "content-type": "application/json; charset=utf-8"}
        h.update(extra or {})
        return h

    def _fail(self, what: str, r, j: dict) -> str:
        """실패 사유를 로그에 한 번 남기고, 사람이 읽을 문장으로 돌려준다.

        로그에만 남기면 텔레그램에는 '실패 — 로그를 확인하세요' 밖에 못 쓴다.
        장중에 Actions 로그를 뒤지게 만드는 것은 설계 실패다."""
        code = str(j.get("msg_cd") or "")
        msg = str(j.get("msg1") or r.text)[:200]
        sig = (what, r.status_code, code)
        if sig not in self._seen:
            self._seen.add(sig)
            self.log(f"모의 {what} 실패 HTTP {r.status_code} {code}: {msg}")
        return f"{msg} ({code})" if code else msg

    # ── 주문 ────────────────────────────────────────────────────────────
    def order(self, code: str, qty: int, side: str = "buy",
              ord_dvsn: str = "01", price: int = 0,
              _retry: bool = False) -> dict | None:
        """국내주식 주문. side 는 buy/sell, ord_dvsn 01=시장가 00=지정가.
        성공하면 {"ord_no", "krx_fwdg_ord_orgno", "ord_tmd"}, 실패하면 None."""
        if qty < 1:
            return None
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod, "PDNO": code,
                "ORD_DVSN": ord_dvsn, "ORD_QTY": str(int(qty)),
                "ORD_UNPR": str(int(price) if ord_dvsn == "00" else 0),
                # 거래소를 고정해야 수수료 요율이 정확히 잡힌다.
                "EXCG_ID_DVSN_CD": "KRX"}
        tr = TR_BUY if side == "buy" else TR_SELL
        hk = self._hashkey(body)
        try:
            r = http("post", f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/order-cash",
                     timeout=20, json=body,
                     headers=self._headers(tr, {"hashkey": hk} if hk else None))
            j = r.json()
        except NET_ERRORS as e:
            # **주문은 함부로 다시 보내지 않는다.** 연결이 끊겼다는 것은 응답을
            # 못 받았다는 뜻일 뿐, 주문이 안 들어갔다는 뜻이 아니다. 그냥 재시도하면
            # 같은 종목을 두 번 살 수 있다. 먼저 체결내역으로 들어갔는지 확인하고,
            # 확실히 안 들어간 경우에만 한 번 다시 보낸다.
            self.log(f"모의 주문 통신 실패 [{code}]: {type(e).__name__} {str(e)[:120]}")
            landed = self.order_landed(code, side)
            if landed is True:
                self.last_err = ""
                self.log(f"  → 체결내역에 주문이 있습니다 — 재전송하지 않습니다 [{code}]")
                return {"ord_no": "", "org": "", "tmd": "",
                        "msg": "통신은 끊겼지만 주문은 접수됨"}
            if landed is None or _retry:
                # 확인 자체가 실패했거나 이미 한 번 다시 보냈다. 중복 매수보다
                # 놓친 매수가 낫다 — 여기서 멈추고 사람이 판단하게 한다.
                self.last_err = ("통신 실패 — 주문 여부 확인 불가, 재전송 안 함"
                                 if landed is None else f"통신 실패 {type(e).__name__}")
                if landed is None:
                    self.log(f"  → 주문 여부를 확인하지 못했습니다 — 중복 방지를 위해 "
                             f"재전송하지 않습니다 [{code}]. 모의계좌에서 직접 확인하세요")
                return None
            self.log(f"  → 체결내역에 없습니다 — 한 번만 다시 보냅니다 [{code}]")
            time.sleep(0.8)
            return self.order(code, qty, side, ord_dvsn, price, _retry=True)
        except Exception as e:
            self.last_err = f"통신 실패 {type(e).__name__}"
            self.log(f"모의 주문 통신 실패 [{code}]: {type(e).__name__} {str(e)[:120]}")
            return None
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            if str(j.get("msg_cd") or "") in TOKEN_DEAD and not _retry:
                self._clear_token()
                return self.order(code, qty, side, ord_dvsn, price, _retry=True)
            self.last_err = self._fail(f"주문({side})", r, j)
            return None
        o = j.get("output") or {}
        return {"ord_no": o.get("ODNO") or "", "org": o.get("KRX_FWDG_ORD_ORGNO") or "",
                "tmd": o.get("ORD_TMD") or "", "msg": str(j.get("msg1") or "")[:60]}

    def order_landed(self, code: str, side: str = "buy",
                     within_sec: int = 180) -> bool | None:
        """방금 낸 주문이 실제로 접수됐는지. True/False, **모르면 None**.

        통신이 끊겼을 때 재전송해도 되는지 판단하는 유일한 근거다. 그래서
        '확인 실패' 를 '주문 없음' 으로 뭉뚱그리지 않는다 — 그렇게 하면 확인이
        안 될 때마다 중복 주문을 내게 된다. 모르면 모른다고 답하고, 호출부는
        모를 때 재전송하지 않는다."""
        today = dt.datetime.now(KST).strftime("%Y%m%d")
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod,
                  "INQR_STRT_DT": today, "INQR_END_DT": today,
                  "SLL_BUY_DVSN_CD": "02" if side == "buy" else "01",
                  "INQR_DVSN": "00", "PDNO": code, "CCLD_DVSN": "00",
                  "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00",
                  "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        try:
            r = http("get",
                     f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                     timeout=15, params=params, headers=self._headers(TR_CCLD))
            j = r.json()
        except Exception as e:
            self.log(f"주문 확인 조회 실패 [{code}]: {type(e).__name__} {str(e)[:80]}")
            return None
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            self.log(f"주문 확인 조회 거절 [{code}]: "
                     f"{j.get('msg_cd')} {str(j.get('msg1'))[:60]}")
            return None
        rows = j.get("output1")
        if rows is None:
            return None                       # 응답 모양이 예상과 다르다 = 모름
        now = dt.datetime.now(KST)
        for o in rows:
            if str(o.get("pdno") or "").zfill(6) != str(code).zfill(6):
                continue
            tmd = str(o.get("ord_tmd") or "")
            if len(tmd) != 6:
                return True                   # 시각을 못 읽어도 주문은 있다
            try:
                t = now.replace(hour=int(tmd[:2]), minute=int(tmd[2:4]),
                                second=int(tmd[4:6]), microsecond=0)
            except ValueError:
                return True
            if 0 <= (now - t).total_seconds() <= within_sec:
                return True
        return False

    # OFL_YN 은 문서상 공란인데, 모의 서버가 공란을 거부하고 "N" 을 요구하는
    # 사례가 보고돼 있다. 어느 쪽이 맞는지 실호출 전에는 알 수 없어, 처음 한 번은
    # 양쪽을 시도해 보고 통한 조합을 기억한다.
    BAL_OFL = ("", "N")

    def _bal_params(self, ofl: str, prod: str | None = None) -> dict:
        return {"CANO": self.cano, "ACNT_PRDT_CD": prod or self.prod,
                "AFHR_FLPR_YN": "N", "OFL_YN": ofl, "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}

    def _bal_raw(self, ofl: str, prod: str | None = None, quiet: bool = False,
                 _retry: int = 0):
        """(응답 dict 또는 None, 오류코드). 진단용으로 오류코드를 함께 돌려준다."""
        try:
            r = http("get", f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-balance",
                             timeout=20, params=self._bal_params(ofl, prod),
                             headers=self._headers(TR_BAL))
            j = r.json()
        except Exception as e:
            # 개장 직후 KIS 가 자주 끊는다. 한 번은 다시 물어본다 — 여기서
            # 포기하면 그 회차의 청산 점검이 통째로 비고, 손절선을 넘긴
            # 포지션이 방치된다.
            if _retry < 1:
                time.sleep(0.5)
                return self._bal_raw(ofl, prod, quiet, _retry + 1)
            self.log(f"모의 잔고조회 통신 실패: {type(e).__name__} {str(e)[:120]}")
            return None, "NET"
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            code = str(j.get("msg_cd") or f"HTTP{r.status_code}")
            # 유량 초과는 '거부' 가 아니라 '답을 못 받음' 이다. 이걸 실패로 세면
            # 멀쩡한 조합을 틀렸다고 판단해 버린다.
            if code == "EGW00201" and _retry < 2:
                time.sleep(1.0 * (_retry + 1))
                return self._bal_raw(ofl, prod, quiet, _retry + 1)
            # 토큰이 죽었으면 버리고 한 번 다시 받는다. 캐시가 남아 있는 한
            # 몇 번을 돌려도 같은 오류만 반복된다.
            if code in TOKEN_DEAD and _retry < 1:
                self._clear_token()
                return self._bal_raw(ofl, prod, quiet, _retry + 1)
            if not quiet:
                self._fail("잔고조회", r, j)
            msg = str(j.get("msg1") or "").strip()[:60]
            return None, (f"{code} {msg}" if msg else code)
        return j, ""

    def can_buy(self, prod: str | None = None, _retry: bool = False) -> tuple[bool, str]:
        """매수가능조회 — 잔고조회와 **다른 엔드포인트**로 같은 계좌를 물어본다.

        둘 다 계좌번호를 받는데, 한쪽만 되면 파라미터 문제이고 둘 다 막히면
        계좌가 이 앱키에 안 붙어 있다는 뜻이다. 그 둘을 가르려는 것이다.
        읽기 전용이라 주문은 나가지 않는다."""
        p = {"CANO": self.cano, "ACNT_PRDT_CD": prod or self.prod,
             "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01",
             "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
        try:
            r = http("get", f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                             timeout=20, params=p, headers=self._headers("VTTC8908R"))
            j = r.json()
        except Exception as e:
            return False, f"통신 실패 {type(e).__name__}"
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            if str(j.get("msg_cd") or "") in TOKEN_DEAD and not _retry:
                self._clear_token()
                return self.can_buy(prod, _retry=True)
            return False, (f"{j.get('msg_cd') or r.status_code} "
                           f"{str(j.get('msg1') or '')[:60]}").strip()
        return True, str((j.get("output") or {}).get("ord_psbl_cash") or "")

    def balance(self) -> dict[str, dict] | None:
        """보유종목 {종목코드: {qty, avg, last, pnl_pct}}. 실패하면 None.

        None 과 {} 를 구분하는 것이 중요하다. {} 는 '정말 하나도 없다',
        None 은 '조회를 못 했다' — 후자를 빈 잔고로 읽으면 장부의 포지션을
        전부 지워 버린다."""
        tries = (self._bal_ofl,) if self._bal_ofl is not None else self.BAL_OFL
        j = None
        for k, ofl in enumerate(tries):
            j, code = self._bal_raw(ofl, quiet=(k < len(tries) - 1))
            if j is not None:
                if self._bal_ofl is None:
                    self._bal_ofl = ofl
                break
        if j is None:
            return None
        out = {}
        for x in (j.get("output1") or []):
            q = _f(x, "hldg_qty")
            if q > 0:
                out[str(x.get("pdno") or "").zfill(6)] = {
                    "qty": int(q), "avg": _f(x, "pchs_avg_pric"),
                    "last": _f(x, "prpr"), "pnl_pct": _f(x, "evlu_pfls_rt"),
                    "name": str(x.get("prdt_name") or "").strip(),
                    # 주문가능수량. 보유수량 > 0 인데 이게 0 이면 매도 주문이 이미
                    # 걸려 있어 체결을 기다리는 중이다. 이걸 모르고 또 팔면 같은
                    # 주식을 두 번 파는 주문이 나간다. 필드가 없으면 None(모름).
                    "sellable": (int(_f(x, "ord_psbl_qty"))
                                 if "ord_psbl_qty" in x else None)}
        return out

    # ── 체결 조회 ───────────────────────────────────────────────────────
    def _ccld(self, start: str, end: str, code: str = "", side: str = "",
              odno: str = "", ccld: str = "00", _retry: int = 0) -> list | None:
        """주식일별주문체결조회. 행 목록, **모르면 None** (빈 목록과 구분).

        side: buy/sell/"" (전체). ccld: 00 전체, 01 체결, 02 미체결."""
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod,
                  "INQR_STRT_DT": start, "INQR_END_DT": end,
                  "SLL_BUY_DVSN_CD": {"buy": "02", "sell": "01"}.get(side, "00"),
                  "INQR_DVSN": "00", "PDNO": code, "CCLD_DVSN": ccld,
                  "ORD_GNO_BRNO": "", "ODNO": odno, "INQR_DVSN_3": "00",
                  "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        try:
            r = http("get",
                     f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                     timeout=15, params=params, headers=self._headers(TR_CCLD))
            j = r.json()
        except Exception as e:
            if _retry < 1:
                time.sleep(0.5)
                return self._ccld(start, end, code, side, odno, ccld, _retry + 1)
            self.log(f"체결조회 통신 실패 [{code or odno}]: {type(e).__name__} {str(e)[:80]}")
            return None
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            mc = str(j.get("msg_cd") or "")
            if mc == "EGW00201" and _retry < 2:          # 유량 초과 — 잠깐 쉬고 다시
                time.sleep(1.0 * (_retry + 1))
                return self._ccld(start, end, code, side, odno, ccld, _retry + 1)
            if mc in TOKEN_DEAD and _retry < 1:
                self._clear_token()
                return self._ccld(start, end, code, side, odno, ccld, _retry + 1)
            self._fail("체결조회", r, j)
            return None
        rows = j.get("output1")
        return rows if isinstance(rows, list) else None

    def fill_of(self, odno: str, code: str, side: str = "buy",
                tries: int = 3, wait: float = 2.0) -> tuple[int, float, int] | None:
        """방금 낸 주문의 (체결수량, 체결평균가, 주문수량). 모르면 None.

        '주문 접수' 는 '체결' 이 아니다. 얇은 종목에 시장가로 3,300주를 넣으면
        438주만 체결되고 나머지는 호가를 기다린다. 접수 응답만 보고 '3,300주
        매수' 라고 알리면, 실제 노출과 알림이 여덟 배 차이가 난다."""
        if not odno:
            return None
        today = dt.datetime.now(KST).strftime("%Y%m%d")
        want = str(odno).lstrip("0")
        last = None
        for k in range(max(1, tries)):
            if k:
                time.sleep(wait)
            rows = self._ccld(today, today, code=code, side=side, odno=odno)
            if rows is None:
                continue
            for o in rows:
                if str(o.get("odno") or "").lstrip("0") != want:
                    continue
                q, oq = int(_f(o, "tot_ccld_qty")), int(_f(o, "ord_qty"))
                avg = _f(o, "avg_prvs")
                if avg <= 0 and q > 0:
                    avg = _f(o, "tot_ccld_amt") / q
                last = (q, avg, oq)
                break
            if last and last[2] > 0 and last[0] >= last[2]:
                break                               # 전량 체결 — 더 기다릴 필요 없다
        return last

    def last_buy_date(self, code: str, lookback: int = 60) -> str | None:
        """이 종목의 가장 최근 **체결된** 매수일 (YYYYMMDD). 모르면 None.

        장부 밖에서 발견된 보유종목의 보유일을 세려면 언제 샀는지가 필요하다.
        그걸 모르고 발견한 날을 진입일로 치면, 이미 열흘 들고 있던 종목에
        시간청산 시계가 처음부터 다시 돌아 닷새를 더 들고 간다."""
        now = dt.datetime.now(KST)
        start = (now - dt.timedelta(days=lookback)).strftime("%Y%m%d")
        rows = self._ccld(start, now.strftime("%Y%m%d"), code=code, side="buy", ccld="01")
        if rows is None:
            return None
        best = ""
        for o in rows:
            if str(o.get("pdno") or "").zfill(6) != str(code).zfill(6):
                continue
            if _f(o, "tot_ccld_qty") <= 0:
                continue
            d = str(o.get("ord_dt") or "")
            if len(d) == 8 and d.isdigit() and d > best:
                best = d
        return best or None

    def buy_power(self, code: str = "005930") -> float | None:
        """지금 이 종목을 살 수 있는 현금(원). 모르면 None.

        미수 없이 살 수 있는 금액(nrcvb_buy_amt)을 우선 쓴다. 주문가능현금
        (ord_psbl_cash)은 증거금률에 따라 실제로 살 수 있는 것보다 커 보일 수 있다.
        모를 때 None 을 돌려주는 것이 중요하다 — 0 으로 읽으면 조회가 한 번
        실패했다는 이유로 모든 매수를 막아 버린다."""
        p = {"CANO": self.cano, "ACNT_PRDT_CD": self.prod,
             "PDNO": code, "ORD_UNPR": "0", "ORD_DVSN": "01",
             "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
        try:
            r = http("get", f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                     timeout=15, params=p, headers=self._headers("VTTC8908R"))
            j = r.json()
        except Exception:
            return None
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            return None
        o = j.get("output")
        if not isinstance(o, dict):
            return None
        for k in ("nrcvb_buy_amt", "ord_psbl_cash"):
            if str(o.get(k) or "").strip():
                v = _f(o, k)
                return v if v >= 0 else None
        return None

    def cash(self) -> float | None:
        """주문가능현금. 잔고조회 output2 에서 꺼낸다."""
        j, _ = self._bal_raw(self._bal_ofl if self._bal_ofl is not None else "", quiet=True)
        if not j:
            return None
        rows = j.get("output2") or []
        return _f(rows[0], "dnca_tot_amt") if rows else None

    def diagnose(self) -> tuple[bool, list[str]]:
        """계좌 조합을 바꿔 가며 잔고조회를 시도하고 무엇이 통했는지 알려준다.

        모의투자 첫 설정에서 막히는 지점이 거의 여기다. 오류코드 하나만 보고는
        '파라미터가 틀렸나, 계좌가 틀렸나' 를 가릴 수 없어서, 조합을 직접
        돌려 보고 결과를 나란히 보여 준다."""
        # 상품코드는 계좌 유형마다 다르고 화면에 안 보이는 경우가 많다.
        # 잔고조회는 읽기 전용이라 몇 개 훑어봐도 위험이 없고, 한 번에 훑는 편이
        # '바꿔서 다시 돌려보세요' 를 반복하는 것보다 빠르다.
        lines, combos, seen = [], [], set()
        for prod in (self.prod, "01", "02", "03"):
            for ofl in self.BAL_OFL:
                if (prod, ofl) not in seen:
                    seen.add((prod, ofl))
                    combos.append((prod, ofl))
        for n, (prod, ofl) in enumerate(combos):
            if n:
                time.sleep(0.4)          # 모의 서버는 유량 한도가 낮다
            j, code = self._bal_raw(ofl, prod, quiet=True)
            tag = f"상품코드 {prod} / OFL_YN {ofl or '공란'}"
            if j is not None:
                lines.append(f"  {tag} → 성공")
                self._bal_ofl, self.prod = ofl, prod
                return True, lines
            lines.append(f"  {tag} → 실패 {code}")
        return False, lines


# ── 텔레그램 승인 ─────────────────────────────────────────────────────────
def keyboard(picks: list[dict], ts: int) -> str:
    """알림 묶음에 붙일 인라인 키보드. callback_data 는 64바이트 제한이 있어
    '동작|종목코드|제안시각' 만 싣는다. 나머지는 state 에서 찾는다."""
    rows = []
    for p in picks:
        code, nm = p["ticker"], (p.get("name") or p["ticker"])[:12]
        rows.append([{"text": f"매수 {nm}", "callback_data": f"b|{code}|{ts}"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def tg(tok: str, method: str, **kw):
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", timeout=20, data=kw)
        return r.json() if r.ok else None
    except Exception:
        return None


def collect(tok: str, st: dict, on_approve, log=print) -> int:
    """버튼 눌린 것을 수거해 처리한다. webhook 없이 getUpdates 폴링으로 돈다.

    이미 도는 60초 루프에 얹는 구조라 서버를 새로 띄울 필요가 없다. 대신
    승인이 한 박자 늦는다 — 알림, 버튼, 다음 스캔이라 진입은 급변 후 3~5분.
    일 단위 호라이즌이면 문제없고, 장중 스캘핑은 애초에 불가능한 설계다."""
    if not tok:
        return 0
    # update_id 는 봇마다 따로 센다. 봇을 갈아끼웠는데 캐시에 남은 옛 봇의 큰
    # offset 을 그대로 쓰면 새 봇의 작은 update_id 가 전부 걸러져, 버튼이
    # 조용히 먹통이 된다 — 에러도 안 난다. 토큰 지문이 바뀌면 위치를 되감는다.
    fp = hashlib.sha256(tok.encode()).hexdigest()[:16]
    if st.get("tg_fp") != fp:
        if st.get("tg_fp"):
            log("텔레그램 봇이 바뀌었습니다 — 버튼 수거 위치를 처음으로 되감습니다")
        st["tg_fp"], st["offset"], st["handled"] = fp, 0, []
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20,
                         params={"offset": st.get("offset", 0), "timeout": 0,
                                 "allowed_updates": '["callback_query"]'})
        j = r.json()
    except Exception as e:
        log(f"텔레그램 수거 실패: {type(e).__name__} {str(e)[:80]}")
        return 0
    if not j.get("ok"):
        return 0
    done = 0
    for u in (j.get("result") or []):
        st["offset"] = max(st.get("offset", 0), int(u.get("update_id", 0)) + 1)
        cq = u.get("callback_query")
        if not cq:
            continue
        cid = str(cq.get("id") or "")
        # 같은 버튼을 연타해도 주문은 한 번만 나가야 한다.
        seen = st.setdefault("handled", [])
        if cid in seen:
            continue
        seen.append(cid)
        del seen[:-200]
        data = str(cq.get("data") or "")
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
        try:
            reply = on_approve(data, chat)
        except Exception as e:
            reply = f"처리 중 오류: {type(e).__name__} {str(e)[:60]}"
            log(f"승인 처리 실패 [{data}]: {type(e).__name__} {str(e)[:120]}")
        tg(tok, "answerCallbackQuery", callback_query_id=cid, text=reply[:190])
        if chat:
            tg(tok, "sendMessage", chat_id=chat, text=reply, parse_mode="Markdown")
        done += 1
    return done


# ── 장부 ─────────────────────────────────────────────────────────────────
# 포지션의 단일 진실 원천은 KIS 잔고 API 다. 이 장부는 '왜 샀는지·언제 나갈지'
# 처럼 잔고에 없는 것만 들고 있고, 수량·보유 여부는 매 회차 잔고와 대조한다.
# 캐시가 날아가도 유령 포지션이 생기지 않게 하려는 것이다.
BOOK0 = {"offset": 0, "tg_fp": "", "handled": [], "pending": {},
         "positions": {}, "closed": [], "held_cache": {}}


def book_path(state_dir: str) -> str:
    return os.path.join(state_dir, "paper_book.json")


def load_book(state_dir: str) -> dict:
    try:
        b = json.load(open(book_path(state_dir)))
    except Exception:
        b = {}
    for k, v in BOOK0.items():
        b.setdefault(k, json.loads(json.dumps(v)))
    return b


def save_book(state_dir: str, bk: dict, log=print) -> None:
    os.makedirs(state_dir, exist_ok=True)
    try:
        json.dump(bk, open(book_path(state_dir), "w"), ensure_ascii=False)
    except Exception as e:
        log(f"모의 장부 저장 실패: {str(e)[:80]}")


def offer(bk: dict, picks: list[dict], market: str) -> int:
    """이번 알림을 승인 대기 목록에 올리고 제안시각을 돌려준다."""
    ts = int(time.time())
    for p in picks:
        bk.setdefault("pending", {})[f"{p['ticker']}|{ts}"] = {
            "code": p["ticker"], "name": p.get("name") or p["ticker"],
            "px": float(p.get("last") or 0.0), "sd": float(p.get("sd_daily") or 0.0),
            "pct": float(p.get("pct") or 0.0), "trigger": p.get("trigger") or "",
            "market": market, "ts": ts}
    # 만료된 제안은 흘려보낸다 (버튼을 눌러도 만료 안내만 나간다).
    cut = ts - 6 * 3600
    for k in [k for k, v in bk["pending"].items() if v.get("ts", 0) < cut]:
        bk["pending"].pop(k, None)
    return ts


def make_approver(pk: "PaperKIS", bk: dict, price_fn, cfg: dict, log=print,
                  bars_fn=None):
    """버튼이 눌렸을 때 실행할 함수를 만든다. 반환 문자열이 그대로 회신된다."""

    def on_approve(data: str, chat: str) -> str:
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "b":
            return "알 수 없는 요청입니다."
        _, code, ts = parts
        key = f"{code}|{ts}"
        pend = bk.get("pending", {}).get(key)
        if not pend:
            return f"`{code}` 제안을 찾을 수 없습니다 (이미 처리됐거나 오래된 알림입니다)."

        age = (time.time() - float(pend.get("ts") or 0)) / 60.0
        ttl = cfg["APPROVE_TTL_MIN"]
        if age > ttl:
            bk["pending"].pop(key, None)
            return (f"⏱ *{pend['name']}* 승인 만료 — 알림 후 {age:.0f}분 지났습니다 "
                    f"(유효 {ttl}분). 그때 가격과 너무 벌어져 다른 거래가 됩니다.")

        bk["pending"].pop(key, None)
        if code in bk.get("positions", {}):
            return f"*{pend['name']}* 는 이미 보유 중입니다 — 추가 진입하지 않습니다."
        cap = int(cfg.get("MAX_POSITIONS", 30) or 0)
        if cap and len(bk.get("positions", {})) >= cap:
            return (f"동시 보유 한도({cap}종목)에 걸렸습니다. "
                    "기존 포지션이 정리되면 다시 받겠습니다.")

        q = price_fn(code) or {}
        px = float(q.get("last") or 0.0) or float(pend.get("px") or 0.0)
        if px <= 0:
            return f"*{pend['name']}* 현재가를 못 받아 주문하지 않았습니다."

        # 상한가는 매도 호가가 없어 실전에서는 시장가로도 못 산다. 모의투자는
        # 체결될 수 있는데 그게 더 나쁘다 — 실전에서 불가능한 체결이 데이터로
        # 쌓이면 나중에 수익률을 믿을 수 없게 된다. 하한가도 같은 이유로 막는다.
        now_pct = float(q.get("chg_pct") if q.get("chg_pct") is not None
                        else pend.get("pct") or 0.0)
        lim = cfg.get("PRICE_LIMIT_PCT", 29.5)
        if lim and abs(now_pct) >= lim:
            side = "상한가" if now_pct > 0 else "하한가"
            return (f"⛔ *{pend['name']}* {now_pct:+.1f}% — {side}라 주문하지 않았습니다.\n"
                    f"반대 호가가 없어 실전에서는 체결이 안 됩니다. "
                    f"모의에서만 체결되면 데이터가 오염됩니다.")
        qty = int(cfg["PAPER_ORDER_KRW"] // px)
        if qty < 1:
            return (f"*{pend['name']}* 주당 {px:,.0f}원이라 주문금액"
                    f"({cfg['PAPER_ORDER_KRW']:,}원)으로 1주도 못 삽니다.")

        # 현금을 먼저 본다. 모자라면 가능한 만큼만 산다. 조회가 실패하면(None)
        # 막지 않고 그대로 낸다 — 정말 모자라면 서버가 사유와 함께 거절한다.
        shrink = ""
        cash = pk.buy_power(code)
        if cash is not None:
            if cash < px:
                return (f"💸 *{pend['name']}* 주문가능현금 {cash:,.0f}원 — "
                        f"1주({px:,.0f}원)도 못 삽니다.")
            if qty * px > cash * 0.98:
                qty = int(cash * 0.98 // px)
                shrink = f"\n(현금이 모자라 {qty:,}주로 줄였습니다 — 가용 {cash:,.0f}원)"

        pk.last_err = ""
        o = pk.order(code, qty, "buy", ord_dvsn=str(cfg.get("PAPER_ORD_DVSN") or "01"))
        if not o:
            why = pk.last_err or "사유 불명 — Actions 로그를 확인하세요"
            return (f"❌ *{pend['name']}* {qty:,}주 @ {px:,.0f}원 주문 실패\n"
                    f"{why}")

        # σ 가 비어 있으면 일봉으로 채운다. 예전에는 여기서 손절가가 0 이 되어
        # 그 포지션은 영영 손절이 안 걸렸다.
        sd = float(pend.get("sd") or 0.0)
        if sd <= 0 and bars_fn:
            try:
                sd = sigma_from_bars(bars_fn(code)) or 0.0
            except Exception as e:
                log(f"σ 보충용 일봉 실패 [{code}]: {type(e).__name__}")

        # 접수 ≠ 체결. 실제로 몇 주가 얼마에 잡혔는지 확인한 뒤 알린다.
        f = pk.fill_of(o.get("ord_no") or "", code)
        filled, avg = (f[0], f[1]) if f else (0, 0.0)
        entry = avg if (filled > 0 and avg > 0) else px
        stop, stop_src = stop_for(entry, sd, cfg)
        tp = float(cfg.get("TAKE_PROFIT_PCT") or 0.0)
        target = entry * (1 + tp / 100) if tp > 0 else 0.0
        bk.setdefault("positions", {})[code] = {
            "name": pend["name"], "qty": filled if filled > 0 else qty,
            "ord_qty": qty, "entry": entry, "quote": px,
            "entry_ts": int(time.time()),
            "entry_date": dt.datetime.now(KST).strftime("%Y%m%d"),
            "sd": sd, "stop": stop, "stop_src": stop_src, "target": target,
            "trigger": pend.get("trigger") or "",
            "entry_pct": pend.get("pct"), "ord_no": o.get("ord_no", ""),
            "delay_min": round(age, 1)}

        nm = pend["name"]
        if f is None or filled <= 0:
            head = (f"✅ *{nm}* 매수 주문 접수 {qty:,}주 @ 약 {px:,.0f}원 "
                    f"(약 {qty * px:,.0f}원) — 체결은 잔고로 확인해 관리합니다")
        elif filled < qty:
            head = (f"🟡 *{nm}* 매수 부분체결 {filled:,}/{qty:,}주 @ {avg:,.0f}원 "
                    f"(약 {filled * avg:,.0f}원) — 나머지 {qty - filled:,}주는 미체결, "
                    f"더 잡히면 잔고 기준으로 따라갑니다")
        else:
            head = (f"✅ *{nm}* 매수 체결 {filled:,}주 @ {avg:,.0f}원 "
                    f"(약 {filled * avg:,.0f}원)")
        drop = f"{stop:,.0f}원" + (f"({stop_src})" if stop_src and stop_src != "σ" else "") \
            if stop else "미설정"
        goal = f"익절 {target:,.0f}원(+{tp:.1f}%) · " if target else ""
        return (f"{head}\n"
                f"{goal}손절 {drop} · 시간청산 T+{cfg['HOLD_DAYS']}영업일 · "
                f"승인지연 {age:.1f}분{shrink}")

    return on_approve


def _ignored(cfg: dict) -> set:
    """관리 대상에서 뺄 종목코드 (손으로 들고 있는 종목 등)."""
    raw = cfg.get("PAPER_IGNORE") or ""
    items = raw if isinstance(raw, (list, set, tuple)) else re.split(r"[,\s]+", str(raw))
    return {str(c).strip().zfill(6) for c in items if str(c).strip()}


def adopt_orphans(pk: "PaperKIS", bk: dict, bal: dict, cfg: dict, log=print,
                  bars_fn=None) -> list[str]:
    """잔고에는 있는데 장부에 없는 종목을 장부로 들인다. 알릴 메시지를 돌려준다.

    **이게 없어서 손절이 안 걸렸다.** 예전 대조는 장부→잔고 한 방향이었다 —
    장부에 있는데 잔고에 없으면 지웠지만, 잔고에 있는데 장부에 없으면 아무것도
    하지 않았다. 그렇게 빠진 종목은 손절도 시간청산도 영영 안 걸린 채 방치된다.

    장부에서 빠지는 길은 여럿이다. 통신이 끊겨 주문 성공 여부를 모른 채 넘어간
    경우, 체결이 10분 넘게 늦어 '잔고불일치' 로 지워진 뒤 뒤늦게 체결된 경우,
    오전·오후 잡이 겹쳐 오전 장부가 저장되기 전 캐시를 오후 잡이 복원한 경우.
    길을 하나씩 막는 것보다, 잔고를 진실 원천으로 두고 매 회차 거꾸로도
    맞추는 편이 확실하다 — 어떤 길로 빠졌든 다음 회차에 다시 잡힌다."""
    msgs: list[str] = []
    pos = bk.setdefault("positions", {})
    ignore = _ignored(cfg)
    now_ts = time.time()
    grace = float(cfg.get("SETTLE_GRACE_SEC", 600))
    # 방금 청산한 종목은 매도 체결 전까지 잔고에 남아 있다. 그걸 다시 들이면
    # 같은 주식을 또 팔러 간다.
    recent = {str(c.get("_code")).zfill(6)
              for c in (bk.get("closed") or [])[-50:]
              if c.get("_code") and now_ts - float(c.get("exit_ts") or 0) < grace}
    adopt = cfg.get("ADOPT_ORPHANS", True)
    today = dt.datetime.now(KST).strftime("%Y%m%d")
    for code, b in bal.items():
        if code in pos or code in ignore or code in recent:
            continue
        if b.get("sellable") == 0:
            continue                    # 매도 주문이 이미 걸려 체결 대기 중
        nm = b.get("name") or code
        if not adopt:
            warned = bk.setdefault("_orphan_warned", {})
            if warned.get(code) != today:
                warned[code] = today
                msgs.append(f"⚠️ 장부에 없는 보유종목: *{nm}* {b['qty']:,}주 "
                            f"({b.get('pnl_pct', 0):+.1f}%) — ADOPT_ORPHANS=0 이라 "
                            f"손절·시간청산을 걸지 않습니다")
            continue
        entry = float(b.get("avg") or 0.0) or float(b.get("last") or 0.0)
        if entry <= 0:
            continue
        sd = 0.0
        if bars_fn:
            try:
                sd = sigma_from_bars(bars_fn(code)) or 0.0
            except Exception as e:
                log(f"편입 σ 계산용 일봉 실패 [{code}]: {type(e).__name__}")
        stop, src = stop_for(entry, sd, cfg)
        tp = float(cfg.get("TAKE_PROFIT_PCT") or 0.0)
        bought = pk.last_buy_date(code)
        time.sleep(0.3)                 # 모의 서버 유량 한도가 낮다
        pos[code] = {
            "name": nm, "qty": int(b["qty"]), "entry": entry,
            "entry_ts": int(now_ts), "entry_date": bought or today,
            "entry_date_src": "체결내역" if bought else "발견일(체결내역 조회 실패)",
            "sd": sd, "stop": stop, "stop_src": src,
            "target": entry * (1 + tp / 100) if tp > 0 else 0.0,
            "trigger": "장부 밖 보유 → 편입", "adopted": True, "adopted_ts": int(now_ts)}
        when = f"{bought[4:6]}/{bought[6:]} 매수" if bought else "매수일 불명 → 오늘부터 셈"
        stop_s = f"{stop:,.0f}원" + (f"({src})" if src != "σ" else "") if stop else "미설정"
        log(f"장부 밖 보유 편입: {nm} {b['qty']}주 @ {entry:,.0f}")
        msgs.append(f"🔎 장부에 없던 보유종목 편입: *{nm}* {int(b['qty']):,}주 "
                    f"@ {entry:,.0f}원 (현재 {b.get('pnl_pct', 0):+.1f}%)\n"
                    f"손절 {stop_s} · {when} — 이번 회차부터 청산 규칙을 적용합니다")
    return msgs


def check_exits(pk: "PaperKIS", bk: dict, price_fn, cfg: dict, log=print,
                bars_fn=None) -> list[str]:
    """보유 포지션을 점검해 청산 조건에 닿은 것을 시장가로 판다.

    청산이 자동인 이유는 단순하다. 진입에 재량이 들어가고 청산에도 재량이
    들어가면, 결과가 좋든 나쁘든 무엇 때문인지 가릴 수가 없다."""
    msgs: list[str] = []
    pos = bk.setdefault("positions", {})
    now_ts = time.time()

    # 장부가 비어도 잔고는 본다. 예전엔 여기서 바로 돌아갔는데, 그러면 장부
    # 밖으로 빠진 보유종목은 장부가 빌 때 영영 발견되지 않는다. 다만 모의
    # 서버는 호출 한도가 낮으므로, 장부가 빈 동안에는 간격을 둔다.
    if not pos:
        gap = float(cfg.get("ORPHAN_SCAN_SEC", 300))
        if now_ts - float(bk.get("_orphan_scan_ts") or 0) < gap:
            return msgs
    bk["_orphan_scan_ts"] = now_ts

    # 잔고와 대조. 조회 실패(None)와 빈 잔고({})는 반드시 구분해야 한다.
    bal = pk.balance()
    grace = float(cfg.get("SETTLE_GRACE_SEC", 600))
    if bal is not None:
        for code in list(pos):
            if code in bal:
                continue
            # 시장가 주문도 접수와 체결 사이에 시차가 있다. 그 사이 잔고에는
            # 안 잡히는데, 그걸 '없는 포지션' 으로 읽고 지우면 방금 산 종목을
            # 장부에서 잃어버린다. (그래도 빠졌다면 편입이 다시 잡아 온다.)
            age = now_ts - float(pos[code].get("entry_ts") or 0)
            if age < grace:
                log(f"{pos[code].get('name', code)} 아직 잔고 미반영 "
                    f"({age:.0f}초 경과) — 체결 대기로 보고 유지")
                continue
            p = pos.pop(code)
            log(f"장부에만 있던 포지션 정리: {p.get('name', code)} (잔고에 없음)")
            bk.setdefault("closed", []).append(
                {**p, "_code": code, "exit_reason": "잔고불일치", "exit_ts": int(now_ts)})

        msgs += adopt_orphans(pk, bk, bal, cfg, log, bars_fn)

        # 수량·평균단가는 잔고를 따른다. 부분체결이면 주문수량이 아니라 실제
        # 체결수량이 포지션이고, 진입가도 호가가 아니라 체결평균가다. 손절·익절선은
        # 실제 진입가 기준으로 다시 긋는다 — 호가 기준이면 체결이 높게 됐을 때
        # 손절선이 실제보다 가깝게 잡혀 멀쩡한 포지션을 자른다.
        for code, p in pos.items():
            b = bal.get(code)
            if not b:
                continue
            if b.get("qty"):
                p["qty"] = int(b["qty"])
            avg = float(b.get("avg") or 0.0)
            ent = float(p.get("entry") or 0.0)
            if avg > 0 and (ent <= 0 or abs(avg / ent - 1) > 1e-4):
                p["entry"] = avg
                p["stop"], p["stop_src"] = stop_for(avg, float(p.get("sd") or 0.0), cfg)
                tp = float(cfg.get("TAKE_PROFIT_PCT") or 0.0)
                p["target"] = avg * (1 + tp / 100) if tp > 0 else 0.0

    # 손절선이 비어 있는 포지션을 메운다 (예전 버전이 σ 없이 만든 것).
    for code, p in pos.items():
        if p.get("stop"):
            continue
        sd = float(p.get("sd") or 0.0)
        if sd <= 0 and bars_fn:
            try:
                sd = sigma_from_bars(bars_fn(code)) or 0.0
                p["sd"] = sd
            except Exception:
                sd = 0.0
        p["stop"], p["stop_src"] = stop_for(float(p.get("entry") or 0.0), sd, cfg)
        if p["stop"]:
            log(f"{p.get('name', code)} 손절선이 비어 있어 새로 그었습니다: "
                f"{p['stop']:,.0f}원 ({p['stop_src']})")

    now = dt.datetime.now(KST)
    hm = now.hour * 60 + now.minute
    for code in list(pos):
        p = pos[code]
        q = price_fn(code) or {}
        px = float(q.get("last") or 0.0)
        # 현재가 조회가 실패하면 예전엔 그 회차 손절 판정을 통째로 건너뛰었다.
        # 잔고 응답에 현재가가 이미 실려 오므로 그것으로 대신한다.
        if px <= 0 and bal and code in bal:
            px = float(bal[code].get("last") or 0.0)
        # 보유일은 **실제 일봉 개수**로 센다. 주중 일수로 세면 연휴가 낀 구간에서
        # 휴장일까지 경과일로 쳐서 시간청산이 일찍 나가고, 일봉으로 재현하는
        # backfill 과 숫자가 어긋난다 (2026 추석: 라이브 3거래일 vs 시뮬 5거래일).
        held, exact = held_days(code, p.get("entry_date", ""), bars_fn,
                                bk.setdefault("held_cache", {}), log)
        if not exact and not bk.get("_warned_inexact"):
            bk["_warned_inexact"] = True
            log("일봉을 못 받아 보유일을 주중 일수로 셉니다 — 연휴가 끼면 "
                "시간청산이 일찍 나갈 수 있습니다")
        ent = float(p.get("entry") or 0.0)
        reason = ""
        # 손절을 먼저 본다. 같은 스캔에서 양쪽 조건이 다 맞는 경우는 장중에
        # 위아래로 크게 흔들렸다는 뜻이고, 그때는 나쁜 쪽을 가정하는 편이
        # 성과를 부풀리지 않는다.
        if px > 0 and p.get("stop") and px <= p["stop"]:
            reason = "손절"
        # 익절은 진입 다음 영업일부터 본다. 당일 익절을 허용하면 몇 분 만에
        # 나가는 일이 생기는데, 그건 T+5 호라이즌 전략이 아니라 스캘핑이다.
        # 더 큰 문제는 일봉 시뮬레이션이 당일 장중 경로를 볼 수 없다는 것이다 —
        # 라이브만 당일 익절하면 두 결과를 비교할 수 없고, 비교가 이 시스템의
        # 존재 이유다.
        elif px > 0 and p.get("target") and px >= p["target"] and held >= 1:
            reason = "익절"
        elif held >= cfg["HOLD_DAYS"] and (hm >= 15 * 60 + 10 or held > cfg["HOLD_DAYS"]):
            reason = "시간청산"
        if not reason:
            continue
        # 팔 수 있는 수량만 판다. 주문가능수량이 0 이면 매도가 이미 걸려 체결을
        # 기다리는 중이라, 여기서 또 내면 같은 주식에 매도가 두 번 걸린다.
        sell_q = int(p["qty"])
        sellable = (bal or {}).get(code, {}).get("sellable")
        if sellable is not None:
            if sellable <= 0:
                log(f"{p.get('name', code)} {reason} 대상이지만 매도 체결 대기 중 — 건너뜀")
                continue
            sell_q = min(sell_q, int(sellable))
        pk.last_err = ""
        o = pk.order(code, sell_q, "sell")
        if not o:
            log(f"{p.get('name', code)} {reason} 주문 실패 — 다음 회차에 다시 시도합니다"
                + (f" ({pk.last_err})" if pk.last_err else ""))
            msgs.append(f"⚠️ *{p.get('name', code)}* {reason} 주문이 실패했습니다 "
                        f"— 다음 회차에 다시 시도합니다\n{pk.last_err}")
            continue
        ret = (px / ent - 1) * 100 if (ent and px) else float("nan")
        # 왕복 거래비용: 증권거래세 0.15% + 수수료·슬리피지 가정. 모의투자는
        # 슬리피지 없이 체결되므로 이 값을 빼야 실전에 가깝다.
        net = ret - cfg["ROUND_TRIP_PCT"]
        pos.pop(code, None)
        bk.setdefault("closed", []).append(
            {**p, "_code": code, "sold_qty": sell_q,
             "exit": px, "exit_reason": reason, "exit_ts": int(time.time()),
             "exit_date": now.strftime("%Y%m%d"), "held_days": held,
             "ret_pct": round(ret, 2), "ret_net_pct": round(net, 2)})
        msgs.append(f"🔻 *{p.get('name', code)}* {reason} — {ent:,.0f} → {px:,.0f}원 "
                    f"({ret:+.2f}%, 비용차감 {net:+.2f}%), 보유 {held}영업일")
    del bk.setdefault("closed", [])[:-500]
    return msgs
