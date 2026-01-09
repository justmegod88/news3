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

    # ✅ 이미지 광고(배너) 판별용
    is_image_ad: bool = False
    content_type: Optional[str] = None


# =========================
# Exclusion rules
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "코스피", "코스닥", "상장", "공모", "ipo", "m&a", "인수", "합병",
    "투자", "증권", "실적", "매출", "영업이익", "순이익", "재무", "손익", "배당", "지분",
    "기업가치", "밸류", "valuation", "자금조달", "유상증자", "무상증자", "전환사채", "cb", "bw",
    "채권", "금리", "환율", "달러", "원화", "시총", "시가총액", "펀드", "etf",
]

YAKUP_HINTS = [
    "약업신문", "yakup", "약업", "약사", "제약", "pharm", "pharmaceutical",
]

# 체인/업계 단어(예시)
OPTICAL_HINTS = [
    "안경", "안경원", "안경사", "검안", "시력", "콘택트렌즈", "콘택트 렌즈", "렌즈", "컨택트렌즈",
    "시력교정", "난시", "근시", "원시", "노안", "각막", "눈", "안과", "안질환",
    "optical", "optomet", "eye", "vision",
]

# 인사 / 승진
PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입", "양성",
]

# 가수 다비치
DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인", "개그맨", "연기", "배우", "뮤지컬", "뮤지션", "1위",
    "콘서트", "공연", "뮤직비디오", "강민경", "이해리", "개그", "듀오", "카메라", "드라마", "연극", "탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예", "무대", "히든싱어", "가요", "음악", "시상식", "프로그램",
]


# =========================
# Config
# =========================
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# =========================
# Parsing helpers
# =========================
def _parse_google_news_link(link: str) -> str:
    # google news rss link는 redirect 형태가 많아서 qs 파싱
    try:
        q = parse_qs(urlparse(link).query)
        if "url" in q and q["url"]:
            return q["url"][0]
    except Exception:
        pass
    return link


def _split_title_and_press(raw_title: str) -> Tuple[str, str]:
    # Google News RSS: "제목 - 언론사"
    if not raw_title:
        return "", ""
    if " - " in raw_title:
        t, s = raw_title.rsplit(" - ", 1)
        return t.strip(), s.strip()
    return raw_title.strip(), ""


def _to_datetime(published_str: str) -> dt.datetime:
    try:
        d = date_parser.parse(published_str)
        if d.tzinfo is None:
            return d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def clean_summary(text: str) -> str:
    if not text:
        return ""
    t = html.unescape(text)
    t = re.sub(r"<[^>]+>", " ", t)   # tag 제거
    t = re.sub(r"\s+", " ", t).strip()
    return t


# =========================
# Fetching
# =========================
def fetch_google_news_rss(query: str, hl="ko", gl="KR", ceid="KR:ko") -> List[Article]:
    url = f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"
    feed = feedparser.parse(url)

    out: List[Article] = []
    for e in feed.entries:
        raw_title = getattr(e, "title", "") or ""
        title, press = _split_title_and_press(raw_title)

        link = getattr(e, "link", "") or ""
        link = _parse_google_news_link(link)

        published = _to_datetime(getattr(e, "published", "") or "")

        summary = clean_summary(getattr(e, "summary", "") or "")
        out.append(Article(
            title=title,
            link=link,
            published=published,
            source=press or "Google News",
            summary=summary,
            image_url=None,
            is_naver=False
        ))
    return out


def fetch_naver_news_rss(query: str) -> List[Article]:
    # (기존 구현 유지 / 필요시 확장)
    # 네이버 RSS/검색 결과를 쓰고 있다면 기존 로직 그대로 둠
    return []


def fetch_all_articles(cfg: dict) -> List[Article]:
    queries = cfg.get("queries", [])
    out: List[Article] = []

    for q in queries:
        try:
            out.extend(fetch_google_news_rss(q))
        except Exception:
            continue

    # 네이버를 쓰고 있으면 여기 추가 (config에 따라)
    if cfg.get("naver", {}).get("enabled"):
        naver_queries = cfg.get("naver", {}).get("queries", [])
        for q in naver_queries:
            try:
                out.extend(fetch_naver_news_rss(q))
            except Exception:
                continue

    return out


# =========================
# Filters
# =========================
def filter_out_finance_articles(articles: List[Article]) -> List[Article]:
    out = []
    for a in articles:
        text = f"{a.title} {a.summary}".lower()
        if any(k.lower() in text for k in FINANCE_KEYWORDS):
            continue
        out.append(a)
    return out


