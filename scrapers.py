import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, parse_qs
import re
import html
import json

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
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당","부동산",
    "상장", "ipo", "공모", "증권", "리포트","선물",
    "목표주가", "시가총액", "ir", "주주","오렌지",
]

YAKUP_BLOCK_HOSTS = [
    "yakup.com", "www.yakup.com",
    "yakup.co.kr", "www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업", "약업신문", "약학신문", "yakup"]

ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬","공연", "문화",
    "유튜버", "크리에이터","스포츠","화제","논란",
]

PERSONNEL_HINTS = [
    "인사", "승진", "선임", "대표이사", "사장",
    "ceo", "cfo", "cto", "취임",
]

AD_SNIPPET_HINTS = [
    "모두가 속았다", "충격", "지금 확인", "이유는?",
]

INDUSTRY_WHITELIST = [
    "안경", "안경원","안경사",
    "렌즈", "콘택트", "콘택트렌즈",
    "아큐브", "acuvue",
    "알콘", "쿠퍼비전", "바슈롬",
    "안과", "검안",
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

    if summary and any(h in summary for h in AD_SNIPPET_HINTS):
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
# Date parsing (핵심)
# =========================
REL_RE = re.compile(r"(\d+)\s*(년|개월|주|일|시간|분)\s*전")
ABS_PATTERNS = [
    re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"),
    re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
]

def _relative_to_dt(text: str, now: dt.datetime):
    if "어제" in text:
        y = now.date() - dt.timedelta(days=1)
        return dt.datetime(y.year, y.month, y.day, 12, 0, tzinfo=now.tzinfo)

    m = REL_RE.search(text)
    if not m:
        return None

    n = int(m.group(1))
    unit = m.group(2)

    if unit == "분":
        return now - dt.timedelta(minutes=n)
    if unit == "시간":
        return now - dt.timedelta(hours=n)
    if unit == "일":
        return now - dt.timedelta(days=n)
    if unit == "주":
        return now - dt.timedelta(weeks=n)
    if unit == "개월":
        return now - dt.timedelta(days=30 * n)
    if unit == "년":
        return now - dt.timedelta(days=365 * n)
    return None


def _absolute_from_text(text: str, tz):
    for p in ABS_PATTERNS:
        m = p.search(text)
        if m:
            y, mo, d = map(int, m.groups())
            return dt.datetime(y, mo, d, 12, 0, tzinfo=tz)
    return None


def extract_published_from_article_page(url: str, tz):
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(3, 7),
            allow_redirects=True,
        )
        if r.status_code >= 400:
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        now = _safe_now(tz)

        # meta / json-ld
        for m in soup.find_all("meta"):
            if m.get("content"):
                try:
                    d = date_parser.parse(m["content"])
                    if 2000 <= d.year <= 2100:
                        return d.astimezone(tz) if d.tzinfo else d.replace(tzinfo=tz)
                except Exception:
                    pass

        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.get_text())
                if isinstance(data, dict) and data.get("datePublished"):
                    d = date_parser.parse(data["datePublished"])
                    return d.astimezone(tz) if d.tzinfo else d.replace(tzinfo=tz)
            except Exception:
                pass

        text = soup.get_text(" ", strip=True)
        abs_dt = _absolute_from_text(text, tz)
        if abs_dt:
            return abs_dt

        rel_dt = _relative_to_dt(text, now)
        if rel_dt:
            return rel_dt

    except Exception:
        return None

    return None


# =========================
# Helpers
# =========================
def parse_rss_datetime(value, tz):
    d = date_parser.parse(value)
    return d.astimezone(tz) if d.tzinfo else d.replace(tzinfo=tz)


def build_google_news_url(query):
    return f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=ko&gl=KR&ceid=KR:ko"


def clean_summary(raw):
    text = raw or ""
    text = re.sub(r"<.*?>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_final_url(link: str) -> str:
    try:
        qs = parse_qs(urlparse(link).query)
        if "url" in qs:
            return qs["url"][0]
    except Exception:
        pass
    return link


# =========================
# Google News
# =========================
def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in feed.entries:
        try:
            title = e.title
            summary = clean_summary(getattr(e, "summary", ""))
            link = resolve_final_url(e.link)

            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            if pub_val:
                published = parse_rss_datetime(pub_val, tz)
            else:
                published = _safe_now(tz)

            # ⭐ 본문 날짜로 덮어쓰기
            page_dt = extract_published_from_article_page(link, tz)
            if page_dt:
                published = page_dt

            if should_exclude_article(title, summary):
                continue

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source_name,
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

            title = a["title"]
            link = a["href"]
            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            published = extract_published_from_article_page(link, tz) or _safe_now(tz)

            if should_exclude_article(title, summary):
                continue

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source_name,
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

    out = []

    for src in sources:
        for kw in keywords:
            if src["name"] == "NaverNews":
                out += fetch_from_naver_news(kw, src["name"], tz, naver_pages)
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                out += fetch_from_google_news(q, src["name"], tz)

    return out


def filter_yesterday_articles(articles, cfg):
    tz = _get_tz(cfg)
    yesterday = _safe_now(tz).date() - dt.timedelta(days=1)
    return [a for a in articles if a.published.date() == yesterday]


def filter_out_yakup_articles(articles):
    out = []
    for a in articles:
        host = urlparse(a.link).netloc.lower()
        if any(h in host for h in YAKUP_BLOCK_HOSTS):
            continue
        out.append(a)
    return out
