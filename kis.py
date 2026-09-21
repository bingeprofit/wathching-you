"""한국투자증권 KIS OpenAPI 래퍼 — 국내·해외 시세 조회용.

엔드포인트와 tr_id 는 KIS 공식 저장소(koreainvestment/open-trading-api)의
examples_llm 기준입니다. 추측이 아니라 공식 예제에서 확인한 값입니다.

  국내 등락률 순위   /uapi/domestic-stock/v1/ranking/fluctuation      FHPST01700000
  국내 현재가        /uapi/domestic-stock/v1/quotations/inquire-price FHKST01010100
  국내 일봉          .../inquire-daily-itemchartprice                 FHKST03010100
  해외 등락률 순위   /uapi/overseas-stock/v1/ranking/updown-rate      HHDFS76290000
  해외 거래량급증    /uapi/overseas-stock/v1/ranking/volume-surge     HHDFS76270000
  해외 현재체결가    /uapi/overseas-price/v1/quotations/price         HHDFS00000300
  해외 일봉          /uapi/overseas-price/v1/quotations/dailyprice    HHDFS76240000
  해외선물 현재가    /uapi/overseas-futureoption/v1/quotations/inquire-price  HHDFC55010000
  해외선물 일봉      /uapi/overseas-futureoption/v1/quotations/daily-ccnl     HHDFC55020100

시세 지연에 대하여 (KIS 공식 문서):
  미국 주식 — 실시간 무료 (0분 지연). 홍콩·중국·일본·베트남 — 15분 지연.
  즉 미국 종목은 이 API 로 실시간 감시가 됩니다. 단 '장중 당일 시가'는 상이할 수
  있고 익일 정정된다고 명시돼 있어, 시가가 아니라 전일종가(base)를 기준으로 씁니다.

  해외선물은 다릅니다. 공식 예제에 "CME, SGX 실시간시세 유료시세 신청 필수" 라고
  적혀 있는데 그 문구는 **웹소켓 실시간 체결가/호가** 쪽에 달려 있고, 여기서 쓰는
  REST 현재가(inquire-price)가 지연인지 실시간인지는 문서에 명시가 없습니다.
  그래서 응답의 proc_date/proc_time(최종처리일시)을 현재 시각과 비교해 실제 지연을
  직접 재고 로그에 남깁니다. 첫 실행 로그를 보면 몇 분 지연인지 바로 드러납니다.

토큰은 24시간 유효하므로 state/ 에 캐시합니다. 매 실행 새로 받으면 발급 한도에 걸립니다.
"""
from __future__ import annotations
import json, os, threading, time
import requests

PROD = "https://openapi.koreainvestment.com:9443"
PAPER = "https://openapivts.koreainvestment.com:29443"

US_EXCHANGES = ["NAS", "NYS", "AMS"]     # 나스닥 / 뉴욕 / 아멕스
# 잠깐 실패했다가 다시 물으면 되는 오류들 (KIS 안내 문구가 "재조회"인 것)
TRANSIENT = {"EGW00316"}


def _f(o: dict, k: str, d: float = 0.0) -> float:
    try:
        v = str(o.get(k, d)).replace(",", "").strip()
        return float(v) if v not in ("", "-", "None") else d
    except (TypeError, ValueError):
        return d


# 한글 종목명이 실려 오는 필드는 엔드포인트마다 다르다. 순위 API 는
# hts_kor_isnm, 종목 마스터는 prdt_abrv_name 을 쓰고, 현재가 응답에는
# 아예 없을 수도 있다 (bstp_kor_isnm 은 '업종'명이라 종목명이 아니다).
# 어느 쪽이 오든 받도록 순서대로 훑고, 없으면 None 을 돌려준다.
NAME_KEYS = ("hts_kor_isnm", "prdt_abrv_name", "prdt_name", "prdt_eng_name")


