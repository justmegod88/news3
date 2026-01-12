import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, parse_qs
import re
import html
import time

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
    "매출", "영업이익", "순이익", "배당", "부동산",
    "상장", "ipo", "공모", "증권", "리포트", "선물",
    "목표주가", "시가총액", "ir", "주주", "오렌지",
]

# ✅ 약업(야쿠프/약업신문) 도메인: 날짜 오류(과거 기사 유입) 방지용
YAKUP_BLOCK_HOSTS = [
    "yakup.com", "www.yakup.com",
    "yakup.co.kr", "www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업", "약업신문", "약학신문", "yakup"]

# ✅ 재배포/애그리게이터(원문 아닌 경우가 많아서 날짜 오염 유발) - 우선 차단
AGGREGATOR_BLOCK_HOSTS = [
    "msn.com", "www.msn.com",
    "flipboard.com", "www.flipboard.com",
    "smartnews.com", "www.smartnews.com",
    "newsbreak.com", "www.newsbreak.com",
]

# ✅ source(언론사명)로도 재배포를 차단(구글RSS에서 link가 news.google.com으로 남는 케이스 방어)
AGGREGATOR_BLOCK_SOURCES = [
    "msn",
    "flipboard",
    "smartnews",
    "newsbreak",
]

# 연예 / 예능 / 오락
ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬", "공연", "문화",
    "유튜버", "크리에이터", "특훈", "스포츠", "매달", "선수",
    "화제", "논란", "근황", "게임", "스타트업",
    "팬미팅", "콘서트",
]

# 인사 / 승진
PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입", "양성",
]

# 가수 다비치
DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인", "개그맨", "연기", "배우", "뮤지컬", "뮤지션", "1위",
    "콘서트", "공연", "뮤직비디오", "강민경", "이해리", "개그", "듀오", "카메라", "드라마", "연극", "탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예", "무대", "히든싱어", "가요", "음악", "시상식", "프로그램",
]

# 얼굴/뷰티 노안
FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티", "카메라", "나이", "젊은데",
]

# 포털 광고/ 낚시형 요약 문구
AD_SNIPPET_HINTS = [
    "모두가 속았다", "이걸 몰랐", "충격", "지금 확인", "알고 보니", "이유는?", "화제",
    "논란", "깜짝", "지금 다운로드", "지금 클릭", "지금 확인",
]

# 광학/렌즈 업계 화이트리스트
INDUSTRY_WHITELIST = [
    "안경", "안경원", "안경사", "호야", "에실로", "자이스", "노안 렌즈", "노안 교정",
    "렌즈", "콘택트", "콘택트렌즈", "오렌즈", "하피크리스틴",
    "안과", "검안", "시력", "콘택트 렌즈", "contact lens",
    "아큐브", "acuvue",
    "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬", "쿠퍼 비젼",
    "인터로조", "클라렌",
    "쿠퍼", "렌즈미", "안경진정성"
]


# =========================
# Utils
# =========================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _is_aggregator_host(host: str) -> bool:
    """
    ✅ 재배포/애그리게이터 도메인 차단용
    - 정확히 일치 + 서브도메인까지 커버(endswith)
    """
    h = (host or "").lower().strip()
    if not h:
        return False

    # netloc에 포트가 붙는 케이스 제거
    if ":" in h:
        h = h.split(":", 1)[0]

    for b in AGGREGATOR_BLOCK_HOSTS:
        b = b.lower()
        if h == b or h.endswith("." + b):
            return True
    return False


def _is_aggregator_source(source: str) -> bool:
    """
    ✅ source(언론사명) 기반 차단
    """
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


# =========================
# Networking helpers (timeout/retry)
# =========================
def _make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _get_with_retry(session, url, *, params=None, timeout=15, tries=3, sleep_sec=1.2):
    """
    ✅ 네이버/원문 페이지 ReadTimeout 때문에 워크플로가 통째로 죽는 걸 방지
    - 실패하면 None 반환(상위에서 그냥 skip)
    """
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code >= 400:
                return None
            return r
        except requests.exceptions.RequestException:
            if i == tries - 1:
                return None
            time.sleep(sleep_sec * (i + 1))
    return None