def filter_out_yakup_articles(articles: List[Article]) -> List[Article]:
    out = []
    for a in articles:
        text = f"{a.title} {a.summary} {a.source} {a.link}".lower()
        if any(h in text for h in YAKUP_HINTS):
            continue
        out.append(a)
    return out


def filter_yesterday_articles(articles: List[Article], cfg: dict) -> List[Article]:
    tzname = cfg.get("timezone", "Asia/Seoul")
    tz = ZoneInfo(tzname) if ZoneInfo else dt.timezone.utc

    now = dt.datetime.now(tz)
    yday = now.date() - dt.timedelta(days=1)

    out = []
    for a in articles:
        # published를 tz로 맞추어 비교
        p = a.published
        if p.tzinfo is None:
            p = p.replace(tzinfo=dt.timezone.utc)
        p_local = p.astimezone(tz)

        if p_local.date() == yday:
            out.append(a)

    return out


# =========================
# Dedup (fast: URL+title)
# =========================
def _normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    path = (p.path or "").rstrip("/")
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc.lower()}{path}"


def deduplicate_articles(articles: List[Article]) -> List[Article]:
    seen = set()
    out = []
    for a in articles:
        key = (_normalize_url(a.link), (a.title or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


# =========================
# ✅ SAFE: image ad annotation (timeout + cap)
# =========================
def annotate_image_ads(
    articles: List["Article"],
    *,
    timeout_connect: float = 3.0,
    timeout_read: float = 5.0,
    max_checks: int = 60,
) -> List["Article"]:
    """
    링크가 이미지 파일로 바로 연결되는 경우(배너/광고 등)를 표시:
      - article.is_image_ad: bool
      - article.content_type: Optional[str]

    ✅ 안전장치:
      - timeout 지정 (connect/read)
      - HEAD 실패/차단 시 GET(stream=True)로 1회 fallback
      - 예외는 전부 무시하고 False 유지
      - max_checks로 네트워크 요청 상한
    """
    if not articles:
        return articles

    image_ext_re = re.compile(r"\.(png|jpg|jpeg|gif|webp|bmp|tiff)(\?.*)?$", re.IGNORECASE)

    def looks_like_image_url(url: str) -> bool:
        if not url:
            return False
        path = (urlparse(url).path or "").lower()
        return bool(image_ext_re.search(path))

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/*;q=0.8,*/*;q=0.7",
    }

    checked = 0
    cache = {}  # url -> (is_img, content_type)

    for a in articles:
        if not hasattr(a, "is_image_ad"):
            setattr(a, "is_image_ad", False)
        if not hasattr(a, "content_type"):
            setattr(a, "content_type", None)

        link = (getattr(a, "link", "") or "").strip()
        if not link:
            continue
        if not (link.startswith("http://") or link.startswith("https://")):
            continue

        # 1) 확장자 기반 빠른 판별(네트워크 없음)
        if looks_like_image_url(link):
            a.is_image_ad = True
            a.content_type = "image/*(by_ext)"
            continue

        # 2) 캐시
        if link in cache:
            is_img, ct = cache[link]
            a.is_image_ad = bool(is_img)
            a.content_type = ct
            continue

        # 3) 네트워크 검사 상한
        if checked >= max_checks:
            cache[link] = (False, None)
            continue

        checked += 1
        is_img = False
        ct = None

        # 4) HEAD 시도
        try:
            r = session.head(
                link,
                allow_redirects=True,
                timeout=(timeout_connect, timeout_read),
                headers=headers,
            )
            ct = (r.headers.get("Content-Type") or "").lower().strip() or None
            if ct and ct.startswith("image/"):
                is_img = True
        except Exception:
            pass

        # 5) GET fallback
        if not is_img:
            try:
                r = session.get(
                    link,
                    allow_redirects=True,
                    timeout=(timeout_connect, timeout_read),
                    headers=headers,
                    stream=True,
                )
                ct = (r.headers.get("Content-Type") or "").lower().strip() or ct
                if ct and ct.startswith("image/"):
                    is_img = True
            except Exception:
                pass

        a.is_image_ad = bool(is_img)
        a.content_type = ct
        cache[link] = (a.is_image_ad, a.content_type)

    return articles


# =========================
# Final safety filter
# =========================
def should_exclude_article(title: str, summary: str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()
    txt = f"{t} {s}"

    # 다비치(가수) 오탐 제외
    if "다비치" in txt:
        if any(n in txt for n in DAVICHI_SINGER_NAMES) or any(h in txt for h in DAVICHI_SINGER_HINTS):
            return True

    # 필요하면 더 추가
    return False
