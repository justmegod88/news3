# scrapers.py
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
# Exclusion rules (원본 유지)
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

# ✅ 재배포/애그리게이터(원문 아닌 경우가 많아서 날짜 오염 유발) - (원본 유지/확장 가능)
AGGREGATOR_BLOCK_HOSTS = [
    "msn.com", "www.msn.com",
    "flipboard.com", "www.flipboard.com",
    "smartnews.com", "www.smartnews.com",
    "newsbreak.com", "www.newsbreak.com",
]

AGGREGATOR_BLOCK_SOURCES = [
    "msn",
    "flipboard",
    "smartnews",
    "newsbreak",
]

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


def _is_aggregator_host(host: str) -> bool:
    h = (host or "").lower().strip()
    if not h:
        return False
    if ":" in h:
        h = h.split(":", 1)[0]
    for b in AGGREGATOR_BLOCK_HOSTS:
        b = b.lower()
        if h == b or h.endswith("." + b):
            return True
    return False


def _is_aggregator_source(source: str) -> bool:
    s = (source or "").strip().lower()
    if not s:
        return False
    return any(b in s for b in AGGREGATOR_BLOCK_SOURCES)


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


def _newsletter_anchor_now(cfg, tz) -> dt.datetime:
    """
    ✅ 핵심: '실행 시각'이 아니라 '뉴스레터 발행 기준 시각'을 기준으로 상대시간을 환산
    - 예: 발행시간이 09:00인데, 실제 실행이 11:50이어도 기준은 09:00으로 고정
    - config.yaml에 publish_hour / publish_minute 없으면 기본 9:00
    """
    now = _safe_now(tz)
    h = int(cfg.get("publish_hour", 9))
    m = int(cfg.get("publish_minute", 0))
    anchor = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return anchor


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
    """
    ✅ 구글뉴스 링크에 url= 로 원문이 들어있는 경우를 최대한 원문으로 풀어줌
    """
    try:
        qs = parse_qs(urlparse(link).query)
        if "url" in qs and qs["url"]:
            return qs["url"][0]
    except Exception:
        pass
    return link


def _parse_naver_time_text_to_datetime(time_text: str, anchor_now: dt.datetime, tz) -> Optional[dt.datetime]:
    """
    ✅ 네이버 검색 결과에 있는 시간 텍스트를 anchor_now 기준으로 dt로 변환
    케이스 예:
      - "4시간 전"
      - "18분 전"
      - "1일 전"
      - "2026.01.12."
      - "2026.01.12. 오후 3:10" (가끔)
    """
    s = (time_text or "").strip()
    if not s:
        return None

    s = s.replace(" ", "")

    # 1) "몇분 전"
    m = re.match(r"(\d+)분전", s)
    if m:
        mins = int(m.group(1))
        return (anchor_now - dt.timedelta(minutes=mins)).astimezone(tz)

    # 2) "몇시간 전"
    m = re.match(r"(\d+)시간전", s)
    if m:
        hours = int(m.group(1))
        return (anchor_now - dt.timedelta(hours=hours)).astimezone(tz)

    # 3) "1일 전", "2일 전" (일 단위)
    m = re.match(r"(\d+)일전", s)
    if m:
        days = int(m.group(1))
        return (anchor_now - dt.timedelta(days=days)).astimezone(tz)

    # 4) 절대 날짜 "YYYY.MM.DD." 또는 "YYYY.MM.DD"
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})\.?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 시간이 없으면 12:00로 두는 게 안전(필터는 'date' 기준이라 상관없음)
        return dt.datetime(y, mo, d, 12, 0, 0, tzinfo=tz)

    # 5) 혹시 파서가 먹는 형태면 마지막으로 시도
    try:
        d = date_parser.parse(time_text)
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz)
        else:
            d = d.astimezone(tz)
        return d
    except Exception:
        return None


def _extract_naver_time_text(news_wrap) -> str:
    """
    ✅ 네이버 검색 결과에서 시간 텍스트를 최대한 안정적으로 뽑기
    - 보통 info_group 안의 span.info 중 날짜/시간이 들어있는 게 있음
    """
    # 가장 흔한 케이스
    info_group = news_wrap.select_one("div.news_info div.info_group")
    if info_group:
        infos = info_group.select("span.info")
        # '언론사'도 span.info로 들어올 수 있어서, 날짜/시간처럼 보이는 걸 우선 선택
        for sp in infos:
            t = sp.get_text(strip=True)
            if re.search(r"(분\s*전|시간\s*전|일\s*전|\d{4}\.\d{2}\.\d{2})", t):
                return t

    # 백업: 전체에서 찾기
    for sp in news_wrap.select("span.info"):
        t = sp.get_text(strip=True)
        if re.search(r"(분\s*전|시간\s*전|일\s*전|\d{4}\.\d{2}\.\d{2})", t):
            return t

    return ""


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
# Google News (RSS)
# =========================
def fetch_from_google_news(query, source_name, tz, cfg):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []

    for e in getattr(feed, "entries", []):
        try:
            raw_title = getattr(e, "title", "") or ""
            title, press2 = parse_google_title_and_press(raw_title)

            summary = clean_summary(getattr(e, "summary", "") or "")
            link = resolve_final_url(getattr(e, "link", "") or "")

            # ✅ 0) 재배포 도메인 차단
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            # ✅ published/updated 없으면 (날짜 오염 방지) 아예 스킵
            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            if not pub_val:
                continue
            published = parse_rss_datetime(pub_val, tz)

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            # ✅ source 기반 재배포 차단(보험)
            if _is_aggregator_source(source):
                continue

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
# Naver News (검색 결과)
# =========================
def fetch_from_naver_news(keyword, source_name, tz, cfg, pages=8):
    base = "https://search.naver.com/search.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    articles = []

    anchor_now = _newsletter_anchor_now(cfg, tz)

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

            # ✅ 링크 도메인 차단(보험)
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary):
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name

            # ✅ source 기준 재배포 차단(보험)
            if _is_aggregator_source(source):
                continue

            # ✅ 핵심: 네이버 상대시간/절대날짜를 anchor_now 기준으로 published로 환산
            time_text = _extract_naver_time_text(it)
            published = _parse_naver_time_text_to_datetime(time_text, anchor_now, tz)
            if published is None:
                # 날짜가 없으면 오염 가능성이 높으니 스킵(속도/정확도 우선)
                continue

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
                all_articles += fetch_from_naver_news(kw, src["name"], tz, cfg, naver_pages)
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz, cfg)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    """
    ✅ '어제(달력 기준)' 필터
    - 기준 시각은 실행 시각이 아니라 newsletter anchor (예: 09:00 고정)
    - anchor 기준으로 '어제 날짜'를 계산해서 date == yesterday 로 필터
    """
    tz = _get_tz(cfg)
    anchor_now = _newsletter_anchor_now(cfg, tz)
    yesterday = (anchor_now.date() - dt.timedelta(days=1))
    return [a for a in articles if getattr(a, "published", None) and a.published.date() == yesterday]


def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]


def filter_out_yakup_articles(articles):
    """약업(야쿠프) 기사만 확실히 제외(날짜 문제 해결되면 cfg로 조절 가능)."""
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
