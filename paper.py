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
import hashlib, json, os, re, time, datetime as dt
from zoneinfo import ZoneInfo
import requests

KST = ZoneInfo("Asia/Seoul")
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"

# 신 TR (모의투자는 앞에 V). 실전은 T 로 시작하지만 이 파일은 모의 전용이다.
TR_BUY, TR_SELL, TR_BAL = "VTTC0012U", "VTTC0011U", "VTTC8434R"
# 토큰이 죽었을 때 나오는 코드들. 캐시를 버리고 한 번 다시 받으면 된다.
TOKEN_DEAD = {"EGW00123", "EGW00121", "EGW00105"}


def _f(d: dict, k: str) -> float:
    try:
        return float(str(d.get(k, "") or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def weekdays_between(ymd: str, today: dt.date | None = None) -> int:
    """진입일로부터 지난 영업일 수. 공휴일 달력은 없으므로 주말만 뺀다.

    공휴일 때문에 하루 이틀 늦게 나가는 것은 허용한다. 반대로 달력을 들고
    있다가 틀리는 것보다, 늦게라도 반드시 나가는 쪽이 안전하다."""
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
            r = requests.post(f"{PAPER_BASE}/oauth2/tokenP", timeout=20,
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
            r = requests.post(f"{PAPER_BASE}/uapi/hashkey", timeout=10, json=body,
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

    def _fail(self, what: str, r, j: dict) -> None:
        code = str(j.get("msg_cd") or "")
        msg = str(j.get("msg1") or r.text)[:200]
        sig = (what, r.status_code, code)
        if sig not in self._seen:
            self._seen.add(sig)
            self.log(f"모의 {what} 실패 HTTP {r.status_code} {code}: {msg}")

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
            r = requests.post(f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/order-cash",
                              timeout=20, json=body,
                              headers=self._headers(tr, {"hashkey": hk} if hk else None))
            j = r.json()
        except Exception as e:
            self.log(f"모의 주문 통신 실패 [{code}]: {type(e).__name__} {str(e)[:120]}")
            return None
        if r.status_code != 200 or str(j.get("rt_cd", "1")) != "0":
            if str(j.get("msg_cd") or "") in TOKEN_DEAD and not _retry:
                self._clear_token()
                return self.order(code, qty, side, ord_dvsn, price, _retry=True)
            self._fail(f"주문({side})", r, j)
            return None
        o = j.get("output") or {}
        return {"ord_no": o.get("ODNO") or "", "org": o.get("KRX_FWDG_ORD_ORGNO") or "",
                "tmd": o.get("ORD_TMD") or "", "msg": str(j.get("msg1") or "")[:60]}

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
            r = requests.get(f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-balance",
                             timeout=20, params=self._bal_params(ofl, prod),
                             headers=self._headers(TR_BAL))
            j = r.json()
        except Exception as e:
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
            r = requests.get(f"{PAPER_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
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
                    "last": _f(x, "prpr"), "pnl_pct": _f(x, "evlu_pfls_rt")}
        return out

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
BOOK0 = {"offset": 0, "handled": [], "pending": {}, "positions": {}, "closed": []}


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


def make_approver(pk: "PaperKIS", bk: dict, price_fn, cfg: dict, log=print):
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
        if len(bk.get("positions", {})) >= cfg["MAX_POSITIONS"]:
            return (f"동시 보유 한도({cfg['MAX_POSITIONS']}종목)에 걸렸습니다. "
                    "기존 포지션이 정리되면 다시 받겠습니다.")

        q = price_fn(code) or {}
        px = float(q.get("last") or 0.0) or float(pend.get("px") or 0.0)
        if px <= 0:
            return f"*{pend['name']}* 현재가를 못 받아 주문하지 않았습니다."
        qty = int(cfg["PAPER_ORDER_KRW"] // px)
        if qty < 1:
            return (f"*{pend['name']}* 주당 {px:,.0f}원이라 주문금액"
                    f"({cfg['PAPER_ORDER_KRW']:,}원)으로 1주도 못 삽니다.")

        o = pk.order(code, qty, "buy")
        if not o:
            return f"❌ *{pend['name']}* 모의 주문 실패 — 로그를 확인하세요."

        sd = float(pend.get("sd") or 0.0)
        stop = px * (1 - cfg["STOP_SD"] * sd) if sd > 0 else 0.0
        bk.setdefault("positions", {})[code] = {
            "name": pend["name"], "qty": qty, "entry": px,
            "entry_ts": int(time.time()),
            "entry_date": dt.datetime.now(KST).strftime("%Y%m%d"),
            "sd": sd, "stop": stop, "trigger": pend.get("trigger") or "",
            "entry_pct": pend.get("pct"), "ord_no": o.get("ord_no", ""),
            "delay_min": round(age, 1)}
        drop = f"{stop:,.0f}원" if stop else "미설정(σ 없음)"
        return (f"✅ *{pend['name']}* 매수 {qty:,}주 @ {px:,.0f}원 "
                f"(약 {qty * px:,.0f}원)\n"
                f"손절 {drop} · 시간청산 T+{cfg['HOLD_DAYS']}영업일 · "
                f"승인지연 {age:.1f}분")

    return on_approve


def check_exits(pk: "PaperKIS", bk: dict, price_fn, cfg: dict, log=print) -> list[str]:
    """보유 포지션을 점검해 청산 조건에 닿은 것을 시장가로 판다.

    청산이 자동인 이유는 단순하다. 진입에 재량이 들어가고 청산에도 재량이
    들어가면, 결과가 좋든 나쁘든 무엇 때문인지 가릴 수가 없다."""
    msgs: list[str] = []
    pos = bk.get("positions") or {}
    if not pos:
        return msgs

    # 잔고와 대조. 조회 실패(None)와 빈 잔고({})는 반드시 구분해야 한다.
    bal = pk.balance()
    if bal is not None:
        for code in list(pos):
            if code not in bal:
                p = pos.pop(code)
                log(f"장부에만 있던 포지션 정리: {p.get('name', code)} (잔고에 없음)")
                bk.setdefault("closed", []).append(
                    {**p, "exit_reason": "잔고불일치", "exit_ts": int(time.time())})
        for code, p in pos.items():
            if bal.get(code, {}).get("qty"):
                p["qty"] = bal[code]["qty"]

    now = dt.datetime.now(KST)
    hm = now.hour * 60 + now.minute
    for code in list(pos):
        p = pos[code]
        q = price_fn(code) or {}
        px = float(q.get("last") or 0.0)
        held = weekdays_between(p.get("entry_date", ""))
        reason = ""
        if px > 0 and p.get("stop") and px <= p["stop"]:
            reason = "손절"
        elif held >= cfg["HOLD_DAYS"] and (hm >= 15 * 60 + 10 or held > cfg["HOLD_DAYS"]):
            reason = "시간청산"
        if not reason:
            continue
        o = pk.order(code, int(p["qty"]), "sell")
        if not o:
            log(f"{p.get('name', code)} 청산 주문 실패 — 다음 회차에 다시 시도합니다")
            continue
        ent = float(p.get("entry") or 0.0)
        ret = (px / ent - 1) * 100 if (ent and px) else float("nan")
        # 왕복 거래비용: 증권거래세 0.15% + 수수료·슬리피지 가정. 모의투자는
        # 슬리피지 없이 체결되므로 이 값을 빼야 실전에 가깝다.
        net = ret - cfg["ROUND_TRIP_PCT"]
        pos.pop(code, None)
        bk.setdefault("closed", []).append(
            {**p, "exit": px, "exit_reason": reason, "exit_ts": int(time.time()),
             "exit_date": now.strftime("%Y%m%d"), "held_days": held,
             "ret_pct": round(ret, 2), "ret_net_pct": round(net, 2)})
        msgs.append(f"🔻 *{p.get('name', code)}* {reason} — {ent:,.0f} → {px:,.0f}원 "
                    f"({ret:+.2f}%, 비용차감 {net:+.2f}%), 보유 {held}영업일")
    del bk.setdefault("closed", [])[:-500]
    return msgs