# =========================
# Date parsing helpers
# =========================
_REL_TIME_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>분|시간|일|주|개월|달|년)\s*전")


def _parse_relative_time_kor(text: str, now_dt: dt.datetime) -> Optional[dt.datetime]:
    """
    '3시간 전', '15분 전', '1일 전' 같은 상대시간을 now_dt 기준으로 환산
    """
    if not text:
        return None
    m = _REL_TIME_RE.search(text.strip())
    if not m:
        return None

    num = int(m.group("num"))
    unit = m.group("unit")

    if unit == "분":
        return now_dt - dt.timedelta(minutes=num)
    if unit == "시간":
        return now_dt - dt.timedelta(hours=num)
    if unit == "일":
        return now_dt - dt.timedelta(days=num)
    if unit == "주":
        return now_dt - dt.timedelta(weeks=num)
    if unit in ("개월", "달"):
        # 정확한 월 계산은 복잡하지만 "대략적 어제 필터" 목적상 충분
        return now_dt - dt.timedelta(days=30 * num)
    if unit == "년":
        return now_dt - dt.timedelta(days=365 * num)

    return None


def _parse_datetime_any(value: str, tz) -> Optional[dt.datetime]:
    """
    다양한 포맷을 dateutil로 파싱 + tz 적용
    """
    if not value:
        return None
    try:
        d = date_parser.parse(value)
        if d.tzinfo is None:
            return d.replace(tzinfo=tz)
        return d.astimezone(tz)
    except Exception:
        return None


def _extract_datetime_from_text_blob(text: str, tz) -> Optional[dt.datetime]:
    """
    본문 텍스트에서 흔한 한국 기사 날짜 패턴을 긁어오기
    예) '입력 2026.01.12 22:04', '2026-01-12 22:04', '2026. 01. 12. 22:04'
    """
    if not text:
        return None

    candidates = []

    # 1) YYYY.MM.DD HH:MM
    for m in re.finditer(r"(\d{4}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})\s*(\d{1,2}:\d{2})", text):
        candidates.append(f"{m.group(1)} {m.group(2)}")

    # 2) YYYY.MM.DD (시간이 없는 경우)
    for m in re.finditer(r"(\d{4}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})", text):
        candidates.append(m.group(1))

    for c in candidates:
        d = _parse_datetime_any(c, tz)
        if d:
            return d

    return None


