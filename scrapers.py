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
# ❌ 주식 / 투자 / 재무 / 실적 기사 제외
FINANCE_KEYWORDS = [
   "주가", "주식", "증시", "투자", "재무", "실적",
   "매출", "영업이익", "순이익", "배당",
   "eps", "per", "pbr", "roe",
   "상장", "ipo", "공모", "증권", "리포트",
   "목표주가", "시가총액", "ir", "주주"
]
# ❌ '다비치' 연예(가수) 기사만 제외
DAVICHI_SINGER_HINTS = [
   "가수", "음원", "신곡", "컴백", "앨범",
   "콘서트", "공연", "뮤직비디오",
   "차트", "유튜브", "방송", "예능",
   "ost", "드라마 ost"
]

def _normalize_text(text: str) -> str:
   return re.sub(r"\s+", " ", (text or "")).strip()

def should_exclude_article(title: str, summary: str) -> bool:
   """
   True 반환 시 해당 기사는 수집 제외
   """
   full = f"{_normalize_text(title)} {_normalize_text(summary)}".lower()
   # 1️⃣ 주식/투자/재무 기사 제외
   if any(k.lower() in full for k in FINANCE_KEYWORDS):
       return True
   # 2️⃣ 다비치 '가수/연예' 기사만 제외
   if "다비치" in full and any(h in full for h in DAVICHI_SINGER_HINTS):
       return True
   return False

# =========================
# Config
# =========================
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
   with open(path, "r", encoding="utf-8") as f:
       return yaml.safe_load(f)

# =========================
# Helpers
# =========================
def parse_rss_datetime(value: str, tz: ZoneInfo) -> dt.datetime:
   d = date_parser.parse(value)
   if d.tzinfo is None:
       return d.replace(tzinfo=tz)
   return d.astimezone(tz)

def build_google_news_url(query: str) -> str:
   q = quote_plus(query)
   return f"{GOOGLE_NEWS_RSS_BASE}?q={q}&hl=ko&gl=KR&ceid=KR:ko"

def clean_title(raw_title: str) -> str:
   title = (raw_title or "").strip()
   if " - " in title:
       title = title.split(" - ")[0].strip()
   return title

def clean_summary(raw_summary: str) -> str:
   text = raw_summary or ""
   text = re.sub(r"<.*?>", " ", text)
   text = re.sub(r"https?://\S+", " ", text)
   text = html.unescape(text)
   text = re.sub(r"\s+", " ", text).strip()
   return text

def extract_image_url(entry) -> Optional[str]:
   try:
       thumbs = getattr(entry, "media_thumbnail", None)
       if thumbs:
           return thumbs[0].get("url")
   except Exception:
       pass
   return None

def extract_og_image(url: str) -> Optional[str]:
   try:
       headers = {"User-Agent": "Mozilla/5.0"}
       r = requests.get(url, headers=headers, timeout=8)
       if r.status_code >= 400:
           return None
       soup = BeautifulSoup(r.text, "html.parser")
       tag = soup.find("meta", property="og:image")
       if tag and tag.get("content"):
           return urljoin(r.url, tag["content"])
   except Exception:
       pass
   return None

# =========================
# Fetch
# =========================
def fetch_from_google_news(query: str, source_name: str, tz: ZoneInfo) -> List[Article]:
   feed = feedparser.parse(build_google_news_url(query))
   articles: List[Article] = []
   for entry in feed.entries:
       title = clean_title(getattr(entry, "title", ""))
       link = getattr(entry, "link", "")
       summary = clean_summary(getattr(entry, "summary", ""))
       raw_date = getattr(entry, "published", None) or getattr(entry, "updated", None)
       published = parse_rss_datetime(raw_date, tz) if raw_date else dt.datetime.now(tz)
       # ❌ 제외 조건
       if should_exclude_article(title, summary):
           continue
       image_url = extract_image_url(entry) or extract_og_image(link)
       articles.append(
           Article(
               title=title,
               link=link,
               published=published,
               source=source_name,
               summary=summary,
               image_url=image_url,
           )
       )
   return articles

def fetch_all_articles(cfg: Dict[str, Any]) -> List[Article]:
   tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
   keywords = cfg.get("keywords", [])
   sources = cfg.get("news_sources", [])
   seen = set()
   all_articles: List[Article] = []
   for src in sources:
       source_name = src.get("name", "GoogleNews")
       host = src.get("host", "")
       for kw in keywords:
           base_query = f"{kw} site:{host}" if host else kw
           query = f"{base_query} when:1d"  # 수집량 보조 힌트
           fetched = fetch_from_google_news(query, source_name, tz)
           for a in fetched:
               key = (a.title, a.link)
               if key in seen:
                   continue
               seen.add(key)
               all_articles.append(a)
   return all_articles

# =========================
# Date filter (어제 기준)
# =========================
def filter_yesterday_articles(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
   """
   한국시간 기준 '어제 00:00 ~ 23:59' 기사만 필터링
   """
   tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
   today = dt.datetime.now(tz).date()
   yesterday = today - dt.timedelta(days=1)
   start = dt.datetime.combine(yesterday, dt.time.min).replace(tzinfo=tz)
   end = dt.datetime.combine(yesterday, dt.time.max).replace(tzinfo=tz)
   return [
       a for a in articles
       if start <= a.published.astimezone(tz) <= end
   ]

def filter_by_keywords(articles: List[Article], cfg: Dict[str, Any]) -> List[Article]:
   keywords = [k.lower() for k in cfg.get("keywords", [])]
   return [
       a for a in articles
       if any(k in (a.title + " " + a.summary).lower() for k in keywords)
   ]
