import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote_plus, urlparse, urlunparse
import re
import html

import feedparser
import yaml
from dateutil import parser as date_parser

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # fallback below


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
# Exclusion rules (기본값)
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당",
    "eps", "per", "pbr", "roe",
    "상장", "ipo", "공모", "증권", "리포트",
    "목표주가", "시가총액", "ir", "주주",
]

DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범",
    "콘서트", "공연", "뮤직비디오",
    "차트", "유튜브", "방송", "예능",
    "ost", "드라마 ost",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _extra_excludes(cfg: Optional[Dict[str, Any]]) -> List[str]:
    if not cfg:
        return []
    ex = cfg.get("exclude_keywords", []) or []
    return [str(x).strip() for x in ex if str(x).strip()]


def should_exclude_article(title: str, summary: str = "", cfg: Optional[Dict[str, Any]] = None) -> bool:
    full = f"{_normalize(title)} {_normalize(summary)}".lower()

    # 1) 주식/투자/재무/실적 제외
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # 2) 다비치(가수/연예)만 제외 (다비치안경은 살림)
    if "다비치" in full and any(h in full for h in DAVICHI_SINGER_HINTS):
        return True

    # 3) config.yaml의 exclude_keywords 추가 제외
    extra = _extra_excludes(cfg)
    if extra and any(k.lower() in full for k in extra):
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
    return t.split(" - ")[0].strip() if " - " in t else t


def clean_summary(raw: str) -> str:
    text = raw or ""
    text = re.sub(r"<.*?>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_now(tz):
    try:
        return dt.datetime.now(tz)
    except Exception:
        return dt.datetime.now()


def normalize_url(url: str) -> str:
    """
    URL 정규화:
    - 쿼리/프래그먼트 제거 (utm 등 제거)
    """
    if not url:
        return url
    try:
        p = urlparse(url)
        p2 = p._replace(query="", fragment="")
        return urlunparse(p2)
    except Exception:
        return url


def iter_keywords_with_priority(cfg: Dict[str, Any]) -> List[Tuple[str, int]]:
    """
    config에 keywords_with_priority가 있으면 우선 사용.
    없으면 기존 keywords를 priority=0으로 사용.
    """
    kwp = cfg.get("keywords_with_priority")
    if isinstance(kwp, list) and kwp:
        out = []
        for item in kwp:
            if not isinstance(item, dict):
                continue
            kw = str(item.get("keyword", "")).strip()
            if not kw:
                continue
            pr = item.get("priority", 0)
            try:
                pr = int(pr)
            except Exception:
                pr = 0
            out.append((kw, pr))
        if out:
            return out

    # fallback: 기존 keywords
    kws = cfg.get("keywords", []) or []
    return [(str(k).strip(), 0) for k in kws if str(k).strip()]


# =========================
# Google News RSS
# =========================
def fetch_from_google_news(query: str, source_name: str, tz, cfg: Optional[Dict[str, Any]] = None) -> List[Article]:
    feed = feedparser.parse(build_google_news_url(query))
    articles: List[Article] = []

    for e in getattr(feed, "entries", []):
        title = clean_title(getattr(e, "title", ""))
        link = normalize_url(getattr(e, "link", "") or "")
        summary = clean_summary(getattr(e, "summary", "") or "")

        raw_date = getattr(e, "published", None) or getattr(e, "updated", None)
        published = parse_rss_datetime(raw_date, tz) if raw_date else _safe_now(tz)

        if should_exclude_article(title, summary, cfg=cfg):
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
# Naver News (HTML + pagination + time fallback)
# =========================
_NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def parse_naver_published_time(url: str, tz) -> Optional[dt.datetime]:
    """
    네이버 기사 본문에서 발행시간 최대한 정확히 파싱
    """
    try:
        r = requests.get(url, headers=_NAVER_HEADERS, timeout=10, allow_redirects=True)
        if r.status_code >= 400:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # 1) meta article:published_time
        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return date_parser.parse(meta["content"]).astimezone(tz)

        # 2) 네이버 뉴스 본문 data-date-time
        t = soup.select_one("span.media_end_head_info_datestamp_time")
        if t and t.get("data-date-time"):
            return date_parser.parse(t["data-date-time"]).astimezone(tz)

        # 3) 일부 페이지 time 태그
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            return date_parser.parse(time_tag["datetime"]).astimezone(tz)

    except Exception:
        return None

    return None


def _parse_naver_relative_time(item_soup, tz) -> Optional[dt.datetime]:
    """
    네이버 검색결과의 '2시간 전', '5시간 전', '1일 전' 등을 파싱 (fallback)
    """
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

        # 절대 날짜가 있는 경우 (예: 2025.12.15.)
        m = re.search(r"(\d{4}\.\d{2}\.\d{2})\.?", text)
        if m:
            d = dt.datetime.strptime(m.group(1), "%Y.%m.%d").date()
            return dt.datetime.combine(d, dt.time(12, 0)).replace(tzinfo=tz)

        return None
    except Exception:
        return None


def fetch_from_naver_news(
    keyword: str,
    source_name: str,
    tz,
    cfg: Optional[Dict[str, Any]] = None,
    pages: int = 8,
) -> List[Article]:
    """
    네이버 뉴스 검색:
      - 페이지네이션으로 수집량 증가
      - 본문 발행시간 파싱 실패 시 상대시간 fallback
      - 그래도 실패하면 '버리지 않고' now로 저장(수집량 확보)
    """
    base_url = "https://search.naver.com/search.naver"
    articles: List[Article] = []
    seen_links = set()

    for i in range(pages):
        start = 1 + i * 10
        params = {
            "where": "news",
            "query": keyword,
            "sort": 1,     # 최신순
            "start": start,
        }

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
            link = normalize_url((a.get("href") or "").strip())
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary, cfg=cfg):
                continue

            published = parse_naver_published_time(link, tz)
            if not published:
                published = _parse_naver_relative_time(it, tz)

            # ✅ 여기서 더 이상 버리지 않음 (기사 수 확보)
            if not published:
                published = _safe_now(tz)

            press = it.select_one("a.info.press")
            source = press.get_text(" ", strip=True) if press else source_name

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

    return articles


