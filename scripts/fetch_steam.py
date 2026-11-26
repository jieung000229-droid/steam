"""
스팀 할인 게임 + 출시 예정 게임 정보를 모아서 docs/data.json 으로 저장하는 스크립트.

GitHub Actions 가 하루 두 번 자동으로 실행합니다. 직접 고칠 필요는 없고,
가져올 게임 수만 바꾸고 싶다면 아래 '설정' 부분의 숫자만 바꾸면 됩니다.

추가 설치가 필요 없는 파이썬 기본 기능만 사용합니다.
데이터 출처: 스팀 상점 공개 정보 (비공식)
"""

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ───────────── 설정 ─────────────
MAX_SALE = int(os.environ.get("MAX_SALE", 150))          # 할인 게임 최대 몇 개
MAX_UPCOMING = int(os.environ.get("MAX_UPCOMING", 150))  # 출시 예정 게임 최대 몇 개
KEEP_RELEASED_DAYS = 31   # 출시된 뒤에도 달력에 며칠 동안 남겨 둘지
REQUEST_GAP = 1.6         # 스팀 요청 사이 쉬는 시간(초). 줄이면 차단될 수 있어요.
# ────────────────────────────────

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data.json")
KST = dt.timezone(dt.timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 스팀 카테고리 번호 → 플레이 방식
CAT_SINGLE = {2}
CAT_MULTI = {1, 36, 49, 47, 27}       # 멀티플레이, 온라인 PvP 등
CAT_COOP = {9, 38, 48, 39, 24, 44}    # 협동, 온라인 협동, 로컬 협동 등


def log(*a):
    print(*a, flush=True)


def http_get(url, as_json=True, tries=4):
    """스팀에 요청. 너무 많이 요청해서 막히면 잠깐 기다렸다가 다시 시도."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Cookie": "birthtime=631152001; lastagecheckage=1-0-1990; wants_mature_content=1",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(REQUEST_GAP)
            if not as_json:
                return body
            data = json.loads(body)
            if data is None:  # appdetails 는 막히면 null 을 돌려줌
                raise ValueError("빈 응답(null) - 요청 제한 가능성")
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError) as e:
            last = e
            wait = 30 * (i + 1) if "429" in str(e) or "null" in str(e) else 5 * (i + 1)
            log(f"  ! 요청 실패({e}) → {wait}초 후 재시도")
            time.sleep(wait)
    log(f"  ✗ 포기: {url[:120]} ({last})")
    return None


# ───────── 1) 스팀 검색에서 appid 목록 가져오기 ─────────

def search_appids(extra_params, limit):
    ids, start = [], 0
    while len(ids) < limit:
        params = {
            "start": start, "count": 100, "infinite": 1,
            "cc": "kr", "l": "koreana",
            "category1": 998,          # 998 = 게임만 (DLC·사운드트랙 제외)
            "ndl": 1,
        }
        params.update(extra_params)
        url = "https://store.steampowered.com/search/results/?" + urllib.parse.urlencode(params)
        data = http_get(url)
        if not data or not data.get("results_html"):
            break
        found = parse_search_html(data["results_html"])
        if not found:
            break
        for appid in found:
            if appid not in ids:
                ids.append(appid)
        start += 100
        if start >= int(data.get("total_count", 0) or 0):
            break
    return ids[:limit]


def parse_search_html(results_html):
    # 번들(여러 appid 가 쉼표로 묶인 것)은 건너뛰고 단일 게임만
    return [int(x) for x in re.findall(r'data-ds-appid="(\d+)"', results_html)]


# ───────── 2) 게임 상세 정보 (장르, 가격, 언어, 출시일 등) ─────────

def app_details(appid):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=kr&l=koreana"
    data = http_get(url)
    if not data:
        return None
    entry = data.get(str(appid)) or {}
    if not entry.get("success"):
        return None
    return entry.get("data")


def has_korean(langs):
    if not langs:
        return False
    return ("한국어" in langs) or ("Korean" in langs)


def korean_voice(langs):
    """'한국어<strong>*</strong>' 처럼 별표가 붙으면 음성까지 지원"""
    if not langs:
        return False
    m = re.search(r"(한국어|Korean)\s*<strong>\*</strong>", langs)
    return bool(m)


EN_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_release_text(text):
    """스팀 출시일 글자 → (YYYY-MM-DD 또는 None, 정확도)
    정확도: day(날짜 확정) / month / quarter / year / unknown"""
    t = (text or "").strip()
    if not t:
        return None, "unknown"
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}", "day"
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월", t)
    if m:
        y, mo = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-01", "month"
    m = re.search(r"(\d{4})\s*년\s*(\d)\s*분기", t) or re.search(r"Q(\d)\s*(\d{4})", t)
    if m:
        a, b = m.groups()
        y, q = (int(a), int(b)) if len(a) == 4 else (int(b), int(a))
        return f"{y:04d}-{(q - 1) * 3 + 1:02d}-01", "quarter"
    # 영어 형식: "4 Dec, 2026" / "Dec 4, 2026" / "December 2026"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s+(\d{4})", t)
    if m and m.group(2).lower() in EN_MONTHS:
        return f"{int(m.group(3)):04d}-{EN_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}", "day"
    m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})", t)
    if m and m.group(1).lower() in EN_MONTHS:
        return f"{int(m.group(3)):04d}-{EN_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}", "day"
    m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{4})", t)
    if m and m.group(1).lower() in EN_MONTHS:
        return f"{int(m.group(2)):04d}-{EN_MONTHS[m.group(1).lower()]:02d}-01", "month"
    m = re.search(r"(\d{4})", t)
    if m:
        return f"{int(m.group(1)):04d}-01-01", "year"
    return None, "unknown"


def build_game(appid, d):
    cats = {c.get("id") for c in (d.get("categories") or [])}
    po = d.get("price_overview") or {}
    rel = d.get("release_date") or {}
    rel_text = rel.get("date", "")
    rel_date, rel_prec = parse_release_text(rel_text)
    langs = d.get("supported_languages", "")
    return {
        "id": appid,
        "name": d.get("name", ""),
        "img": d.get("header_image", ""),
        "url": f"https://store.steampowered.com/app/{appid}/",
        "desc": html.unescape(re.sub(r"<[^>]+>", "", d.get("short_description", "")))[:220],
        "genres": [g.get("description") for g in (d.get("genres") or []) if g.get("description")],
        "dev": ", ".join(d.get("developers") or [])[:60],
        "free": bool(d.get("is_free")),
        "price": {
            "orig": (po.get("initial") or 0) / 100,
            "final": (po.get("final") or 0) / 100,
            "pct": po.get("discount_percent") or 0,
            "orig_text": po.get("initial_formatted") or po.get("final_formatted") or "",
            "final_text": po.get("final_formatted") or "",
        } if po else None,
        "sale_end": None,  # 아래에서 채움 (유닉스 시간)
        "release": {"text": rel_text, "date": rel_date, "precision": rel_prec,
                    "coming_soon": bool(rel.get("coming_soon"))},
        "korean": has_korean(langs),
        "korean_voice": korean_voice(langs),
        "single": bool(cats & CAT_SINGLE),
        "multi": bool(cats & CAT_MULTI),
        "coop": bool(cats & CAT_COOP),
        "demo": bool(d.get("demos")),
        "mac": bool((d.get("platforms") or {}).get("mac")),
    }


# ───────── 3) 할인 종료일 ─────────

def find_key(obj, key):
    """중첩된 JSON 안에서 특정 이름의 값을 모두 찾기"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            out.extend(find_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_key(v, key))
    return out


def sale_ends_from_storebrowse(appids):
    """스팀 상점 API(IStoreBrowseService/GetItems)에서 할인 종료 시각 + 정확한 출시 시각"""
    ends, releases = {}, {}
    for i in range(0, len(appids), 40):
        chunk = appids[i:i + 40]
        payload = {
            "ids": [{"appid": a} for a in chunk],
            "context": {"language": "koreana", "country_code": "KR", "steam_realm": 1},
            "data_request": {"include_release": True, "include_all_purchase_options": True},
        }
        url = ("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json="
               + urllib.parse.quote(json.dumps(payload, separators=(",", ":"))))
        data = http_get(url)
        items = ((data or {}).get("response") or {}).get("store_items") or []
        for it in items:
            appid = it.get("appid") or it.get("id")
            if not appid:
                continue
            bpo = it.get("best_purchase_option") or {}
            cands = [int(x) for x in find_key(bpo, "discount_end_date") if str(x).isdigit()]
            if cands:
                ends[int(appid)] = min(cands)
            r = it.get("release") or {}
            ts = r.get("steam_release_date")
            disp = r.get("coming_soon_display", "")
            if ts and str(ts).isdigit() and (not r.get("is_coming_soon") or disp == "date_full"):
                releases[int(appid)] = int(ts)
    return ends, releases


def sale_ends_from_featured():
    """스팀 '특별 할인' 목록에 있는 할인 종료 시각 (보조 자료)"""
    data = http_get("https://store.steampowered.com/api/featuredcategories?cc=kr&l=koreana")
    ends = {}
    for it in ((data or {}).get("specials") or {}).get("items", []):
        if it.get("id") and it.get("discount_expiration"):
            ends[int(it["id"])] = int(it["discount_expiration"])
    return ends


def sale_end_from_store_page(appid, now):
    """마지막 방법: 게임 상점 페이지의 '할인 종료: 12월 3일' 글자 읽기"""
    page = http_get(f"https://store.steampowered.com/app/{appid}/?cc=kr&l=koreana", as_json=False)
    if not page:
        return None
    m = re.search(r"InitDailyDealTimer\(\s*\$DiscountCountdown,\s*(\d{9,11})", page)
    if m:
        return int(m.group(1))
    m = re.search(r'game_purchase_discount_countdown">[^<]*?(\d{1,2})\s*월\s*(\d{1,2})\s*일', page)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = now.year + (1 if mo < now.month - 1 else 0)
        end = dt.datetime(y, mo, d, 3, 0, tzinfo=KST)  # 스팀 할인은 보통 한국시간 새벽 3시 종료
        return int(end.timestamp())
    m = re.search(r"game_purchase_discount_countdown\">[^<]*?(\d{1,2})\s+([A-Za-z]{3})", page)
    if m and m.group(2).lower() in EN_MONTHS:
        mo, d = EN_MONTHS[m.group(2).lower()], int(m.group(1))
        y = now.year + (1 if mo < now.month - 1 else 0)
        return int(dt.datetime(y, mo, d, 3, 0, tzinfo=KST).timestamp())
    return None


def ts_to_kst_date(ts):
    return dt.datetime.fromtimestamp(int(ts), KST).strftime("%Y-%m-%d")


# ───────── 실행 ─────────

def load_previous():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    now = dt.datetime.now(KST)
    prev = load_previous()
    log(f"▶ 시작 {now:%Y-%m-%d %H:%M} (한국시간)")

    log("1. 할인 중인 게임 목록")
    sale_ids = search_appids({"specials": 1}, MAX_SALE)
    log(f"   {len(sale_ids)}개")

    log("2. 출시 예정 게임 목록")
    up_ids = search_appids({"filter": "popularcomingsoon"}, MAX_UPCOMING)
    log(f"   {len(up_ids)}개")

    if not sale_ids and not up_ids:
        log("✗ 스팀에서 목록을 못 가져왔어요. 기존 데이터를 그대로 둡니다.")
        sys.exit(1)

    log("3. 게임별 상세 정보")
    games = {}
    all_ids = list(dict.fromkeys(sale_ids + up_ids))
    for n, appid in enumerate(all_ids, 1):
        d = app_details(appid)
        if d and d.get("type") in ("game", None):
            games[appid] = build_game(appid, d)
        if n % 25 == 0:
            log(f"   {n}/{len(all_ids)}")

    log("4. 할인 종료일 · 정확한 출시 시각")
    ends, rel_ts = sale_ends_from_storebrowse(all_ids)
    for k, v in sale_ends_from_featured().items():
        ends.setdefault(k, v)
    missing = [a for a in sale_ids if a in games and a not in ends
               and (games[a]["price"] or {}).get("pct")]
    log(f"   API로 {len(ends)}개 확인, 상점 페이지에서 {len(missing)}개 추가 확인")
    for appid in missing:
        e = sale_end_from_store_page(appid, now)
        if e:
            ends[appid] = e

    for appid, g in games.items():
        if appid in ends:
            g["sale_end"] = ends[appid]
        if appid in rel_ts:  # 미국 시간 기준 날짜를 한국 날짜로 맞춤
            g["release"]["date"] = ts_to_kst_date(rel_ts[appid])
            g["release"]["precision"] = "day"

    sales = [games[a] for a in sale_ids if a in games and (games[a]["price"] or {}).get("pct")]
    upcoming = [games[a] for a in up_ids if a in games]

    # 이미 출시된 게임도 한 달 동안은 달력에 남겨 두기
    cutoff = (now - dt.timedelta(days=KEEP_RELEASED_DAYS)).strftime("%Y-%m-%d")
    have = {g["id"] for g in upcoming}
    for g in prev.get("upcoming", []):
        r = g.get("release") or {}
        if g.get("id") not in have and r.get("precision") == "day" and r.get("date", "") >= cutoff:
            upcoming.append(g)

    out = {
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "source": "스팀 상점 공개 정보 (비공식)",
        "sales": sales,
        "upcoming": upcoming,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"✔ 저장 완료: 할인 {len(sales)}개, 출시 예정 {len(upcoming)}개")


if __name__ == "__main__":
    main()
