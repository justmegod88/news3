import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus, urljoin
import re
import html

import feedparser
import yaml
from dateutil import parser as date_parser
from zoneinfo import ZoneInfo

# ✅ 추가
import requests
from bs4 import BeautifulSoup


GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"

# (요청사항 #2) 투자/기업 재무/실적 중심 기사 제외를 위한 패턴
FINANCE_PATTERNS = [
    # KR
    "투자", "증권", "주가", "실적", "매출", "영업이익", "순이익",
    "재무", "분기", "상장", "IPO", "공모", "유상증자", "감자",
    "인수", "합병", "M&A", "자금조달", "밸류에이션", "목표주가",
    "공시", "IR", "컨퍼런스콜", "가이던스", "전망",
    # EN
    "earnings", "revenue", "operating profit", "net income", "stock", "shares",
    "ipo", "acquisition", "merger", "financing", "investment", "guidance",
]


@dataclass
class Article:
    title: str
    link: str
    published: dt.datetime
    source: str
    summary: str
    image_url: Optional[str] = None


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_rss_datetime(value: str, tz: ZoneInfo) -> dt.datetime:
    d = date_parser.parse(value)
    if d.tzinfo is None:
        return d.replace(tzinfo=tz)
    return d.astimezone(tz)


def build_google_news_url(query: str) -> str:
    q = quote_plus(query)
    # num 파라미터는 Google News RSS에서 더 많은 항목을 반환하도록 힌트를 주는 용도
    return f"{GOOGLE_NEWS_RSS_BASE}?q={q}&hl=ko&gl=KR&ceid=KR:ko&num=100"


def filter_out_finance_articles(articles: List[Article]) -> List[Article]:
    """(요청사항 #2) 투자/기업 재무/실적 중심 기사 제외"""
    out: List[Article] = []
    patterns = [p.lower() for p in FINANCE_PATTERNS]
    for a in articles:
        blob = f"{a.title} {a.summary}".lower()
        if any(p in blob for p in patterns):
            continue
        out.append(a)
    return out


def extract_image_url(entry) -> Optional[str]:
    """RSS에서 제공되는 이미지 (있을 경우)"""
    try:
        thumbs = getattr(entry, "media_thumbnail", None)
        if thumbs and isinstance(thumbs, list):
            return thumbs[0].get("url")
    except Exception:
        pass

    try:
        media = getattr(entry, "media_content", None)
        if media and isinstance(media, list):
            return media[0].get("url")
    except Exception:
        pass

    return None


# ✅ 추가: 기사 페이지에서 og:image 추출
def extract_og_image(article_url: str) -> Optional[str]:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(article_url, headers=headers, timeout=8, allow_redirects=True)
        if r.status_code >= 400:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return urljoin(r.url, tag["content"].strip())

        tag = soup.find("meta", attrs={"name": "twitter:image"})
        if tag and tag.get("content"):
            return urljoin(r.url, tag["content"].strip())

        return None
    except Exception:
        return None


def clean_title(raw_title: str) -> str:
    title = raw_title.strip()
    if " - " in title:
        title = title.split(" - ")[0].strip()
    return title


def clean_summary(raw_summary: str) -> str:
    text = raw_summary or ""
    text = re.sub(r"<.*?>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[^0-9A-Za-z가-힣 .,·…~\-_%\(\)\/\"'!?:]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return ""

    sentences = re.split(r"(?<=[\.!?…。])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return " ".join(sentences[:3])


def extract_publisher(entry, default_source_name: str) -> str:
    try:
        src = getattr(entry, "source", None)
        if src:
            title = getattr(src, "title", None)
            if title:
                return str(title).strip()
    except Exception:
        pass
    return default_source_name


def fetch_from_google_news(query: str, source_name: str, tz: ZoneInfo) -> List[Article]:
    url = build_google_news_url(query)
    feed = feedparser.parse(url)

    articles: List[Article] = []

    for entry in feed.entries:
        title = clean_title(getattr(entry, "title", ""))
        link = getattr(entry, "link", "").strip()

        raw_date = getattr(entry, "published", None) or getattr(entry, "updated", None)
        published = parse_rss_datetime(raw_date, tz) if raw_date else dt.datetime.now(tz)

        summary = clean_summary(getattr(entry, "summary", ""))

        # 🔹 1차: RSS 이미지
        image_url = extract_image_url(entry)

        # 🔹 2차: RSS에 없으면 og:image
        if not image_url and link:
            image_url = extract_og_image(link)

        publisher = extract_publisher(entry, source_name)

        articles.append(
            Article(
                title=title,
                link=link,
                published=published,
                source=publisher,
                summary=summary,
                image_url=image_url,
            )
        )

    return articles


def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    keywords = cfg.get("keywords", [])
    sources = cfg.get("news_sources", [])

    all_articles: List[Article] = []
    seen = set()

    for source in sources:
        source_name = source.get("name", "Unknown")
        host = (source.get("host") or "").strip()

        for kw in keywords:
            if not kw:
                continue
            # (요청사항 #3) 최근 24시간을 더 잘 긁어오기 위한 힌트 키워드
            base = f"{kw} site:{host}" if host else kw
            query = f"{base} when:1d"

            fetched = fetch_from_google_news(query, source_name, tz)
            for a in fetched:
                key = (a.title, a.link)
                if key in seen:
                    continue
                seen.add(key)
                all_articles.append(a)

    return all_articles


def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    # (요청사항 #3) '어제(캘린더 날짜)' 대신 '최근 24시간' 기준으로 누락을 줄임
    now = dt.datetime.now(tz)
    cutoff = now - dt.timedelta(hours=24)
    return [a for a in articles if a.published.astimezone(tz) >= cutoff]


def filter_by_keywords(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    return [
        a for a in articles
        if any(k in (a.title + " " + a.summary).lower() for k in keywords)
    ]