def _kr_name(o: dict, code: str) -> str | None:
    """응답에서 한글 종목명. 없으면 None.

    **종목코드를 이름 대신 돌려주지 않는다.** 그렇게 하면 호출부에서
    '이름을 못 받았다' 와 '이름이 마침 코드와 같다' 를 구분할 수 없어,
    코드가 이름인 것처럼 캐시에 박혀 버린다."""
    for k in NAME_KEYS:
        v = str(o.get(k) or "").strip()
        if v and v != code and v != code.lstrip("0"):
            return v
    return None


class KIS:
    def __init__(self, app_key: str, app_secret: str, paper: bool = False,
                 state_dir: str = "state", min_gap: float = 0.12, log=print):
        self.key, self.sec = app_key, app_secret
        self.base = PAPER if paper else PROD
        self.state_dir = state_dir
        self.log = log
        # 실전 한도는 문서상 초당 20건이지만 실제로는 더 빡빡하다 (EGW00201).
        # 초당 ~8건에서 시작해, 걸릴 때마다 _get 이 스스로 간격을 늘린다.
        self.min_gap = self.base_gap = min_gap
        self._ok_streak = 0              # 연속 성공 횟수 (간격을 다시 줄이는 근거)
        self._tok, self._exp = None, 0.0
        self._last_call, self._lock = 0.0, threading.Lock()
        self._seen_err: set = set()      # 같은 오류를 매 분 찍지 않기 위해

    # ── 인증 ────────────────────────────────────────────────────────────
    def _token_path(self) -> str:
        return os.path.join(self.state_dir, "kis_token.json")

    def token(self, _retry: int = 0) -> str:
        if self._tok and time.time() < self._exp - 300:
            return self._tok
        p = self._token_path()
        if os.path.exists(p):                       # 실행 간 재사용 (발급 한도 회피)
            try:
                d = json.load(open(p))
                if d.get("base") == self.base and time.time() < d.get("exp", 0) - 300:
                    self._tok, self._exp = d["tok"], d["exp"]
                    return self._tok
            except Exception:
                pass
        r = requests.post(f"{self.base}/oauth2/tokenP", timeout=20,
                          json={"grant_type": "client_credentials",
                                "appkey": self.key, "appsecret": self.sec})
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"KIS 토큰 응답이 JSON 이 아님 (HTTP {r.status_code})")
        if "access_token" not in j:
            # EGW00133 = 1분에 1회만 발급 가능. 워크플로(한국·미국·선물)는 캐시가
            # 각각이라 같은 분에 뜨면 서로 부딪힌다. 이건 실패가 아니라 순서 문제이므로
            # 기다렸다 다시 받는다. 키가 틀린 경우(EGW00201 등)는 바로 실패시킨다.
            body = str(j)
            if _retry < 2 and ("EGW00133" in body or "1분" in str(j.get("msg1", ""))):
                self.log(f"토큰 발급 1분 제한에 걸림 — 70초 뒤 재시도 ({_retry + 1}/2)")
                time.sleep(70)
                return self.token(_retry + 1)
            # 자주 보는 것: EGW00133(1분 내 재발급), EGW00201(키 오류)
            raise RuntimeError(f"KIS 토큰 발급 실패 (HTTP {r.status_code}): {body[:300]}")
        self._tok = j["access_token"]
        self._exp = time.time() + int(j.get("expires_in", 86400))
        os.makedirs(self.state_dir, exist_ok=True)
        json.dump({"tok": self._tok, "exp": self._exp, "base": self.base}, open(p, "w"))
        self.log("KIS 토큰 신규 발급 (24시간 유효)")
        return self._tok

    # ── 공통 호출 ───────────────────────────────────────────────────────
    def _throttle(self):
        with self._lock:
            gap = time.time() - self._last_call
            if gap < self.min_gap:
                time.sleep(self.min_gap - gap)
            self._last_call = time.time()

    def _get(self, path: str, tr_id: str, params: dict, timeout: int = 15,
             _retry: int = 0) -> dict:
        """실패해도 예외를 던지지 않고 빈 dict 를 돌려준다. 대신 **무슨 일이 있었는지
        반드시 한 번은 로그로 남긴다.** 예외로 던지면 호출부의 try/except 가 삼켜서
        '아무것도 못 찾음'만 남고 원인을 알 수 없게 된다."""
        self._throttle()
        h = {"authorization": f"Bearer {self.token()}", "appkey": self.key,
             "appsecret": self.sec, "tr_id": tr_id, "custtype": "P",
             "content-type": "application/json; charset=utf-8"}
        try:
            r = requests.get(f"{self.base}{path}", headers=h, params=params, timeout=timeout)
        except Exception as e:
            self._log_once((tr_id, "net"), f"KIS 통신 실패 [{tr_id}]: {type(e).__name__} {str(e)[:120]}")
            return {}
        try:
            j = r.json()
        except ValueError:
            self._log_once((tr_id, r.status_code, "notjson"),
                           f"KIS 응답이 JSON 이 아님 [{tr_id}] HTTP {r.status_code}: {r.text[:160]}")
            return {}
        # KIS 는 오류를 HTTP 200 + rt_cd 로도, HTTP 4xx/5xx 로도 돌려준다. 둘 다 잡는다.
        if r.status_code != 200 or str(j.get("rt_cd", "0")) != "0":
            code = str(j.get("msg_cd") or "")
            # EGW00201 = 초당 거래건수 초과. 실패로 끝내면 그 종목이 통째로 빠져
            # 스캔에 구멍이 생긴다. 호출 간격을 스스로 늘리고 다시 시도한다.
            # 문서상 실전 한도는 초당 20건이지만 실제로는 더 빡빡해서, 고정값을
            # 정해 두기보다 맞을 때마다 조여 가는 편이 안전하다.
            if code == "EGW00201" and _retry < 3:
                with self._lock:
                    self.min_gap = min(round(self.min_gap * 1.6, 3), 0.4)
                self._ok_streak = 0
                self._log_once(("rate", self.min_gap),
                               f"KIS 유량 초과 → 호출 간격 {self.min_gap:.2f}초로 늘리고 재시도")
                time.sleep(0.3 * (_retry + 1))
                return self._get(path, tr_id, params, timeout, _retry + 1)
            # EGW00316 = "조회 처리 중 오류. 재 조회 수행 부탁드립니다" — KIS 서버의
            # 일시적 오류라 그냥 다시 물으면 대개 된다. 포기하면 그 종목의 일간
            # 변동성을 못 구해 z 가 0 이 되고, 아무리 급등해도 영영 안 걸린다.
            if code in TRANSIENT and _retry < 2:
                self._log_once(("transient", tr_id, code),
                               f"KIS 일시 오류 [{tr_id}] {code} → 재조회")
                time.sleep(0.5 * (_retry + 1))
                return self._get(path, tr_id, params, timeout, _retry + 1)
            self._log_once(
                (tr_id, r.status_code, code),
                f"KIS 오류 [{tr_id}] HTTP {r.status_code} {code}: "
                f"{str(j.get('msg1') or r.text)[:160]}")
            return j
        # 잘 나가고 있으면 간격을 조금씩 되돌린다. 한 번 걸렸다고 그 뒤로 계속
        # 최악의 속도로 도는 것은 낭비다 — 종목 200개면 스캔 한 번이 80초가 되어
        # 60초 루프를 넘겨 버린다. 실제로 견디는 속도를 찾아가게 한다.
        if self.min_gap > self.base_gap:
            with self._lock:
                self._ok_streak += 1
                if self._ok_streak >= 80:
                    self._ok_streak = 0
                    self.min_gap = max(round(self.min_gap * 0.85, 3), self.base_gap)
                    self._log_once(("ease", self.min_gap),
                                   f"KIS 안정적 → 호출 간격 {self.min_gap:.2f}초로 되돌림")
        return j

    def _log_once(self, sig, msg: str):
        if sig not in self._seen_err:
            self._seen_err.add(sig)
            self.log(msg)

    # ── 국내 ────────────────────────────────────────────────────────────
    def price(self, code: str) -> dict | None:
        """국내 현재가. 실패 시 None (한 종목 실패가 전체를 막지 않게)."""
        try:
            j = self._get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                          {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        except Exception:
            return None
        o = j.get("output") or {}
        if not o:
            return None
        return {"code": code, "market": "kr", "name": _kr_name(o, code),
                "last": _f(o, "stck_prpr"), "chg_pct": _f(o, "prdy_ctrt"),
                "volume": _f(o, "acml_vol"), "value": _f(o, "acml_tr_pbmn"),
                "prev_close": _f(o, "stck_prpr") - _f(o, "prdy_vrss")}

    def stock_name(self, code: str) -> str | None:
        """종목 마스터에서 한글 종목명. 시세 응답에 이름이 없을 때의 최후 수단.

        종목명은 거의 바뀌지 않으므로 호출부에서 캐시해 두고 재사용한다.
        실패하면 None — 이름 하나 때문에 알림이 막히면 안 된다."""
        j = self._get("/uapi/domestic-stock/v1/quotations/search-stock-info",
                      "CTPF1002R", {"PRDT_TYPE_CD": "300", "PDNO": code})
        o = j.get("output") or {}
        return _kr_name(o, code) if o else None

    def movers(self, updown: str = "0", limit: int = 30) -> list[dict]:
        """국내 등락률 순위. updown '0'=상승률, '1'=하락률."""
        params = {"fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20170",
                  "fid_input_iscd": "0000", "fid_rank_sort_cls_code": updown,
                  "fid_input_cnt_1": "0", "fid_prc_cls_code": "0", "fid_input_price_1": "",
                  "fid_input_price_2": "", "fid_vol_cnt": "", "fid_trgt_cls_code": "0",
                  "fid_trgt_exls_cls_code": "0", "fid_div_cls_code": "0",
                  "fid_rsfl_rate1": "", "fid_rsfl_rate2": ""}
        try:
            j = self._get("/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000", params)
        except Exception as e:
            self.log(f"국내 등락률 순위 실패: {type(e).__name__} {str(e)[:100]}")
            return []
        out = []
        for o in (j.get("output") or [])[:limit]:
            code = o.get("stck_shrn_iscd")
            if not code:
                continue
            # 거래대금(acml_tr_pbmn)까지 받는다. 등락률 순위 상위는 소형주가
            # 지배하는데, 그 구분은 거래대금으로만 할 수 있다. 지금 임계로
            # 거르지는 않고 로그에 남겨서 나중에 분석에서 갈라 보게 한다.
            out.append({"code": str(code).zfill(6), "market": "kr",
                        "name": _kr_name(o, str(code).zfill(6)),
                        "last": _f(o, "stck_prpr"),
                        "chg_pct": _f(o, "prdy_ctrt"), "volume": _f(o, "acml_vol"),
                        "value": _f(o, "acml_tr_pbmn")})
        return out

    def daily(self, code: str, days: int = 45) -> list[dict]:
        """국내 일봉 (최신순). [{date, close, volume}]"""
        import datetime as dt
        end = dt.datetime.now().strftime("%Y%m%d")
        bgn = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y%m%d")
        try:
            j = self._get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                          "FHKST03010100",
                          {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                           "FID_INPUT_DATE_1": bgn, "FID_INPUT_DATE_2": end,
                           "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        except Exception:
            return []
        # 고가·저가까지 받는다. 진입 뒤 어디까지 갔는지를 알아야 "몇 %에서
        # 익절했으면 어땠을까" 를 사후에 계산할 수 있다.
        return [{"date": r.get("stck_bsop_date"), "close": _f(r, "stck_clpr"),
                 "high": _f(r, "stck_hgpr"), "low": _f(r, "stck_lwpr"),
                 "open": _f(r, "stck_oprc"), "volume": _f(r, "acml_vol")}
                for r in (j.get("output2") or []) if _f(r, "stck_clpr") > 0]

    # ── 해외 (미국) ─────────────────────────────────────────────────────
    def price_os(self, excd: str, symb: str) -> dict | None:
        """해외 현재체결가. base=전일종가, rate=등락율 을 함께 준다."""
        try:
            j = self._get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                          {"AUTH": "", "EXCD": excd, "SYMB": symb})
        except Exception:
            return None
        o = j.get("output") or {}
        if not o or _f(o, "last") <= 0:
            return None
        return {"code": symb, "market": "us", "excd": excd, "name": symb,
                "last": _f(o, "last"), "chg_pct": _f(o, "rate"),
                "volume": _f(o, "tvol"), "value": _f(o, "tamt"),
                "prev_close": _f(o, "base"), "prev_volume": _f(o, "pvol")}

    def movers_os(self, excd: str, gubn: str = "1", limit: int = 20,
                  vol_rang: str = "3") -> list[dict]:
        """해외 등락률 순위. **gubn '1'=상승율, '0'=하락율 — 국내와 반대다.**
        vol_rang '3' = 1만주 이상만 (거래 없는 껍데기 종목 제거)."""
        try:
            j = self._get("/uapi/overseas-stock/v1/ranking/updown-rate", "HHDFS76290000",
                          {"EXCD": excd, "NDAY": "0", "GUBN": gubn,
                           "VOL_RANG": vol_rang, "AUTH": "", "KEYB": ""})
        except Exception as e:
            self.log(f"해외 등락률 순위({excd}) 실패: {type(e).__name__} {str(e)[:100]}")
            return []
        return self._os_rows(j.get("output2") or [], excd, limit)

    def volume_surge_os(self, excd: str, minx: str = "3", limit: int = 10,
                        vol_rang: str = "3") -> list[dict]:
        """해외 거래량급증 순위. minx '3' = 5분전 대비.
        지금 막 거래가 몰리는 종목을 KIS 가 직접 골라주므로 순간급변 후보로 쓴다."""
        try:
            j = self._get("/uapi/overseas-stock/v1/ranking/volume-surge", "HHDFS76270000",
                          {"EXCD": excd, "MINX": minx, "VOL_RANG": vol_rang,
                           "KEYB": "", "AUTH": ""})
        except Exception as e:
            self.log(f"해외 거래량급증({excd}) 실패: {type(e).__name__} {str(e)[:100]}")
            return []
        return self._os_rows(j.get("output2") or [], excd, limit)

    @staticmethod
    def _os_rows(rows, excd: str, limit: int) -> list[dict]:
        out = []
        for o in (rows if isinstance(rows, list) else [])[:limit]:
            symb = (o.get("symb") or "").strip()
            if not symb:
                continue
            out.append({"code": symb, "market": "us", "excd": o.get("excd") or excd,
                        "name": (o.get("ename") or o.get("enam") or o.get("name")
                                 or o.get("knam") or symb).strip() or symb,
                        "last": _f(o, "last"), "chg_pct": _f(o, "rate"),
                        "volume": _f(o, "tvol"), "value": _f(o, "tamt")})
        return out

    def daily_os(self, excd: str, symb: str) -> list[dict]:
        """해외 일봉 (최신순). [{date, close, volume}]"""
        try:
            j = self._get("/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
                          {"AUTH": "", "EXCD": excd, "SYMB": symb,
                           "GUBN": "0", "BYMD": "", "MODP": "1"})
        except Exception:
            return []
        return [{"date": r.get("xymd"), "close": _f(r, "clos"),
                 "high": _f(r, "high"), "low": _f(r, "low"), "open": _f(r, "open"),
                 "volume": _f(r, "tvol")}
                for r in (j.get("output2") or []) if _f(r, "clos") > 0]

    # ── 해외선물 ────────────────────────────────────────────────────────
    def fut_price(self, srs_cd: str) -> dict | None:
        """해외선물 종목현재가. 종목코드는 ROOT+월물코드+2자리연도 (예: GCZ26, CLF27).

        proc_date/proc_time(최종처리일시)을 그대로 실어 보낸다. 호출 시각과 견주면
        이 시세가 실시간인지 몇 분 지연인지 바로 알 수 있다."""
        j = self._get("/uapi/overseas-futureoption/v1/quotations/inquire-price",
                      "HHDFC55010000", {"SRS_CD": srs_cd})
        o = j.get("output1") or j.get("output") or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        if not o or _f(o, "last_price") <= 0:
            # 응답은 왔는데 시세가 없는 경우. 권한 문제인지, 종목코드가 없는 건지,
            # 응답 구조가 내 가정과 다른 건지 구분되도록 실제 모양을 한 번 남긴다.
            if j:
                self._log_once(("futshape",),
                               f"해외선물 시세 없음 (예: {srs_cd}) — 응답 키 {list(j)[:6]}, "
                               f"rt_cd={j.get('rt_cd')}, output1={str(o)[:120]}")
            return None
        return {"code": srs_cd, "market": "fut", "name": srs_cd,
                "last": _f(o, "last_price"), "chg_pct": _f(o, "prev_diff_rate"),
                "volume": _f(o, "vol"), "prev_close": _f(o, "prev_price"),
                "exch": (o.get("exch_cd") or "").strip(),
                "remain_days": _f(o, "remn_cnt"), "expiry": o.get("expr_date"),
                "proc_date": (o.get("proc_date") or "").strip(),
                "proc_time": (o.get("proc_time") or "").strip()}

    def fut_daily(self, srs_cd: str, exch_cd: str = "CME", count: int = 40) -> list[dict]:
        """해외선물 일봉 (일자별 체결). [{date, close, volume}]"""
        import datetime as dt
        try:
            j = self._get("/uapi/overseas-futureoption/v1/quotations/daily-ccnl",
                          "HHDFC55020100",
                          {"SRS_CD": srs_cd, "EXCH_CD": exch_cd or "CME",
                           "START_DATE_TIME": "",
                           "CLOSE_DATE_TIME": dt.datetime.now().strftime("%Y%m%d"),
                           "QRY_TP": "Q", "QRY_CNT": str(min(count, 40)),
                           "QRY_GAP": "", "INDEX_KEY": ""})
        except Exception:
            return []
        return [{"date": r.get("data_date"), "close": _f(r, "last_price"),
                 "volume": _f(r, "vol")}
                for r in (j.get("output2") or []) if _f(r, "last_price") > 0]

    def fut_probe(self, symbols: list[str]) -> None:
        """진단 전용. 해외선물 상품기본정보로 종목코드 여러 개를 한 번에 물어보고
        응답을 그대로 로그에 남긴다. 근월물을 하나도 못 찾았을 때 그 이유가
        (1) 시세 권한 없음 (2) 종목코드 형식이 다름 (3) 응답 구조가 다름
        중 무엇인지 가르는 데 쓴다. 호출 1회면 끝나므로 비용이 없다."""
        syms = [s for s in symbols if s][:32]
        if not syms:
            return
        params = {"QRY_CNT": str(len(syms))}
        for i, s in enumerate(syms, 1):
            params[f"SRS_CD_{i:02d}"] = s
        j = self._get("/uapi/overseas-futureoption/v1/quotations/search-contract-detail",
                      "HHDFC55200000", params)
        if not j:
            self.log("진단: 상품기본정보도 응답이 없습니다 → 해외선물 시세 권한 문제로 보입니다")
            return
        rows = j.get("output") or j.get("output1") or j.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        self.log(f"진단: 상품기본정보 rt_cd={j.get('rt_cd')} msg={str(j.get('msg1'))[:80]} "
                 f"응답키={list(j)[:6]} 행수={len(rows) if isinstance(rows, list) else '?'}")
        for r in (rows if isinstance(rows, list) else [])[:3]:
            if isinstance(r, dict):
                keep = {k: r.get(k) for k in
                        ("srs_cd", "exch_cd", "clas_cd", "expr_date", "remn_cnt", "stat_tp")
                        if k in r}
                self.log(f"진단: 샘플 {keep or list(r)[:8]}")

    # 해외는 종목코드만으로는 거래소를 모른다. 한 번 찾으면 state/ 에 적어 두고 재사용한다.
    def resolve_excd(self, symb: str, cache: dict) -> str | None:
        if symb in cache:
            return cache[symb] or None
        for ex in US_EXCHANGES:
            if self.price_os(ex, symb):
                cache[symb] = ex
                return ex
        cache[symb] = ""                 # 못 찾은 것도 기록해 매 분 3번씩 재시도하지 않게
        return None
