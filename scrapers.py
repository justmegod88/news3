import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus
import re
import html

import feedparser
import yaml
from dateutil import parser as date_parser

# zoneinfo (Py 3.9+). GitHub Actions는 보통 3.11이라 OK.
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # Py 3.8 이하 fallback 용

import requests
from bs4 import BeautifulSoup


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


# =========================
# Exclusion rules
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당",
    "eps", "per", "pbr", "roe",
    "상장", "ipo", "공모", "증권", "리포트",
    "목표주가", "시가총액", "ir", "주주"
]

DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범",
    "콘서트", "공연", "뮤직비디오",
    "차트", "유튜브", "방송", "예능",
    "ost", "드라마 ost"
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def should_exclude_article(title: str, summary: str) -> bool:
    full = f"{_normalize(title)} {_normalize(summary)}".lower()

    # 1) 주식/투자/재무/실적 제외
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # 2) 다비치 연예(가수)만 제외 (다비치안경은 살림)
    if "다비치" in full and any(h in full for h in DAVICHI_SINGER_HINTS):
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
    if ZoneInfo is not None:
        return ZoneInfo(tz_name)
    # fallback (Py 3.8 이하)
    from dateutil import tz
    return tz.gettz(tz_name)


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
    return t.split(" - ")[0] if " - " in t else t


