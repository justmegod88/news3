import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
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


# =========================
# Exclusion rules (최소)
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당",
    "상장", "ipo", "공모", "증권", "리포트",
    "목표주가", "시가총액", "ir", "주주",
]

DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범",
    "콘서트", "공연", "뮤직비디오",
    "차트", "유튜브", "방송", "예능", "ost", "연예"
]

FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티"
]

OPTICAL_HINTS = [
    "안경", "렌즈", "콘택트", "콘택트렌즈",
    "시력", "안과", "검안", "노안 렌즈", "다초점"
]


def _normalize(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def should_exclude_article(title: str, summary: str = "") -> bool:
    full = _normalize(title + " " + summary)

    # 투자/재무
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # 얼굴 노안 (광학 문맥 아니면 제외)
    if "노안" in full and any(k in full for k in FACE_AGING_HINTS):
        if not any(k in full for k in OPTICAL_HINTS):
            return True

    # 가수 다비치 / 멤버
    if any(n in full for n in DAVICHI_SINGER_NAMES):
        return True

    if "다비치" in full or "davichi" in full:
        if any(h in full for h in DAVICHI_SINGER_HINTS):
            if not any(o in full for o in OPTICAL_HINTS):
                return True

    return False


# =========================
# Config
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
# Helpers
# =========================
def parse_rss_datetime(v, tz):
    d = date_parser.parse(v)
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
# 느슨 중복 제거 (URL만)
# =========================
def _normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}"


def deduplicate_articles(articles: List[Article]) -> List[Article]:
    seen = set()
    out = []

    for a in articles:
        a.link = resolve_final_url(a.link)
        key = _normalize_url(a.link)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(a)

    return out


# =========================
# Google News
# =========================
def _google_entry_datetime(e, tz):
    if getattr(e, "published", None):
        return parse_rss_datetime(e.published, tz)
    # fallback: 어제 정오
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return dt.datetime.combine(y, dt.time(12, 0)).replace(tzinfo=tz)


def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in getattr(feed, "entries", []):
        title, press2 = parse_google_title_and_press(e.title)
        summary = clean_summary(getattr(e, "summary", ""))
        published = _google_entry_datetime(e, tz)

        press1 = ""
        if getattr(e, "source", None):
            press1 = getattr(e.source, "title", "") or ""

        source = press1 or press2 or source_name
        link = getattr(e, "link", "")

        if should_exclude_article(title, summary):
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

    return articles


# =========================
# Naver News
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8):
    base = "https://search.naver.com/search.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    articles = []

    for i in range(pages):
        start = 1 + i * 10
        params = {"where": "news", "query": keyword, "start": start}
        r = requests.get(base, params=params, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        items = soup.select("div.news_wrap")
        if not items:
            break

        for it in items:
            a = it.select_one("a.news_tit")
            if not a:
                continue

            title = a.get("title", "")
            link = a.get("href", "")
            summary = it.select_one("div.news_dsc")
            summary = summary.get_text(" ", strip=True) if summary else ""

            if should_exclude_article(title, summary):
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name

            published = _safe_now(tz)

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
                all_articles += fetch_from_naver_news(kw, src["name"], tz, naver_pages)
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    tz = _get_tz(cfg)
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return [a for a in articles if a.published.date() == y]


def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]
