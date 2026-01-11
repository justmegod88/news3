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
    published_raw: Optional[str] = None  # ✅ 추가: '8개월 전' 같은 원문 날짜 보관


# =========================
# Relative time utils (핵심)
# =========================
RELATIVE_TIME_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>년|개월|달|일|시간)\s*전")

def is_relative_time_text(text: str) -> bool:
    if not text:
        return False
    t = str(text).strip()
    return bool(RELATIVE_TIME_RE.search(t) or ("어제" in t) or ("하루 전" in t) or ("1일 전" in t))

def allow_only_one_day_ago_relative(text: str) -> bool:
    """
    상대시간 중 '1일 전 / 어제 / 하루 전'만 허용
    """
    if not text:
        return False
    t = str(text).strip()

    if "어제" in t or "하루 전" in t:
        return True

    m = RELATIVE_TIME_RE.search(t)
    if not m:
        return False

    num = int(m.group("num"))
    unit = m.group("unit")
    return unit == "일" and num == 1


def _parse_absolute_date_text(text: str, tz) -> Optional[dt.datetime]:
    """
    네이버/기타에서 나올 수 있는 '2026.01.10.' 같은 절대날짜 파싱
    """
    if not text:
        return None
    t = str(text).strip()
    try:
        d = date_parser.parse(t)
        if d.tzinfo is None:
            return d.replace(tzinfo=tz)
        return d.astimezone(tz)
    except Exception:
        return None


# =========================
# Exclusion rules
# =========================
FINANCE_KEYWORDS = [
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당","부동산",
    "상장", "ipo", "공모", "증권", "리포트","선물",
    "목표주가", "시가총액", "ir", "주주","오렌지",
]

# ✅ 약업(야쿠프/약업신문) 도메인: 날짜 오류(과거 기사 유입) 방지용
YAKUP_BLOCK_HOSTS = [
    "yakup.com", "www.yakup.com",
    "yakup.co.kr", "www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업", "약업신문", "약학신문", "yakup"]

# 연예 / 예능 / 오락
ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬","공연", "문화",
    "유튜버", "크리에이터","특훈","스포츠","매달","선수",
    "화제", "논란", "근황","게임","스타트업",
    "팬미팅", "콘서트",
]

# 인사 / 승진
PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입","양성",
]

# 가수 다비치
DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인","개그맨", "연기", "배우","뮤지컬","뮤지션","1위",
    "콘서트", "공연", "뮤직비디오","강민경","이해리","개그","듀오","카메라","드라마","연극","탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예","무대","히든싱어","가요","음악","시상식", "프로그램",
]

# 얼굴/뷰티 노안
FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티","카메라","나이", "젊은데",
]

# 포털 광고/ 낚시형 요약 문구
AD_SNIPPET_HINTS = [
    "모두가 속았다", "이걸 몰랐", "충격", "지금 확인", "알고 보니", "이유는?", "화제",
    "논란", "깜짝","지금 다운로드", "지금 클릭", "지금 확인",
]

# 광학/렌즈 업계 화이트리스트
INDUSTRY_WHITELIST = [
    "안경", "안경원","안경사", "호야", "에실로","자이스", "노안 렌즈", "노안 교정",
    "렌즈", "콘택트", "콘택트렌즈","오렌즈", "하피크리스틴",
    "안과", "검안", "시력","콘택트 렌즈", "contact lens",
    "아큐브", "acuvue",
    "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬","쿠퍼 비젼",
    "인터로조", "클라렌",
    "쿠퍼", "렌즈미", "안경진정성"
]


