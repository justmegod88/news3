import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus
import re
import html
import feedparser
import yaml
from dateutil import parser as date_parser
# zoneinfo는 Python 3.9+ 기본
try:
   from zoneinfo import ZoneInfo
except Exception:
   ZoneInfo = None  # 아래에서 fallback 처리
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
   # 2) 다비치 연예(가수)만 제외
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
   if ZoneInfo is None:
       # Python 3.8 이하일 가능성: dateutil tz로 fallback
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
   return t.split(" - ")[0] if " - " in t else t

def clean_summary(raw: str) -> str:
   text = raw or ""
   text = re.sub(r"<.*?>", " ", text)
   text = re.sub(r"https?://\S+", " ", text)
   text = html.unescape(text)
   return re.sub(r"\s+", " ", text).strip()

# =========================
# Google News
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
# Naver News
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
       # 1) meta published_time (가장 정확)
       meta = soup.find("meta", property="article:published_time")
       if meta and meta.get("content"):
           return date_parser.parse(meta["content"]).astimezone(tz)
       # 2) Naver 언론사 기사 페이지의 data-date-time
       t = soup.select_one("span.media_end_head_info_datestamp_time")
       if t and t.get("data-date-time"):
           return date_parser.parse(t["data-date-time"]).astimezone(tz)
       return None
   except Exception:
       return None

def fetch_from_naver_news(keyword: str, source_name: str, tz) -> List[Article]:
   """
   네이버 뉴스 검색 -> 기사 리스트 -> 기사 본문 들어가서 published_time 추출
   """
   base_url = "https://search.naver.com/search.naver"
   params = {"where": "news", "query": keyword, "sort": 1}
   headers = {"User-Agent": "Mozilla/5.0"}
   r = requests.get(base_url, params=params, headers=headers, timeout=10)
   if r.status_code != 200:
       return []
   soup = BeautifulSoup(r.text, "html.parser")
   items = soup.select("div.news_wrap.api_ani_send")
   articles: List[Article] = []
   for it in items:
       a = it.select_one("a.news_tit")
       if not a:
           continue
       title = a.get("title", "").strip()
       link = a.get("href", "").strip()
       summary_tag = it.select_one("div.news_dsc")
       summary = summary_tag.get_text(strip=True) if summary_tag else ""
       if should_exclude_article(title, summary):
           continue
       published = parse_naver_published_time(link, tz)
       if not published:
           # 날짜를 못 잡으면 "어제 기준" 필터를 정확히 못하므로 제외
           continue
       press = it.select_one("a.info.press")
       source = press.get_text(strip=True) if press else source_name
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
   keywords = cfg.get("keywords", [])
   sources = cfg.get("news_sources", [])
   seen = set()
   all_articles: List[Article] = []
   for src in sources:
       name = src.get("name", "")
       host = (src.get("host") or "").strip()
       for kw in keywords:
           kw = (kw or "").strip()
           if not kw:
               continue
           if name == "NaverNews":
               fetched = fetch_from_naver_news(kw, name, tz)
           else:
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
   한국시간 기준 '어제 00:00 ~ 23:59' 기사만 필터링 (네이버/구글 공통 적용)
   """
   tz = _get_tz(cfg)
   now = dt.datetime.now(tz)
   yesterday = (now.date() - dt.timedelta(days=1))
   start = dt.datetime.combine(yesterday, dt.time.min).replace(tzinfo=tz)
   end = dt.datetime.combine(yesterday, dt.time.max).replace(tzinfo=tz)
   out = []
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
   ✅ 기존 코드 호환용: 다른 파일에서 호출할 수 있어서 유지
   """
   keywords = [str(k).lower() for k in cfg.get("keywords", []) if k]
   out = []
   for a in articles:
       text = (a.title + " " + (a.summary or "")).lower()
       if any(k in text for k in keywords):
           out.append(a)
   return out
