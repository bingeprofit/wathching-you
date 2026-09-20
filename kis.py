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


def _f(o: dict, k: str, d: float = 0.0) -> float:
    try:
        v = str(o.get(k, d)).replace(",", "").strip()
        return float(v) if v not in ("", "-", "None") else d
    except (TypeError, ValueError):
        return d


class KIS:
    def __init__(self, app_key: str, app_secret: str, paper: bool = False,
                 state_dir: str = "state", min_gap: float = 0.06, log=print):
        self.key, self.sec = app_key, app_secret
        self.base = PAPER if paper else PROD
        self.state_dir = state_dir
        self.log = log
        self.min_gap = min_gap           # 실전 유량제한 초당 20건 → 여유를 두고 ~16건/초
        self._tok, self._exp = None, 0.0
        self._last_call, self._lock = 0.0, threading.Lock()
        self._seen_err: set = set()      # 같은 오류를 매 분 찍지 않기 위해

    # ── 인증 ────────────────────────────────────────────────────────────
    def _token_path(self) -> str:
        return os.path.join(self.state_dir, "kis_token.json")

    def token(self) -> str:
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
            # 자주 보는 것: EGW00133(1분 내 재발급), EGW00201(키 오류)
            raise RuntimeError(f"KIS 토큰 발급 실패 (HTTP {r.status_code}): {str(j)[:300]}")
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

    def _get(self, path: str, tr_id: str, params: dict, timeout: int = 15) -> dict:
        self._throttle()
        h = {"authorization": f"Bearer {self.token()}", "appkey": self.key,
             "appsecret": self.sec, "tr_id": tr_id, "custtype": "P",
             "content-type": "application/json; charset=utf-8"}
        r = requests.get(f"{self.base}{path}", headers=h, params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        # KIS 는 오류도 HTTP 200 + rt_cd 로 돌려준다. 조용히 빈 결과가 되는 걸 막는다.
        if str(j.get("rt_cd", "0")) != "0":
            sig = (tr_id, str(j.get("msg_cd")))
            if sig not in self._seen_err:
                self._seen_err.add(sig)
                self.log(f"KIS 오류 [{tr_id}] {j.get('msg_cd')}: {str(j.get('msg1'))[:120]}")
        return j

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
        return {"code": code, "market": "kr", "name": o.get("hts_kor_isnm", code),
                "last": _f(o, "stck_prpr"), "chg_pct": _f(o, "prdy_ctrt"),
                "volume": _f(o, "acml_vol"), "value": _f(o, "acml_tr_pbmn"),
                "prev_close": _f(o, "stck_prpr") - _f(o, "prdy_vrss")}

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
            out.append({"code": str(code).zfill(6), "market": "kr",
                        "name": o.get("hts_kor_isnm", code), "last": _f(o, "stck_prpr"),
                        "chg_pct": _f(o, "prdy_ctrt"), "volume": _f(o, "acml_vol")})
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
        return [{"date": r.get("stck_bsop_date"), "close": _f(r, "stck_clpr"),
                 "volume": _f(r, "acml_vol")}
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
        return [{"date": r.get("xymd"), "close": _f(r, "clos"), "volume": _f(r, "tvol")}
                for r in (j.get("output2") or []) if _f(r, "clos") > 0]

    # ── 해외선물 ────────────────────────────────────────────────────────
    def fut_price(self, srs_cd: str) -> dict | None:
        """해외선물 종목현재가. 종목코드는 ROOT+월물코드+2자리연도 (예: GCZ26, CLF27).

        proc_date/proc_time(최종처리일시)을 그대로 실어 보낸다. 호출 시각과 견주면
        이 시세가 실시간인지 몇 분 지연인지 바로 알 수 있다."""
        try:
            j = self._get("/uapi/overseas-futureoption/v1/quotations/inquire-price",
                          "HHDFC55010000", {"SRS_CD": srs_cd})
        except Exception:
            return None
        o = j.get("output1") or j.get("output") or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        if not o or _f(o, "last_price") <= 0:
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