# =========================
# Orchestration
# =========================
def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
    tz = _get_tz(cfg)
    sources = cfg.get("news_sources", []) or []

    naver_pages = int(cfg.get("naver_pages", 8) or 8)

    # keyword + priority 지원
    kw_list = iter_keywords_with_priority(cfg)

    seen_by_link = set()
    all_articles: List[Article] = []

    for src in sources:
        name = (src.get("name") or "").strip()
        host = (src.get("host") or "").strip()

        for kw, _priority in kw_list:
            if name == "NaverNews":
                fetched = fetch_from_naver_news(kw, name, tz, cfg=cfg, pages=naver_pages)
            else:
                base_query = f"{kw} site:{host}" if host else kw
                # ✅ when:1d 제거 (RSS에서 0건 나오는 케이스 방지)
                fetched = fetch_from_google_news(base_query, name or "GoogleNews", tz, cfg=cfg)

            for a in fetched:
                # ✅ 링크 정규화 기준으로 중복 제거(키워드 많아도 폭증 방지)
                key = normalize_url(a.link)
                if key in seen_by_link:
                    continue
                seen_by_link.add(key)
                all_articles.append(a)

    return all_articles


# =========================
# Date filter (최근 24시간 이내)
# =========================
def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    tz = _get_tz(cfg)
    now = _safe_now(tz)
    start = now - dt.timedelta(hours=24)

    out: List[Article] = []
    for a in articles:
        try:
            ap = a.published.astimezone(tz)
        except Exception:
            ap = a.published
        if start <= ap <= now:
            out.append(a)
    return out


# =========================
# Keyword filter (호환 유지)
# =========================
def filter_by_keywords(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    # keywords_with_priority가 있으면 거기 keyword를 사용
    kwp = cfg.get("keywords_with_priority")
    if isinstance(kwp, list) and kwp:
        keywords = [str(x.get("keyword", "")).lower() for x in kwp if isinstance(x, dict) and x.get("keyword")]
    else:
        keywords = [str(k).lower() for k in (cfg.get("keywords", []) or []) if k]

    out: List[Article] = []
    for a in articles:
        text = (a.title + " " + (a.summary or "")).lower()
        if any(k in text for k in keywords):
            out.append(a)
    return out


# =========================
# Finance filter function required by newsletter.py (호환 유지)
# =========================
def filter_out_finance_articles(articles):
    filtered = []

    for a in articles:
        if hasattr(a, "title") and hasattr(a, "summary"):
            title = getattr(a, "title", "") or ""
            summary = getattr(a, "summary", "") or ""
            if should_exclude_article(title, summary):
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