def extract_published_from_article_page(url: str, tz, session=None) -> Optional[dt.datetime]:
    """
    ✅ 핵심: '원문 페이지'에서 published를 뽑아 RSS/검색목록 날짜 오염을 교정
    우선순위:
      - meta(article:published_time / og:pubdate / pubdate / date 등)
      - JSON-LD(datePublished / dateModified)
      - 본문 텍스트 패턴(입력/수정 표기 포함)
      - 상대시간(분/시간/일 전) → now 기준 환산
    """
    if not url:
        return None

    session = session or _make_session()
    r = _get_with_retry(session, url, timeout=15, tries=2, sleep_sec=1.0)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) meta tags
    meta_props = [
        ("property", "article:published_time"),
        ("property", "og:pubdate"),
        ("name", "pubdate"),
        ("name", "date"),
        ("name", "parsely-pub-date"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateModified"),
    ]
    for attr, key in meta_props:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            d = _parse_datetime_any(tag.get("content"), tz)
            if d:
                return d

    # 2) JSON-LD
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.get_text(" ", strip=True)
            if not raw:
                continue
            # 간단 파싱(완전 JSON이 아닌 케이스 대비)
            # datePublished / dateModified 문자열만 regex로 뽑기
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
            if m:
                d = _parse_datetime_any(m.group(1), tz)
                if d:
                    return d
            m = re.search(r'"dateModified"\s*:\s*"([^"]+)"', raw)
            if m:
                d = _parse_datetime_any(m.group(1), tz)
                if d:
                    return d
        except Exception:
            pass

    # 3) visible text patterns
    text_blob = soup.get_text(" ", strip=True)

    # 3-1) 상대시간(몇시간 전)
    now_dt = _safe_now(tz)
    rel = _parse_relative_time_kor(text_blob, now_dt)
    if rel:
        return rel

    # 3-2) 일반 날짜 패턴
    d = _extract_datetime_from_text_blob(text_blob, tz)
    if d:
        return d

    return None


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


def _parse_naver_result_time(it: BeautifulSoup, tz) -> Optional[dt.datetime]:
    """
    네이버 검색 결과에서 보이는 '3시간 전', '2026.01.12.' 같은 시간을 파싱
    - 성공하면 KST datetime 반환
    """
    now_dt = _safe_now(tz)

    # 네이버 검색 결과는 보통 a.info / span.info / span 등 여러 형태가 섞임
    info_texts = []
    for sel in ["span.info", "a.info", "div.info_group span.info", "div.info_group a.info"]:
        for t in it.select(sel):
            s = t.get_text(" ", strip=True)
            if s:
                info_texts.append(s)

    # 먼저 상대시간 우선
    for s in info_texts:
        rel = _parse_relative_time_kor(s, now_dt)
        if rel:
            return rel

    # 다음 날짜형
    for s in info_texts:
        # 예: '2026.01.12.' / '2026.01.12'
        d = _parse_datetime_any(s.replace(".", "."), tz)
        if d:
            return d

    return None


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
# Google News (✅ 본문 날짜 우선 교정 버전)
# =========================
def fetch_from_google_news(query, source_name, tz):
    feed = feedparser.parse(build_google_news_url(query))
    articles = []
    session = _make_session()

    for e in getattr(feed, "entries", []):
        try:
            raw_title = getattr(e, "title", "") or ""
            title, press2 = parse_google_title_and_press(raw_title)

            summary = clean_summary(getattr(e, "summary", "") or "")
            link = resolve_final_url(getattr(e, "link", "") or "")

            # ✅ 0) 도메인 기준 재배포 차단(링크가 msn 등으로 바로 오는 경우)
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            # ✅ 1) RSS published/updated(기본값)
            pub_val = getattr(e, "published", None) or getattr(e, "updated", None)
            if pub_val:
                published = parse_rss_datetime(pub_val, tz)
            else:
                published = _safe_now(tz)

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            # ✅ 0.5) source 기준 재배포 차단
            if _is_aggregator_source(source):
                continue

            if should_exclude_article(title, summary):
                continue

            # ✅ 2) 핵심: "본문 날짜"가 뽑히면 published를 덮어쓰기
            page_dt = extract_published_from_article_page(link, tz, session=session)
            if page_dt:
                published = page_dt

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
# Naver News (✅ 결과시간/본문날짜로 published 세팅 + timeout 안전)
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8):
    base = "https://search.naver.com/search.naver"
    session = _make_session()
    articles = []

    for i in range(pages):
        start = 1 + i * 10
        params = {"where": "news", "query": keyword, "start": start}

        r = _get_with_retry(session, base, params=params, timeout=15, tries=3, sleep_sec=1.2)
        if not r:
            # ✅ 네이버가 잠깐 느려도 워크플로 통째로 죽지 않게
            continue

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

            # ✅ 네이버 링크 도메인 차단(안전망)
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary):
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name

            # ✅ source 기준 재배포 차단(혹시 모를 변형)
            if _is_aggregator_source(source):
                continue

            # ✅ 1) 네이버 검색결과에 보이는 시간 먼저 파싱(3시간 전 / 2026.01.12.)
            published = _parse_naver_result_time(it, tz) or _safe_now(tz)

            # ✅ 2) 가능하면 본문(n.news.naver.com 등) 들어가서 published를 덮어쓰기(정확도 최고)
            page_dt = extract_published_from_article_page(link, tz, session=session)
            if page_dt:
                published = page_dt

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