# =========================
# Utils
# =========================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def should_exclude_article(title: str, summary: str = "") -> bool:
    full = _normalize(title + " " + summary)

    # 1) 투자 / 재무
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # 2) 얼굴/뷰티 노안
    if "노안" in full and any(k in full for k in FACE_AGING_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    # 3) 가수 다비치
    if any(n in full for n in DAVICHI_SINGER_NAMES):
        return True
    if "다비치" in full or "davichi" in full:
        if any(h in full for h in DAVICHI_SINGER_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
                return True

    # 4) 연예 / 예능 / 오락
    if any(h in full for h in ENTERTAINMENT_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    # 5) 타 업계 인사 / 승진
    if any(h in full for h in PERSONNEL_HINTS):
        if not any(i in full for i in INDUSTRY_WHITELIST):
            return True

    # 6) 포털 광고 / 카드형 요약 제거
    if summary:
        if any(h in summary for h in AD_SNIPPET_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
                return True

    # 7) 요약이 너무 짧은 카드형 문구 제거
    if summary and len(summary) < 40:
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
    try:
        d = date_parser.parse(value)
        if d.tzinfo is None:
            return d.replace(tzinfo=tz)
        return d.astimezone(tz)
    except Exception:
        return None


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
# Deduplication (URL + Title)
# =========================
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


# =========================
# Google News (✅ 안정화 버전)
# =========================
def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in getattr(feed, "entries", []):
        try:
            raw_title = getattr(e, "title", "") or ""
            title, press2 = parse_google_title_and_press(raw_title)

            summary = clean_summary(getattr(e, "summary", "") or "")
            link = resolve_final_url(getattr(e, "link", "") or "")

            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            published_raw = str(pub_val) if pub_val else None

            # ✅ 상대시간이면: 1일 전만 허용, 나머지는 skip
            if published_raw and is_relative_time_text(published_raw):
                if not allow_only_one_day_ago_relative(published_raw):
                    continue  # ❌ 8개월 전/몇시간 전 등은 여기서 바로 제외
                published = _safe_now(tz) - dt.timedelta(days=1)
            else:
                published = parse_rss_datetime(pub_val, tz) if pub_val else _safe_now(tz)

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            if should_exclude_article(title, summary):
                continue

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,
                    summary=summary,
                    published_raw=published_raw,
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

            # ✅ 네이버에서 날짜/상대시간 텍스트 추출 (중요)
            published_raw = None
            info_spans = it.select("span.info")
            if info_spans:
                candidates = [s.get_text(" ", strip=True) for s in info_spans if s.get_text(" ", strip=True)]
                # 우선순위: '전' 포함(상대시간) > 숫자/점 포함(절대날짜) > 마지막
                for c in candidates:
                    if "전" in c:
                        published_raw = c
                        break
                if not published_raw:
                    for c in candidates:
                        if "." in c or "-" in c:
                            published_raw = c
                            break
                if not published_raw and candidates:
                    published_raw = candidates[-1]

            # ✅ 날짜를 못 찾으면 안전하게 제외 (8개월전 섞이는 걸 막기 위한 보수 정책)
            if not published_raw:
                continue

            # ✅ 상대시간이면: 1일 전만 허용, 나머지 즉시 제외
            if is_relative_time_text(published_raw):
                if not allow_only_one_day_ago_relative(published_raw):
                    continue  # ❌ 8개월 전/몇시간 전 등은 여기서 바로 제외
                published = _safe_now(tz) - dt.timedelta(days=1)
            else:
                # 절대날짜 파싱 시도
                parsed = _parse_absolute_date_text(published_raw, tz)
                if not parsed:
                    # 절대날짜도 못 읽으면 제외(안전)
                    continue
                published = parsed

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,
                    summary=summary,
                    is_naver=True,
                    published_raw=published_raw,
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
    return [a for a in articles if a.published and a.published.date() == y]


def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]


def filter_out_yakup_articles(articles):
    """약업(야쿠프) 기사만 확실히 제외."""
    out = []
    for a in articles:
        host = urlparse(a.link).netloc.lower() if getattr(a, "link", None) else ""
        src = (getattr(a, "source", "") or "").lower()
        title = (getattr(a, "title", "") or "").lower()

        if host in YAKUP_BLOCK_HOSTS:
            continue

        if any(t in src for t in YAKUP_BLOCK_TOKENS) or any(t in title for t in YAKUP_BLOCK_TOKENS):
            continue

        out.append(a)
    return out