def clean_summary(raw: str) -> str:
    text = raw or ""
    text = re.sub(r"<.*?>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_get_text(el) -> str:
    try:
        return el.get_text(" ", strip=True)
    except Exception:
        return ""


def _is_naver_source(name: str) -> bool:
    n = (name or "").strip().lower().replace(" ", "")
    return n in {"navernews", "naver", "navernews검색", "navernewssearch", "navernews(검색)", "navernews(serach)"} or "naver" in n


# =========================
# Google News RSS
# =========================
def fetch_from_google_news(query: str, source_name: str, tz) -> List[Article]:
    feed = feedparser.parse(build_google_news_url(query))
    articles: List[Article] = []

    for e in feed.entries:
        title = clean_title(getattr(e, "title", ""))
        link = getattr(e, "link", "")
        summary = clean_summary(getattr(e, "summary", ""))

        raw_date = getattr(e, "published", None) or getattr(e, "updated", None)
        published = parse_rss_datetime(raw_date, tz) if raw_date else dt.datetime.now(tz)

        if should_exclude_article(title, summary):
            continue

        articles.append(
            Article(
                title=title,
                link=link,
                published=published,
                source=source_name,
                summary=summary,
                image_url=None,
            )
        )

    return articles


# =========================
# Naver News (HTML)
# =========================
def parse_naver_published_time(url: str, tz) -> Optional[dt.datetime]:
    """
    네이버 기사 본문에서 발행시각을 최대한 정확히 파싱
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code >= 400:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # 1) meta tag (가장 정확)
        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return date_parser.parse(meta["content"]).astimezone(tz)

        # 2) 네이버 언론사 기사 페이지 data-date-time
        t = soup.select_one("span.media_end_head_info_datestamp_time")
        if t and t.get("data-date-time"):
            return date_parser.parse(t["data-date-time"]).astimezone(tz)

        # 3) 구형/다른 템플릿: time 태그
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            return date_parser.parse(time_tag["datetime"]).astimezone(tz)

        return None
    except Exception:
        return None


def parse_naver_relative_time(item_soup, tz) -> Optional[dt.datetime]:
    """
    네이버 검색결과에 표시된 '2시간 전', '3일 전' 등을 fallback으로 파싱
    """
    try:
        # 보통 span.info가 여러 개(언론사/시간/네이버뉴스 등)
        infos = item_soup.select("span.info")
        text = " ".join([_safe_get_text(x) for x in infos])

        # "2시간 전"
        m = re.search(r"(\d+)\s*시간\s*전", text)
        if m:
            return dt.datetime.now(tz) - dt.timedelta(hours=int(m.group(1)))

        # "15분 전"
        m = re.search(r"(\d+)\s*분\s*전", text)
        if m:
            return dt.datetime.now(tz) - dt.timedelta(minutes=int(m.group(1)))

        # "3일 전"
        m = re.search(r"(\d+)\s*일\s*전", text)
        if m:
            return dt.datetime.now(tz) - dt.timedelta(days=int(m.group(1)))

        # "2025.12.15." 같은 날짜가 찍히는 경우
        m = re.search(r"(\d{4}\.\d{2}\.\d{2}\.?)", text)
        if m:
            return date_parser.parse(m.group(1)).replace(tzinfo=tz)

        return None
    except Exception:
        return None


def _naver_search_pages(max_pages: int) -> List[int]:
    """
    네이버 뉴스 검색 start 파라미터 생성
    - 1페이지: start=1
    - 2페이지: start=11
    - 3페이지: start=21 ...
    """
    pages = max(1, int(max_pages))
    return [1 + 10 * i for i in range(pages)]


def fetch_from_naver_news(keyword: str, source_name: str, tz, max_pages: int = 5) -> List[Article]:
    """
    네이버 뉴스 검색 -> 여러 페이지 -> 기사 본문에서 published_time 추출
    - 본문 시간 파싱 실패 시: 검색결과 상대시간('2시간 전') fallback 적용
    """
    base_url = "https://search.naver.com/search.naver"
    headers = {"User-Agent": "Mozilla/5.0"}

    articles: List[Article] = []
    seen_links = set()

    for start in _naver_search_pages(max_pages):
        params = {
            "where": "news",
            "query": keyword,
            "sort": 1,      # 최신순
            "start": start
        }

        try:
            r = requests.get(base_url, params=params, headers=headers, timeout=10)
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select("div.news_wrap.api_ani_send")

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
                summary = _safe_get_text(summary_tag)

                if should_exclude_article(title, summary):
                    continue

                # 1) 본문에서 정확한 발행시각
                published = parse_naver_published_time(link, tz)

                # 2) 실패 시: 검색결과 표시시간 fallback
                if not published:
                    published = parse_naver_relative_time(it, tz)

                # 그래도 없으면 제외 (어제 기준 필터를 정확히 적용하기 위함)
                if not published:
                    continue

                press = it.select_one("a.info.press")
                source = _safe_get_text(press) or source_name

                articles.append(
                    Article(
                        title=title,
                        link=link,
                        published=published,
                        source=source,
                        summary=summary,
                        image_url=None,
                    )
                )

        except Exception:
            continue

    return articles


# =========================
# Orchestration
# =========================
def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
    tz = _get_tz(cfg)
    keywords = cfg.get("keywords", [])
    sources = cfg.get("news_sources", [])

    # 네이버 수집량(페이지 수) 조절 옵션 (config에 없으면 5페이지=약50개)
    naver_pages = int(cfg.get("naver_pages", 5))

    seen = set()
    all_articles: List[Article] = []

    for src in sources:
        name = src.get("name", "")
        host = (src.get("host") or "").strip()

        for kw in keywords:
            kw = (kw or "").strip()
            if not kw:
                continue

            if _is_naver_source(name):
                fetched = fetch_from_naver_news(kw, name or "NaverNews", tz, max_pages=naver_pages)
            else:
                # 업계지는 site:도메인, 전체는 host=""로 전체 검색
                base_query = f"{kw} site:{host}" if host else kw
                query = f"{base_query} when:1d"
                fetched = fetch_from_google_news(query, name or "GoogleNews", tz)

            for a in fetched:
                key = (a.title, a.link)
                if key in seen:
                    continue
                seen.add(key)
                all_articles.append(a)

    return all_articles


def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    """
    한국시간 기준 '어제 00:00 ~ 23:59:59' 기사만 필터링 (네이버/구글 공통)
    """
    tz = _get_tz(cfg)

    now = dt.datetime.now(tz)
    yesterday = now.date() - dt.timedelta(days=1)

    start = dt.datetime.combine(yesterday, dt.time.min).replace(tzinfo=tz)
    end = dt.datetime.combine(yesterday, dt.time.max).replace(tzinfo=tz)

    out: List[Article] = []
    for a in articles:
        try:
            ap = a.published.astimezone(tz)
        except Exception:
            ap = a.published
        if start <= ap <= end:
            out.append(a)
    return out


def filter_by_keywords(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    """
    기존 코드 호환용: 키워드가 title/summary에 포함된 기사만 유지
    """
    keywords = [str(k).lower() for k in cfg.get("keywords", []) if k]
    out: List[Article] = []
    for a in articles:
        text = (a.title + " " + (a.summary or "")).lower()
        if any(k in text for k in keywords):
            out.append(a)
    return out


def filter_out_finance_articles(articles):
    """
    newsletter.py 호환용 함수.
    - 주식/투자/재무/실적 기사 제외
    - 다비치(가수/연예) 기사 제외
    scrapers.py의 should_exclude_article() 규칙을 그대로 사용.
    """
    filtered = []

    for a in articles:
        # Article(dataclass) 형태
        if hasattr(a, "title") and hasattr(a, "summary"):
            title = getattr(a, "title", "") or ""
            summary = getattr(a, "summary", "") or ""
            if should_exclude_article(title, summary):
                continue
            filtered.append(a)
            continue

        # dict 형태 대비
        if isinstance(a, dict):
            title = a.get("title", "") or ""
            summary = a.get("summary", "") or a.get("description", "") or ""
            if should_exclude_article(title, summary):
                continue
            filtered.append(a)
            continue

        filtered.append(a)

    return filtered
