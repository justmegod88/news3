import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, parse_qs
import re
import html

import feedparser
import yaml
from dateutil import parser as date_parser

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"


# =========================
# ✅ Debug toggle
# - config.yaml에 debug: true 넣으면 로그가 많이 찍힘
# - 또는 환경변수 DEBUG=1 로 켤 수 있음
# =========================
import os
def _debug_enabled(cfg=None) -> bool:
    if os.getenv("DEBUG", "").strip() in ("1", "true", "True", "YES", "yes"):
        return True
    if cfg and isinstance(cfg, dict):
        return bool(cfg.get("debug", False))
    return False

def _dprint(cfg, msg: str):
    if _debug_enabled(cfg):
        print(msg)


# =========================
# Data model
# =========================
@dataclass
class Article:
    title: str
    link: str
    published: dt.datetime
    source: str
    summary: str
    image_url: Optional[str] = None
    is_naver: bool = False


# =========================
# Exclusion rules  (✅ 여기 "단어" 그대로 유지)
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
  # "매출", "영업이익", "순이익", "배당","부동산",
    "상장", "ipo", "공모", "증권", "리포트","선물",
    "목표주가", "시가총액", "ir", "주주",
]

# ✅ 약업(야쿠프/약업신문) 도메인: 날짜 오류(과거 기사 유입) 방지용
YAKUP_BLOCK_HOSTS = [
    "yakup.com", "www.yakup.com",
    "yakup.co.kr", "www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업", "약업신문", "약학신문", "yakup"]

# ✅ 재배포/애그리게이터(원문 아닌 경우가 많아서 날짜 오염 유발) - 우선 차단
AGGREGATOR_BLOCK_HOSTS = [
    "msn.com", "www.msn.com",
    "flipboard.com", "www.flipboard.com",
    "smartnews.com", "www.smartnews.com",
    "newsbreak.com", "www.newsbreak.com",
]

# ✅ (추가) 구글뉴스 RSS에서 링크가 news.google.com으로 남는 경우가 많아서
# ✅ source(언론사명)로도 재배포를 차단
AGGREGATOR_BLOCK_SOURCES = [
    "msn",
    "flipboard",
    "smartnews",
    "newsbreak",
]

# 연예 / 예능 / 오락
ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬","공연", "문화",
    "유튜버", "크리에이터","특훈","스포츠","매달","선수",
    "화제", "논란", "근황","게임","스타트업",
    "팬미팅", "콘서트",
]

# 인사 / 승진
PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입","양성",
]

# 가수 다비치
DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인","개그맨", "연기", "배우","뮤지컬","뮤지션","1위",
    "콘서트", "공연", "뮤직비디오","강민경","이해리","개그","듀오","카메라","드라마","연극","탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예","무대","히든싱어","가요","음악","시상식", "프로그램",
]

# 얼굴/뷰티 노안
FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티","카메라","나이", "젊은데","패션",
]

# 포털 광고/ 낚시형 요약 문구
AD_SNIPPET_HINTS = [
    "모두가 속았다", "이걸 몰랐", "충격", "지금 확인", "알고 보니", "이유는?", "화제",
    "논란", "깜짝","지금 다운로드", "지금 클릭", "지금 확인",
]

# 광학/렌즈 업계 화이트리스트
INDUSTRY_WHITELIST = [
    "안경", "안경원","안경사", "호야", "에실로", "노안 렌즈", "노안 교정",
    "렌즈", "콘택트", "콘택트렌즈","오렌즈", "하피크리스틴",
    "안과", "검안", "시력","콘택트 렌즈", "contact lens",
    "아큐브", "acuvue",
    "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬","쿠퍼 비젼",
    "인터로조", 
    "쿠퍼", "렌즈미",
]

# ✅ (추가) 무신사/K패션 같은 "패션 잡음" 차단용
# - 단, INDUSTRY_WHITELIST가 있으면 살림(네가 원한 동작)
FASHION_HINTS = [
    "무신사", "k패션", "패션", "의류", "룩북", "컬렉션", "오프화이트", "오프화이트",
    "스타일", "코디", "브랜드", "쇼핑", "온라인몰", "패션플랫폼", "편집숍"
]


# =========================
# Utils
# =========================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _is_aggregator_host(host: str) -> bool:
    """
    ✅ 재배포/애그리게이터 도메인 차단용
    - 정확히 일치 + 서브도메인까지 커버(endswith)
    """
    h = (host or "").lower().strip()
    if not h:
        return False

    # netloc에 포트가 붙는 케이스 제거
    if ":" in h:
        h = h.split(":", 1)[0]

    for b in AGGREGATOR_BLOCK_HOSTS:
        b = b.lower()
        if h == b or h.endswith("." + b):
            return True
    return False


