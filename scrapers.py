import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus
import re
import html
from functools import lru_cache

import feedparser
import yaml
from dateutil import parser as date_parser

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # py>=3.9
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
    is_naver: bool = False  # ✅ 네이버 기사 여부 표시


# =========================
# Exclusion rules (최소 필터)
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당",
    "eps", "per", "pbr", "roe",
    "상장", "ipo", "공모", "증권", "리포트",
    "목표주가", "시가총액", "ir", "주주",
]

# ✅ 가수 다비치만 제외(다비치안경은 살림)
DAVICHI_SINGER_HINTS = [
    # 멤버명 (가장 확실)
    "이해리", "강민경",

    # 연예/음악 신호
    "가수", "그룹", "듀오", "여성 듀오",
    "음원", "신곡", "컴백", "앨범", "미니앨범", "정규",
    "뮤직비디오", "mv", "티저", "트랙리스트",
    "콘서트", "공연", "팬미팅", "투어", "무대",
    "차트", "멜론", "지니", "벅스", "빌보드",
    "유튜브", "방송", "예능", "라디오", "ost", "드라마 ost",
    "연예", "연예뉴스", "entertain",

    # 연예 매체 힌트(자주 등장하는 표기)
    "osen", "텐아시아", "스타뉴스", "마이데일리", "스포츠",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def should_exclude_article(title: str, summary: str = "") -> bool:
    """
    ✅ 목적:
    - 기사 수는 최대한 살리고
    - '투자/재무/실적'과 '가수 다비치(강민경/이해리 포함)'만 제외
    """
    full = f"{_normalize(title)} {_normalize(summary)}".lower()

    # 1) 투자/재무/실적 제외
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # 2) 멤버 이름만 있어도 가수 다비치 기사로 판단 → 제외
    if "이해리" in full or "강민경" in full:
        return True

    # 3) '다비치' 또는 'davichi'가 포함되면서 연예/음악 신호가 있으면 제외
    if ("다비치" in full or "davichi" in full) and any(h in full for h in DAVICHI_SINGER_HINTS):
        return True

    return False


# =========================
# Config
# =========================
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_tz(cfg: Dict[str, Any]):
    tz_name = cfg.get("timezone", "Asia/Seoul")
    if ZoneInfo is None:
        from dateutil import tz
        return tz.gettz(tz_name)
    return ZoneInfo(tz_name)


def _safe_now(tz):
    try:
        return dt.datetime.now(tz)
    except Exception:
        return dt.datetime.now()


# =========================
# Helpers
# =========================
def parse_rss_datetime(value: str, tz) -> dt.datetime:
    d = date_parser.parse(value)
    if d.tzinfo is None:
        return d.replace(tzinfo=tz)
    return d.astimezone(tz)


def build_google_news_url(query: str) -> str:
    q = quote_plus(query)
    return f"{GOOGLE_NEWS_RSS_BASE}?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def clean_title(raw: str) -> str:
    t = (raw or "").strip()
    # "제목 - 언론사"이면 제목만 남김
    return t.split(" - ")[0].strip() if " - " in t else t


def clean_summary(raw: str) -> str:
    text = raw or ""
    text = re.sub(r"<.*?>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=2000)
def resolve_final_url(url: str) -> str:
    """
    ✅ Google News RSS 링크(news.google.com/...)를 실제 원문 URL로 변환
    - 중복 제거 정확도 상승
    - 캐시로 속도/트래픽 절약
    """
    if not url:
        return url
    try:
        r = requests.get(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return r.url or url
    except Exception:
        return url


# =========================
# Google News RSS
# =========================
def fetch_from_google_news(query: str, source_name: str, tz) -> List["Article"]:
    """
    ✅ 개선:
    1) '구글뉴스(전체)' 대신 실제 언론사명으로 source 저장
    2) 구글뉴스 리다이렉트 링크를 실제 원문 링크로 저장
    """
    feed = feedparser.parse(build_google_news_url(query))
    articles: List[Article] = []

    for e in getattr(feed, "entries", []):
        raw_title = getattr(e, "title", "") or ""

        # 언론사명 추출 (우선: entry.source.title, 차선: "제목 - 언론사")
        publisher = None
        try:
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                publisher = str(e.source.title).strip()
        except Exception:
            publisher = None

        if not publisher and " - " in raw_title:
            publisher = raw_title.split(" - ")[-1].strip()

        if not publisher:
            publisher = source_name  # fallback

        title = clean_title(raw_title)

        link = getattr(e, "link", "") or ""
        link = resolve_final_url(link)

        summary = clean_summary(getattr(e, "summary", "") or "")

        raw_date = getattr(e, "published", None) or getattr(e, "updated", None)
        published = parse_rss_datetime(raw_date, tz) if raw_date else _safe_now(tz)

        if should_exclude_article(title, summary):
            continue

        articles.append(
            Article(
                title=title,
                link=link,
                published=published,
                source=publisher,  # ✅ 실제 언론사
                summary=summary,
                image_url=None,
                is_naver=False,
            )
        )

    return articles


# =========================
# Naver News
# =========================
_NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def parse_naver_published_time(url: str, tz) -> Optional[dt.datetime]:
    """네이버 기사 본문에서 발행시간 파싱(가능하면)"""
    try:
        r = requests.get(url, headers=_NAVER_HEADERS, timeout=10, allow_redirects=True)
        if r.status_code >= 400:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return date_parser.parse(meta["content"]).astimezone(tz)

        t = soup.select_one("span.media_end_head_info_datestamp_time")
        if t and t.get("data-date-time"):
            return date_parser.parse(t["data-date-time"]).astimezone(tz)

        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            return date_parser.parse(time_tag["datetime"]).astimezone(tz)

    except Exception:
        return None

    return None


def _parse_naver_relative_time(item_soup, tz) -> Optional[dt.datetime]:
    """네이버 검색결과 '몇 분 전/몇 시간 전/몇 일 전' 등 fallback"""
    try:
        now = _safe_now(tz)
        info_texts = [x.get_text(" ", strip=True) for x in item_soup.select("span.info")]
        text = " ".join(info_texts)

        m = re.search(r"(\d+)\s*분\s*전", text)
        if m:
            return now - dt.timedelta(minutes=int(m.group(1)))

        m = re.search(r"(\d+)\s*시간\s*전", text)
        if m:
            return now - dt.timedelta(hours=int(m.group(1)))

        m = re.search(r"(\d+)\s*일\s*전", text)
        if m:
            return now - dt.timedelta(days=int(m.group(1)))

        m = re.search(r"(\d{4}\.\d{2}\.\d{2})\.?", text)
        if m:
            d = dt.datetime.strptime(m.group(1), "%Y.%m.%d").date()
            return dt.datetime.combine(d, dt.time(12, 0)).replace(tzinfo=tz)

        return None
    except Exception:
        return None


def fetch_from_naver_news(keyword: str, source_name: str, tz, pages: int = 8) -> List["Article"]:
    """
    ✅ 수집 최대화:
    - pages 크게
    - 시간 못 읽어도 기사 버리지 않음
    """
    base_url = "https://search.naver.com/search.naver"
    articles: List[Article] = []
    seen_links = set()

    for i in range(pages):
        start = 1 + i * 10
        params = {"where": "news", "query": keyword, "sort": 1, "start": start}

        try:
            r = requests.get(base_url, params=params, headers=_NAVER_HEADERS, timeout=10)
            if r.status_code != 200:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("div.news_wrap.api_ani_send")
        if not items:
            break

        for it in items:
            a = it.select_one("a.news_tit")
            if not a:
                continue

            title = (a.get("title") or "").strip()
            link = (a.get("href") or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary):
                continue

            published = parse_naver_published_time(link, tz)
            if not published:
                published = _parse_naver_relative_time(it, tz)
            if not published:
                published = _safe_now(tz)

            press = it.select_one("a.info.press")
            source = press.get_text(" ", strip=True) if press else source_name

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,  # ✅ 실제 언론사
                    summary=summary,
                    image_url=None,
                    is_naver=True,
                )
            )

    return articles


# =========================
# Orchestration
# =========================
def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
    tz = _get_tz(cfg)
    keywords = cfg.get("keywords", []) or []
    sources = cfg.get("news_sources", []) or []
    naver_pages = int(cfg.get("naver_pages", 8) or 8)

    # ✅ 수집단계에서는 중복을 "과하게" 지우지 않음
    # - 링크가 완전히 동일한 것만 제거 (뉴스레터에서 최종 dedup)
    seen = set()
    all_articles: List[Article] = []

    for src in sources:
        name = (src.get("name") or "").strip()
        host = (src.get("host") or "").strip()

        for kw in keywords:
            kw = (kw or "").strip()
            if not kw:
                continue

            if name == "NaverNews":
                fetched = fetch_from_naver_news(kw, name, tz, pages=naver_pages)
            else:
                base_query = f"{kw} site:{host}" if host else kw
                fetched = fetch_from_google_news(base_query, name or "GoogleNews", tz)

            for a in fetched:
                key = a.link or (a.title, a.source)
                if key in seen:
                    continue
                seen.add(key)
                all_articles.append(a)

    return all_articles


# =========================
# Date filter (어제 00:00~23:59, KST 고정)
# =========================
def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    """
    ✅ 오늘 뉴스레터 = 어제(00:00~23:59)
    - 네이버 기사: '날짜'만 비교
    - 그 외: datetime 범위로 비교
    """
    tz = _get_tz(cfg)
    now = _safe_now(tz)

    today = now.date()
    yesterday = today - dt.timedelta(days=1)

    start_dt = dt.datetime.combine(yesterday, dt.time.min).replace(tzinfo=tz)
    end_dt = dt.datetime.combine(yesterday, dt.time.max).replace(tzinfo=tz)

    out: List[Article] = []

    for a in articles:
        try:
            pub = a.published.astimezone(tz)
        except Exception:
            pub = a.published

        if getattr(a, "is_naver", False):
            if pub.date() == yesterday:
                out.append(a)
            continue

        if start_dt <= pub <= end_dt:
            out.append(a)

    return out


def filter_out_finance_articles(articles):
    """newsletter.py 호환용: 투자/재무 + 가수다비치 제외"""
    filtered = []
    for a in articles:
        if hasattr(a, "title") and hasattr(a, "summary"):
            if should_exclude_article(a.title, a.summary):
                continue
            filtered.append(a)
            continue

        if isinstance(a, dict):
            title = a.get("title", "") or ""
            summary = a.get("summary", "") or a.get("description", "") or ""
            if should_exclude_article(title, summary):
                continue
            filtered.append(a)
            continue

        filtered.append(a)

    return filtered
