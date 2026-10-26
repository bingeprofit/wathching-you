"""네이버페이 증권 공개 JSON — 시가총액·종목 종류 목록과 국내 종목 뉴스·공시 제목.

키가 필요 없다 (2026-10-12 GitHub 러너에서 확인한 경로).
  시총 목록  한국 m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ}
             미국 api.stock.naver.com/stock/exchange/{NASDAQ|NYSE|AMEX}/marketValue
             → {코드: {"mcap": 원 또는 달러, "type": stock|etf|etn}}
             하루 한 번만 받아 state/mcap_{market}.json 에 둔다 (60초 루프·1시간 단발 모두 재사용).
  뉴스       m.stock.naver.com/api/news/stock/{코드}       (datetime YYYYMMDDHHMM, KST)
  공시       m.stock.naver.com/api/stock/{코드}/disclosure (datetime ISO)

받지 못하면 빈 값을 돌려준다. 호출하는 쪽은 '모르면 거르지 않는다' 로 처리한다.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import time
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://m.stock.naver.com/"}


def norm_us(sym: str) -> str:
    """BRK/B · BRK.B · BRK-B 를 같은 키로."""
    return re.sub(r"[^A-Z0-9]", "", (sym or "").upper())


def _get(url: str, tries: int = 3):
    for a in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.8 * (a + 1))
    return None


def _fetch_kr() -> dict:
    out = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        for p in range(1, 40):
            js = _get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}?page={p}&pageSize=100")
            st = (js or {}).get("stocks") or []
            if not st:
                break
            for s in st:
                try:
                    mc = float(str(s.get("marketValueRaw") or "").replace(",", ""))
                except ValueError:
                    mc = None
                out[str(s.get("itemCode"))] = {"mcap": mc, "type": s.get("stockEndType") or "stock"}
    return out


def _fetch_us() -> dict:
    out = {}
    for ex in ("NASDAQ", "NYSE", "AMEX"):
        for p in range(1, 80):
            js = _get(f"https://api.stock.naver.com/stock/exchange/{ex}/marketValue?page={p}&pageSize=100")
            st = (js or {}).get("stocks") or []
            if not st:
                break
            for s in st:
                try:
                    mc = float(str(s.get("marketValue") or "").replace(",", "")) * 1000   # 천 달러 단위
                except ValueError:
                    mc = None
                out[norm_us(s.get("symbolCode"))] = {"mcap": mc, "type": s.get("stockEndType") or "stock"}
    return out


def load_mcap(market: str, state_dir: str, today: str, log=print) -> dict:
    """{코드: {mcap, type}}. 오늘 받은 캐시가 있으면 그것을 쓴다. 실패하면 {}."""
    p = os.path.join(state_dir, f"mcap_{market}.json")
    try:
        d = json.load(open(p))
        if d.get("date") == today and d.get("rows"):
            return d["rows"]
    except Exception:
        pass
    t0 = time.time()
    rows = _fetch_kr() if market == "kr" else _fetch_us() if market == "us" else {}
    if len(rows) < (1500 if market == "kr" else 3000):          # 목록이 반쯤 끊긴 날은 쓰지 않는다
        log(f"시총 목록 수신 부족({len(rows)}개) — 시총·종류 필터 없이 진행")
        return {}
    os.makedirs(state_dir, exist_ok=True)
    try:
        json.dump({"date": today, "rows": rows}, open(p, "w"))
    except Exception:
        pass
    log(f"시총 목록 {market}: {len(rows):,}종목 ({time.time() - t0:.0f}초)")
    return rows


def kr_headlines(code: str, hours: int = 24) -> tuple[bool, list[str]]:
    """(받았는지, 최근 기사·공시 제목들). 받지 못했으면 (False, [])."""
    now = dt.datetime.now(KST).replace(tzinfo=None)
    got, out = False, []
    js = _get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=15&page=1", tries=2)
    if isinstance(js, list):
        got = True
        for grp in js:
            for it in (grp or {}).get("items") or []:
                try:
                    t = dt.datetime.strptime(str(it.get("datetime")), "%Y%m%d%H%M")
                except ValueError:
                    continue
                if now - t <= dt.timedelta(hours=hours):
                    body = re.sub(r"\s+", " ", (it.get("body") or "")).strip()[:120]
                    out.append(f"[뉴스 {t:%m/%d %H:%M} {it.get('officeName') or ''}] "
                               f"{it.get('titleFull') or it.get('title')} — {body}")
    js = _get(f"https://m.stock.naver.com/api/stock/{code}/disclosure?pageSize=10&page=1", tries=2)
    if isinstance(js, list):
        got = True
        for it in js:
            try:
                t = dt.datetime.fromisoformat(str(it.get("datetime"))[:19])
            except ValueError:
                continue
            if now - t <= dt.timedelta(hours=max(hours, 48)):     # 공시 시각대가 KST/UTC 모호 → 넉넉히
                out.append(f"[공시 {t:%m/%d %H:%M}] {it.get('title')}")
    return got, out[:20]


_PROFILE: dict = {}


def kr_profile(code: str) -> dict:
    """업종(WICS)과 사업 요약 한두 문장 — 와이즈리포트 기업현황(네이버 증권 '기업정보' 탭과 같은 출처).
    기사가 없어 Claude 귀인을 건너뛴 종목에도 '무슨 회사인지' 를 붙이려고 쓴다. 실패하면 {}."""
    if code in _PROFILE:
        return _PROFILE[code]
    out: dict = {}
    try:
        r = requests.get(f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}",
                         headers={"User-Agent": UA["User-Agent"]}, timeout=15)
        if r.status_code == 200:
            t = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S)
            t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
            t = re.sub(r"\s+", " ", t)
            m = re.search(r"WICS\s*:\s*(.+?)\s+(?:EPS|BPS|PER)\b", t)
            if m:
                out["wics"] = m.group(1).strip()
            i = t.find("동사는")
            if i >= 0:
                body = t[i:]
                j = body.find("펀더멘털")
                body = body[:j if j > 0 else 600].strip()
                sents = [x.strip() for x in re.split(r"(?<=[음함임됨])\.\s*", body) if x.strip()]
                summ = ""
                for x in sents:                          # 앞에서부터 160자 안쪽으로
                    if summ and len(summ) + len(x) > 160:
                        break
                    summ += x + ". "
                out["summary"] = re.sub(r"^동사는\s*", "", summ.strip())
    except Exception:
        pass
    _PROFILE[code] = out
    return out
