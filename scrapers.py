# scrapers.py
# ------------------------------------------------------------
# ✅ 역할
# - RSS 기반 기사 수집(구글 뉴스 RSS/네이버 RSS/업계지 RSS)
# - 날짜/중복/제외 필터 제공
# - (핵심 개선) 특정 도메인(옵티뉴스/한국안경신문 등)에서
#   "전면광고/이미지(지면) 페이지"를 감지하여 is_image_ad=True 마킹
#   + 가능하면 본문 텍스트를 content에 채움
# ------------------------------------------------------------

import datetime as dt
import re
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import quote_plus, urlparse

import feedparser
import yaml
from dateutil import parser as date_parser

import requests
from bs4 import BeautifulSoup


try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


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

    # ✅ 본문 텍스트(가능하면 채움)
    content: str = ""

    # ✅ 전면광고/이미지형 페이지 여부(가능하면 마킹)
    is_image_ad: bool = False


# =========================
# Config
# =========================
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# =========================
# RSS helpers
# =========================
GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"


def _to_kst(dt_obj: dt.datetime, tz: str) -> dt.datetime:
    if not dt_obj:
        return dt_obj
    if ZoneInfo is None:
        return dt_obj
    kst = ZoneInfo(tz or "Asia/Seoul")
    if dt_obj.tzinfo is None:
        return dt_obj.replace(tzinfo=kst)
    return dt_obj.astimezone(kst)


def _parse_dt(value: str, tz: str) -> dt.datetime:
    try:
        d = date_parser.parse(value)
        return _to_kst(d, tz)
    except Exception:
        # fallback: now
        if ZoneInfo is None:
            return dt.datetime.now()
        return dt.datetime.now(ZoneInfo(tz or "Asia/Seoul"))


def _clean_text(s: str) -> str:
    s = (s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_source_from_entry(entry: dict) -> str:
    # Google News RSS entry: source often in entry.source.title
    try:
        src = entry.get("source", {})
        if isinstance(src, dict) and src.get("title"):
            return _clean_text(src.get("title"))
    except Exception:
        pass

    # fallback: entry.get("author") or "Unknown"
    return _clean_text(entry.get("author") or entry.get("publisher") or "")


def _extract_image_url(entry: dict) -> Optional[str]:
    # Some RSS include media_content / links
    for key in ("media_content", "media_thumbnail"):
        try:
            arr = entry.get(key)
            if isinstance(arr, list) and arr:
                url = arr[0].get("url")
                if url:
                    return url
        except Exception:
            pass
    return None


def _build_google_news_rss_url(query: str, hl: str = "ko", gl: str = "KR", ceid: str = "KR:ko") -> str:
    q = quote_plus(query)
    return f"{GOOGLE_NEWS_RSS_BASE}?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def _fetch_rss(url: str) -> feedparser.FeedParserDict:
    return feedparser.parse(url)


def _articles_from_feed(feed: feedparser.FeedParserDict, tz: str, is_naver: bool = False) -> List[Article]:
    out: List[Article] = []
    for e in feed.entries or []:
        title = _clean_text(getattr(e, "title", "") or "")
        link = _clean_text(getattr(e, "link", "") or "")
        summary = _clean_text(getattr(e, "summary", "") or getattr(e, "description", "") or "")

        published_raw = getattr(e, "published", None) or getattr(e, "updated", None) or ""
        published = _parse_dt(published_raw, tz)

        source = _clean_text(_extract_source_from_entry(e))
        image_url = _extract_image_url(e)

        if not title or not link:
            continue

        out.append(
            Article(
                title=title,
                link=link,
                published=published,
                source=source,
                summary=summary,
                image_url=image_url,
                is_naver=is_naver,
            )
        )
    return out


# =========================
# ✅ (핵심) 광고/이미지(지면)형 감지 + 본문 텍스트 채우기
# =========================
USER_AGENT = "Mozilla/5.0 (compatible; NewsBot/1.0)"
HTML_TIMEOUT = 8

AD_KEYWORDS = [
    "전면광고", "광고", "협찬", "프로모션", "이벤트", "기획광고", "AD",
    "지면광고", "전단", "홍보", "배너", "특집광고",
]

# RSS summary가 광고/지면 캡션처럼 들어오거나, 페이지가 이미지 위주로 뜨는 도메인
CHECK_DOMAINS_FOR_AD = {
    "www.opticnews.co.kr",
    "opticnews.co.kr",
    "www.optinews.co.kr",
    "optinews.co.kr",
    "www.koennews.co.kr",
    "koennews.co.kr",
}


def _safe_get(url: str) -> str:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=HTML_TIMEOUT,
            allow_redirects=True,
        )
        if r.status_code >= 400:
            return ""
        if not r.encoding:
            r.encoding = r.apparent_encoding
        return r.text or ""
    except Exception:
        return ""


def _extract_text_and_image_count(html: str) -> Tuple[str, int]:
    if not html:
        return "", 0

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        try:
            tag.decompose()
        except Exception:
            pass

    img_count = len(soup.find_all("img"))

    # 본문 후보 셀렉터들(범용)
    candidates = []
    for sel in [
        "div.articleView", "div.article-view", "div#article-view-content",
        "div.view_cont", "div.view-content", "div.article-body", "article",
        "div#content", "div.contents", "div.cont", "section",
        "div#articleBody", "div#article_body", "div#article",
    ]:
        node = soup.select_one(sel)
        if node:
            candidates.append(node)

    node = candidates[0] if candidates else soup.body
    text = node.get_text(" ", strip=True) if node else ""
    text = re.sub(r"\s+", " ", text).strip()

    return text, img_count