def _is_aggregator_source(source: str) -> bool:
    """
    ✅ (추가) source(언론사명) 기반 차단
    - 구글뉴스 RSS에서 link가 news.google.com으로 남는 케이스 방어
    """
    s = (source or "").strip().lower()
    if not s:
        return False
    # "MSN" / "msn" / "MSN Korea" 같은 변형도 커버
    return any(b in s for b in AGGREGATOR_BLOCK_SOURCES)


# ✅ (추가) 너 코드에서 호출하는데 정의가 없어서 런타임 에러나는 함수
def _has_industry_whitelist(full: str) -> bool:
    return any(i in full for i in INDUSTRY_WHITELIST)


# ✅ 디버깅을 위해 "왜 제외됐는지" reason을 리턴하는 버전도 제공 (기본 동작은 그대로 bool)
def should_exclude_article(title: str, summary: str = "", return_reason: bool = False):
    full = _normalize(title + " " + summary)

    # ✅ (추가) 무신사/K패션 잡음 제거
    # - 화이트리스트(콘택트렌즈 등) 있으면 살림
    if any(h in full for h in FASHION_HINTS):
        if not _has_industry_whitelist(full):
            return (True, "FASHION_HINTS") if return_reason else True

    # 1) 투자 / 재무
    if any(k in full for k in FINANCE_KEYWORDS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return (True, "FINANCE_KEYWORDS") if return_reason else True

    # 2) 얼굴/뷰티 노안
    if "노안" in full and any(k in full for k in FACE_AGING_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return (True, "FACE_AGING_HINTS + 노안") if return_reason else True

    # 3) 가수 다비치
    if any(n in full for n in DAVICHI_SINGER_NAMES):
        return (True, "DAVICHI_SINGER_NAMES") if return_reason else True
    if "다비치" in full or "davichi" in full:
        if any(h in full for h in DAVICHI_SINGER_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
                return (True, "DAVICHI_SINGER_HINTS") if return_reason else True

    # 4) 연예 / 예능 / 오락
    if any(h in full for h in ENTERTAINMENT_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return (True, "ENTERTAINMENT_HINTS") if return_reason else True

    # 5) 타 업계 인사 / 승진
    if any(h in full for h in PERSONNEL_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return (True, "PERSONNEL_HINTS") if return_reason else True

    # 6) 포털 광고 / 카드형 요약 제거
    if summary:
        if any(h in summary for h in AD_SNIPPET_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
                return (True, "AD_SNIPPET_HINTS") if return_reason else True

    # 7) 요약이 너무 짧은 카드형 문구 제거
    if summary and len(summary) < 40:
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return (True, "SHORT_SUMMARY(<40)") if return_reason else True

    return (False, "") if return_reason else False


# =========================
# Config / Timezone
# =========================
def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_tz(cfg):
    tz_name = cfg.get("timezone", "Asia/Seoul")
    if ZoneInfo:
        return ZoneInfo(tz_name)
    from dateutil import tz
    return tz.gettz(tz_name)


def _safe_now(tz):
    return dt.datetime.now(tz)


# =========================
# ✅ Newsletter publish anchor (NEW)
# =========================
def _get_newsletter_anchor(cfg, tz) -> dt.datetime:
    """
    뉴스레터 '발행 기준시간' anchor.
    - config.yaml에 newsletter_publish_hour가 있으면 그 시각 기준으로 잡음 (0~23)
    - 없으면 기존처럼 '현재 시각' 기준(= now)으로 동작
    """
    now = _safe_now(tz)
    h = cfg.get("newsletter_publish_hour", None)
    try:
        if h is None:
            return now
        h = int(h)
        if h < 0 or h > 23:
            return now
        anchor = now.replace(hour=h, minute=0, second=0, microsecond=0)
        # 만약 지금이 아직 발행시각 이전이라면(예: 새벽에 돌았는데 publish_hour=9),
        # anchor는 '오늘 9시'가 아니라 '어제 9시'가 되어야 어제 범위를 제대로 잡음
        if now < anchor:
            anchor = anchor - dt.timedelta(days=1)
        return anchor
    except Exception:
        return now


# =========================
# Helpers
# =========================
def parse_rss_datetime(value, tz):
    d = date_parser.parse(value)
    if d.tzinfo is None:
        return d.replace(tzinfo=tz)
    return d.astimezone(tz)


def build_google_news_url(query):
    return f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=ko&gl=KR&ceid=KR:ko"


def clean_summary(raw):
    text = raw or ""
    text = re.sub(r"<.*?>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_google_title_and_press(raw_title: str) -> Tuple[str, str]:
    if " - " not in raw_title:
        return raw_title.strip(), ""
    parts = raw_title.split(" - ")
    return " - ".join(parts[:-1]).strip(), parts[-1].strip()


def resolve_final_url(link: str) -> str:
    try:
        qs = parse_qs(urlparse(link).query)
        if "url" in qs:
            return qs["url"][0]
    except Exception:
        pass
    return link


# =========================
# ✅ Naver relative/absolute time parse (NEW)
# =========================
_NAVER_REL_RE = re.compile(r"^\s*(\d+)\s*(초|분|시간|일)\s*전\s*$")
_NAVER_ABS_RE = re.compile(r"^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*$")

def _parse_naver_time_text_to_published(time_text: str, anchor: dt.datetime, tz) -> Optional[dt.datetime]:
    """
    네이버 검색 결과에 보이는 시간 문자열을 published(datetime)로 변환.
    - "n시간 전" / "n일 전" / "n분 전" / "n초 전"
    - "YYYY.MM.DD."
    """
    if not time_text:
        return None

    t = time_text.strip()

    m = _NAVER_REL_RE.match(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "초":
            return anchor - dt.timedelta(seconds=n)
        if unit == "분":
            return anchor - dt.timedelta(minutes=n)
        if unit == "시간":
            return anchor - dt.timedelta(hours=n)
        if unit == "일":
            return anchor - dt.timedelta(days=n)

    m = _NAVER_ABS_RE.match(t)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            return dt.datetime(y, mo, d, 12, 0, 0, tzinfo=tz)
        except Exception:
            return None

    return None


def _extract_naver_time_text(it) -> str:
    """
    네이버 검색 결과에서 '4시간 전 / 1일 전 / 2026.01.12.' 같은 텍스트를 뽑음.
    """
    cand = it.select("span.info")
    for s in cand:
        txt = s.get_text(" ", strip=True)
        if _NAVER_REL_RE.match(txt) or _NAVER_ABS_RE.match(txt):
            return txt

    cand2 = it.select("a.info, span.info_group span")
    for s in cand2:
        txt = s.get_text(" ", strip=True)
        if _NAVER_REL_RE.match(txt) or _NAVER_ABS_RE.match(txt):
            return txt

    return ""


# =========================
# Deduplication (URL + Title)
# =========================
def _normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    path = (p.path or "").rstrip("/")
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc.lower()}{path}"


def _normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def deduplicate_articles(articles: List[Article]) -> List[Article]:
    seen_urls = set()
    seen_titles = set()
    out = []

    for a in articles:
        a.link = resolve_final_url(a.link)

        u = _normalize_url(a.link)
        t = _normalize_title(a.title)

        if u and u in seen_urls:
            continue
        if t and t in seen_titles:
            continue

        if u:
            seen_urls.add(u)
        if t:
            seen_titles.add(t)

        out.append(a)

    return out


# =========================
# Google News (✅ 기존 유지)
# =========================
def fetch_from_google_news(query, source_name, tz, cfg=None):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    _dprint(cfg, f"[GOOGLE] query='{query}' entries={len(getattr(feed, 'entries', []) or [])}")

    for e in getattr(feed, "entries", []):
        try:
            raw_title = getattr(e, "title", "") or ""
            title, press2 = parse_google_title_and_press(raw_title)

            summary = clean_summary(getattr(e, "summary", "") or "")
            link = resolve_final_url(getattr(e, "link", "") or "")

            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                _dprint(cfg, f"  [GOOGLE] drop aggregator host={host} title={title[:50]}")
                continue

            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            if pub_val:
                published = parse_rss_datetime(pub_val, tz)
            else:
                published = _safe_now(tz)

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            if _is_aggregator_source(source):
                _dprint(cfg, f"  [GOOGLE] drop aggregator source={source} title={title[:50]}")
                continue

            ex, reason = should_exclude_article(title, summary, return_reason=True)
            if ex:
                _dprint(cfg, f"  [GOOGLE] drop reason={reason} title={title[:50]}")
                continue

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,
                    summary=summary,
                )
            )
        except Exception as ex:
            _dprint(cfg, f"  [GOOGLE] exception: {ex}")
            continue

    return articles


# =========================
# Naver News (✅ 디버깅 로그 추가)
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8, cfg=None):
    base = "https://search.naver.com/search.naver"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://search.naver.com/",
    }
    articles = []

    # ✅ 기준 시간(anchor) = 뉴스레터 발행시간 (cfg가 없으면 now)
    if cfg is None:
        anchor = _safe_now(tz)
    else:
        anchor = _get_newsletter_anchor(cfg, tz)

    _dprint(cfg, f"\n[NAVER] keyword='{keyword}' pages={pages} anchor={anchor.isoformat()}")

    for i in range(pages):
        start = 1 + i * 10
        params = {"where": "news", "query": keyword, "start": start}

        try:
            r = requests.get(base, params=params, headers=headers, timeout=15)
        except Exception as ex:
            _dprint(cfg, f"[NAVER] request exception page={i+1} ex={ex}")
            break

        txt = r.text or ""
        _dprint(cfg, f"[NAVER] page={i+1} status={r.status_code} url={r.url} len={len(txt)}")

        # ✅ 차단/캡차/봇 페이지 감지(대표 키워드)
        block_signals = ["자동입력", "captcha", "접근이 제한", "비정상적인 접근", "로그인이 필요", "검색결과를 제공할 수 없습니다"]
        if r.status_code in (401, 403, 429) or any(s in txt.lower() for s in ["captcha"]) or any(s in txt for s in block_signals):
            _dprint(cfg, "[NAVER] ⚠️ possible blocked/captcha page detected")
            # 차단이면 이후 페이지가 의미 없어서 중단
            break

        soup = BeautifulSoup(txt, "html.parser")

        items = soup.select("div.news_wrap")
        _dprint(cfg, f"[NAVER] items found={len(items)} (selector: div.news_wrap)")

        # ✅ 네이버 마크업 변경으로 selector가 깨졌을 수도 있어서 보조 체크
        if not items:
            alt = soup.select("li.bx")  # 예비 후보
            _dprint(cfg, f"[NAVER] items alt(li.bx)={len(alt)}")
            if not alt:
                break
            # alt를 items처럼 돌리기 위해 대체
            items = alt

        for idx, it in enumerate(items[:50]):  # 한 페이지에서 너무 많이 찍히면 로그 폭발해서 안전하게 50개까지만
            a = it.select_one("a.news_tit")
            if not a:
                # alt 구조일 때 링크 셀렉터가 다를 수 있어 보조
                a = it.select_one("a")  # 마지막 보조
                if not a:
                    continue

            title = a.get("title", "") or a.get_text(" ", strip=True)
            link = a.get("href", "")

            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                _dprint(cfg, f"  [NAVER] drop aggregator host={host} title={title[:60]}")
                continue

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            ex, reason = should_exclude_article(title, summary, return_reason=True)
            if ex:
                _dprint(cfg, f"  [NAVER] drop reason={reason} title={title[:60]}")
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name
            if _is_aggregator_source(source):
                _dprint(cfg, f"  [NAVER] drop aggregator source={source} title={title[:60]}")
                continue

            time_text = _extract_naver_time_text(it)
            published = _parse_naver_time_text_to_published(time_text, anchor, tz)
            if published is None:
                published = _safe_now(tz)

            _dprint(cfg, f"  [NAVER] keep title={title[:50]} time_text='{time_text}' published={published.isoformat()}")

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,
                    summary=summary,
                    is_naver=True,
                )
            )

        # 디버깅 시 “첫 페이지만” 보고 싶으면 config.yaml에 debug_one_page: true 넣기
        if cfg and cfg.get("debug_one_page", False):
            _dprint(cfg, "[NAVER] debug_one_page enabled -> stop after first page")
            break

    _dprint(cfg, f"[NAVER] total kept={len(articles)} for keyword='{keyword}'")
    return articles


# =========================
# Orchestration
# =========================
def fetch_all_articles(cfg):
    tz = _get_tz(cfg)
    keywords = cfg.get("keywords", [])
    sources = cfg.get("news_sources", [])
    naver_pages = int(cfg.get("naver_pages", 8))

    all_articles = []

    for src in sources:
        for kw in keywords:
            if src["name"] == "NaverNews":
                all_articles += fetch_from_naver_news(kw, src["name"], tz, naver_pages, cfg=cfg)
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz, cfg=cfg)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    tz = _get_tz(cfg)
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return [a for a in articles if a.published.date() == y]


def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]


def filter_out_yakup_articles(articles):
    """약업(야쿠프) 기사만 확실히 제외."""
    out = []
    for a in articles:
        host = urlparse(a.link).netloc.lower() if getattr(a, "link", None) else ""
        src = (getattr(a, "source", "") or "").lower()
        title = (getattr(a, "title", "") or "").lower()

        if host in YAKUP_BLOCK_HOSTS:
            continue

        if any(t in src for t in YAKUP_BLOCK_TOKENS) or any(t in title for t in YAKUP_BLOCK_TOKENS):
            continue

        out.append(a)
    return out
