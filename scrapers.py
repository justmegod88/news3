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

from date_filter import is_exact_yesterday, extract_best_date

GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"

@dataclass
class Article:
    title: str
    link: str
    published: dt.datetime
    source: str
    summary: str
    image_url: Optional[str] = None
    is_naver: bool = False
    text: str = ""  # 제목+본문+메타 텍스트

FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당","부동산",
    "상장", "ipo", "공모", "증권", "리포트",
    "목표주가", "시가총액", "ir", "주주",
]

ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬","공연", "문화",
    "유튜버", "크리에이터","특훈","스포츠","매달","선수",
    "화제", "논란", "근황",
    "팬미팅", "콘서트",
]

PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입",
]

DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인","개그만", "연기", "배우",
    "콘서트", "공연", "뮤직비디오","강민경","이해리","듀오",
    "차트", "유튜브", "방송", "예능", "ost", "시상식",
]

FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티","나이",
]

AD_SNIPPET_HINTS = [
    "모두가 속았다", "이걸 몰랐", "충격", "지금 확인", "알고 보니", "이유는?", "화제",
    "논란", "깜짝","지금 다운로드", "지금 클릭",
]

INDUSTRY_WHITELIST = [
    "안경", "안경원","안경사", "호야", "에실로","자이스",
    "렌즈", "콘택트", "콘택트렌즈","안과", "검안", "시력",
    "아큐브", "acuvue",
    "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬",
    "인터로조", "클라렌",
]

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()

def should_exclude_article(title: str, summary: str = "") -> bool:
    full = _normalize(title + " " + summary)

    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    if "노안" in full and any(k in full for k in FACE_AGING_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    if any(n in full for n in DAVICHI_SINGER_NAMES):
        return True
    if "다비치" in full or "davichi" in full:
        if any(h in full for h in DAVICHI_SINGER_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
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

    if summary and len(summary) < 40:
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    return False

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

def _kst_yesterday_date(tz):
    return dt.datetime.now(tz).date() - dt.timedelta(days=1)

def _is_published_yesterday(published_dt: dt.datetime, tz) -> bool:
    try:
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=tz)
        return published_dt.astimezone(tz).date() == _kst_yesterday_date(tz)
    except Exception:
        return False

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

def fetch_article_text(url: str) -> str:
    try:
        res = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        title_el = soup.find("h1") or soup.find("title")
        title = title_el.get_text(" ", strip=True) if title_el else ""

        meta_text = soup.get_text(" ", strip=True)[:1500]
        body = soup.find("article") or soup.find("div", id="content") or soup.body
        body_text = body.get_text(" ", strip=True) if body else ""

        return f"{title}\n{meta_text}\n{body_text}"
    except Exception:
        return ""

def _normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    path = (p.path or "").rstrip("/")
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc.lower()}{path}"

def _normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def deduplicate_articles(articles: List[Article]) -> List[Article]:
    seen_urls = set()
    seen_titles = set()
    out = []
    for a in articles:
        a.link = resolve_final_url(a.link)
        u = _normalize_url(a.link)
        t = _normalize_title(a.title)
        if u and u in seen_urls:
            continue
        if t and t in seen_titles:
            continue
        if u:
            seen_urls.add(u)
        if t:
            seen_titles.add(t)
        out.append(a)
    return out

def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in getattr(feed, "entries", []):
        title, press2 = parse_google_title_and_press(e.title)
        summary = clean_summary(getattr(e, "summary", ""))

        if should_exclude_article(title, summary):
            continue

        published = parse_rss_datetime(
            getattr(e, "published", None) or getattr(e, "updated", None),
            tz,
        )

        source = (
            getattr(getattr(e, "source", None), "title", "")
            or press2
            or source_name
        )

        final_url = resolve_final_url(getattr(e, "link", ""))

        text = fetch_article_text(final_url)
        page_date = extract_best_date(text) if text else None

        # ✅ 1순위: 원문 날짜가 있으면 그걸로 '어제' 판정
        if page_date is not None:
            if not is_exact_yesterday(text):
                continue
        else:
            # ✅ 2순위: 원문 날짜 못 찾으면 RSS published로 '어제' 판정
            if not _is_published_yesterday(published, tz):
                continue

        articles.append(
            Article(
                title=title,
                link=final_url,
                published=published,
                source=source,
                summary=summary,
                text=text,
            )
        )

    return articles

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

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary):
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name

            final_url = resolve_final_url(link)
            text = fetch_article_text(final_url)
            page_date = extract_best_date(text) if text else None

            # 네이버는 리스트 자체가 최신 위주지만, 동일 정책 적용
            if page_date is not None:
                if not is_exact_yesterday(text):
                    continue
            else:
                # 원문 날짜 없으면 네이버는 "수집 시각"을 published로 두되,
                # 여기서도 어제만 보내고 싶으면: (현재는 RSS처럼 published가 없어서)
                # 어제 메일 기준이면: _safe_now(tz).date() == yesterday가 성립할 때만 통과
                if _safe_now(tz).date() != _kst_yesterday_date(tz):
                    continue

            articles.append(
                Article(
                    title=title,
                    link=final_url,
                    published=_safe_now(tz),
                    source=source,
                    summary=summary,
                    is_naver=True,
                    text=text,
                )
            )

    return articles

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
    # 인터페이스 유지용 (newsletter.py 호환)
    tz = _get_tz(cfg)
    y = _kst_yesterday_date(tz)

    out = []
    for a in articles:
        text = getattr(a, "text", "") or ""
        page_date = extract_best_date(text) if text else None

        if page_date is not None:
            if page_date == y:
                out.append(a)
        else:
            if _is_published_yesterday(getattr(a, "published", _safe_now(tz)), tz):
                out.append(a)

    return out

def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]
