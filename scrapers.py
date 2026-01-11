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


# =========================
# Exclusion rules
# =========================
FINANCE_KEYWORDS = [
    "주가","주식","증시","투자","재무","실적","매출","영업이익",
    "순이익","배당","부동산","상장","ipo","공모","증권","리포트",
    "선물","목표주가","시가총액","ir","주주","오렌지",
]

YAKUP_BLOCK_HOSTS = [
    "yakup.com","www.yakup.com",
    "yakup.co.kr","www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업","약업신문","약학신문","yakup"]

ENTERTAINMENT_HINTS = [
    "연예","연예인","예능","방송","드라마","영화","배우","아이돌",
    "가수","뮤지컬","공연","문화","유튜버","크리에이터","스포츠",
]

PERSONNEL_HINTS = [
    "인사","임원","승진","선임","발탁","대표이사","사장","부사장",
    "전무","상무","ceo","cfo","cto","coo","취임","영입",
]

INDUSTRY_WHITELIST = [
    "안경","안경원","안경사","렌즈","콘택트","콘택트렌즈",
    "아큐브","acuvue","알콘","쿠퍼비전","바슈롬","자이스",
    "호야","노안","시력","검안","안과",
]


# =========================
# Utils
# =========================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def should_exclude_article(title: str, summary: str = "") -> bool:
    full = _normalize(title + " " + summary)

    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    if any(h in full for h in ENTERTAINMENT_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    if any(h in full for h in PERSONNEL_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    return False


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
# Deduplication
# =========================
def _normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc.lower()}{(p.path or '').rstrip('/')}"


def _normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\[[^\]]+\]|\([^)]*\)", " ", t)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def deduplicate_articles(articles: List[Article]) -> List[Article]:
    seen = set()
    out = []

    for a in articles:
        key = (_normalize_url(a.link), _normalize_title(a.title))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)

    return out


# =========================
# Google News
# =========================
def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in getattr(feed, "entries", []):
        try:
            raw_title = getattr(e, "title", "")
            title, press2 = parse_google_title_and_press(raw_title)

            summary = clean_summary(getattr(e, "summary", ""))
            link = resolve_final_url(getattr(e, "link", ""))

            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            if pub_val:
                published = parse_rss_datetime(pub_val, tz)
            else:
                published = _safe_now(tz)

            source = press2 or source_name

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
        except Exception:
            continue

    return articles


# =========================
# Naver News
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8):
    base = "https://search.naver.com/search.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    articles = []

    for i in range(pages):
        params = {"where": "news", "query": keyword, "start": 1 + i * 10}
        r = requests.get(base, params=params, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        for it in soup.select("div.news_wrap"):
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

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=_safe_now(tz),
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
    all_articles = []

    for src in cfg.get("news_sources", []):
        for kw in cfg.get("keywords", []):
            if src["name"] == "NaverNews":
                all_articles += fetch_from_naver_news(kw, src["name"], tz, cfg.get("naver_pages", 8))
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    tz = _get_tz(cfg)
    yesterday = (_safe_now(tz).date() - dt.timedelta(days=1))
    return [a for a in articles if a.published.date() == yesterday]


def filter_out_yakup_articles(articles):
    out = []
    for a in articles:
        host = urlparse(a.link).netloc.lower()
        src = (a.source or "").lower()
        if host in YAKUP_BLOCK_HOSTS:
            continue
        if any(t in src for t in YAKUP_BLOCK_TOKENS):
            continue
        out.append(a)
    return out


# =========================
# ✅ newsletter.py 호환용 (중요)
# =========================
def filter_out_finance_articles(articles):
    """
    newsletter.py import 에러 방지용.
    실제 필터는 should_exclude_article에서 이미 처리됨.
    """
    return articles