def _looks_like_ad_or_image_page(title: str, rss_summary: str, text: str, img_count: int) -> bool:
    blob = " ".join([title or "", rss_summary or "", text or ""])
    blob = re.sub(r"\s+", " ", blob)

    has_ad_kw = any(k in blob for k in AD_KEYWORDS)
    strong_ad = ("전면광고" in blob) or ("기획광고" in blob)

    # "기사 텍스트가 거의 없고 이미지가 있는 경우" -> 지면/광고일 확률 높음
    very_short = len(text) < 120
    image_heavy = img_count >= 1

    # 강한 신호가 있거나, 약한 신호(키워드) + (짧음/이미지)면 광고로 판단
    return strong_ad or (has_ad_kw and (very_short or image_heavy)) or (very_short and image_heavy)


def enrich_articles_with_page_check(articles: List[Article]) -> None:
    """
    ✅ 특정 도메인만 링크를 열어,
    - content(본문 텍스트) 채우기
    - 광고/지면 이미지형이면 is_image_ad=True 마킹
    """
    for a in articles:
        link = getattr(a, "link", "") or ""
        title = getattr(a, "title", "") or ""
        rss_sum = getattr(a, "summary", "") or ""

        try:
            host = urlparse(link).netloc.lower()
        except Exception:
            host = ""

        if host not in CHECK_DOMAINS_FOR_AD:
            continue

        html = _safe_get(link)
        text, img_count = _extract_text_and_image_count(html)

        # content 채우기
        if text and len(text) >= 60:
            a.content = text

        # 광고/이미지형 마킹
        a.is_image_ad = _looks_like_ad_or_image_page(title, rss_sum, text, img_count)


# =========================
# Fetch all articles
# =========================
def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
    """
    cfg 예시(유연):
    - cfg["timezone"] = "Asia/Seoul"
    - cfg["google_news_queries"] = ["콘택트렌즈", "난시 렌즈", ...]
    - cfg["rss_urls"] = ["https://...", ...]  # 업계지 RSS
    - cfg["naver_rss_urls"] = ["https://...", ...]  # 있으면 사용
    """
    tz = cfg.get("timezone", "Asia/Seoul")

    articles: List[Article] = []

    # 1) Google News RSS (검색어 기반)
    for q in cfg.get("google_news_queries", []) or []:
        if not q:
            continue
        url = _build_google_news_rss_url(str(q))
        feed = _fetch_rss(url)
        articles.extend(_articles_from_feed(feed, tz, is_naver=False))

    # 2) 업계지 RSS (직접 URL)
    for url in cfg.get("rss_urls", []) or []:
        if not url:
            continue
        feed = _fetch_rss(str(url))
        articles.extend(_articles_from_feed(feed, tz, is_naver=False))

    # 3) Naver RSS (있으면)
    for url in cfg.get("naver_rss_urls", []) or []:
        if not url:
            continue
        feed = _fetch_rss(str(url))
        articles.extend(_articles_from_feed(feed, tz, is_naver=True))

    # ✅ (핵심) 문제 도메인만 페이지 체크하여 광고/이미지형 마킹 + content 보강
    enrich_articles_with_page_check(articles)

    return articles


# =========================
# Filters
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "코스피", "코스닥", "시총", "실적", "매출", "영업이익",
    "순이익", "상장", "공모", "투자", "IR", "재무", "배당", "M&A", "인수", "합병",
    "증권", "리포트", "목표주가","선물",
]

YAKUP_DOMAINS = {"yakup.com", "www.yakup.com"}


def filter_out_finance_articles(articles: List[Article]) -> List[Article]:
    out = []
    for a in articles:
        blob = f"{a.title} {a.summary}"
        if any(k in blob for k in FINANCE_KEYWORDS):
            continue
        out.append(a)
    return out


def filter_out_yakup_articles(articles: List[Article]) -> List[Article]:
    out = []
    for a in articles:
        try:
            host = urlparse(a.link).netloc.lower()
        except Exception:
            host = ""
        if host in YAKUP_DOMAINS:
            continue
        out.append(a)
    return out


def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    tz_name = cfg.get("timezone", "Asia/Seoul")
    if ZoneInfo is None:
        # tz 없는 환경이면 naive로 처리
        now = dt.datetime.now()
    else:
        now = dt.datetime.now(ZoneInfo(tz_name))

    yesterday = (now.date() - dt.timedelta(days=1))

    out = []
    for a in articles:
        d = a.published.date()
        if d == yesterday:
            out.append(a)
    return out


def deduplicate_articles(articles: List[Article]) -> List[Article]:
    """
    1차 dedup: URL+제목 기반
    """
    seen = set()
    out = []
    for a in articles:
        url = (a.link or "").split("?")[0].rstrip("/")
        title = _clean_text(a.title).lower()
        key = (url, title)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


# =========================
# Final exclude (safe filter hook)
# =========================
EXCLUDE_KEYWORDS = [
    # 필요하면 여기에 더 추가
    "부고",
    "인사",
]


def should_exclude_article(title: str, summary: str) -> bool:
    blob = f"{title} {summary}"
    return any(k in blob for k in EXCLUDE_KEYWORDS)
