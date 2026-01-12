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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

# ✅ (추가) 구글뉴스 RSS에서 링크가 news.google.com으로 남는 경우가 많아서
# ✅ source(언론사명)로도 재배포를 차단
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


# =========================
# HTTP Session (Retry)
# =========================
def _build_http_session() -> requests.Session:
    """
    ✅ 네이버에서 ReadTimeout/일시적 차단이 자주 나서
    - 자동 재시도(retry)
    - 백오프(backoff)
    - 429/5xx 처리
    를 넣은 Session을 사용.
    """
    s = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=0.8,  # 0.8s, 1.6s, 3.2s... 식으로 증가
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


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
# ✅ A안: 네이버 검색결과 상대시간 파싱
# =========================
def _parse_naver_time_text_to_dt(text: str, tz) -> Optional[dt.datetime]:
    if not text:
        return None

    s = text.strip()
    now = _safe_now(tz)

    # 절대날짜: 2026.01.10. 형태
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\.", s)
    if m:
        y, mo, d = map(int, m.groups())
        return dt.datetime(y, mo, d, 0, 0, 0, tzinfo=tz)

    # 상대시간: n분 전 / n시간 전 / n일 전 / n개월 전 / n년 전
    m = re.search(r"(\d+)\s*(분|시간|일|개월|년)\s*전", s)
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

    # 개월/년은 어제 수집 목적상 None 처리(탈락)
    return None


def _extract_naver_search_item_time_text(news_wrap) -> str:
    for sp in news_wrap.select("span.info"):
        t = sp.get_text(" ", strip=True)
        if re.search(r"(\d+\s*(분|시간|일|개월|년)\s*전)|(\d{4}\.\d{1,2}\.\d{1,2}\.)", t):
            return t

    txt = news_wrap.get_text(" ", strip=True)
    m = re.search(r"(\d+\s*(분|시간|일|개월|년)\s*전)|(\d{4}\.\d{1,2}\.\d{1,2}\.)", txt)
    return m.group(0) if m else ""


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

            # ✅ 0) 도메인 기준 재배포 차단
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

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
# Naver News  (✅ A안 + Timeout/Retry 강화)
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8):
    base = "https://search.naver.com/search.naver"

    # ✅ “네이버가 봇으로 오해”하는 걸 줄이기 위한 헤더
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://search.naver.com/",
    }

    session = _build_http_session()
    articles = []

    # ✅ 네이버 단계에서 "어제"만 통과
    yesterday = (_safe_now(tz).date() - dt.timedelta(days=1))

    # 연속 실패가 쌓이면 네이버 자체를 잠시 포기(파이프라인은 계속)
    consecutive_fail = 0

    for i in range(pages):
        start = 1 + i * 10
        params = {"where": "news", "query": keyword, "start": start}

        try:
            # ✅ read timeout을 10 → 25로 늘리고, connect timeout도 분리
            r = session.get(base, params=params, headers=headers, timeout=(8, 25))
            if r.status_code >= 400:
                consecutive_fail += 1
                # 429/5xx면 잠깐 쉬었다가 다음 페이지 시도
                time.sleep(1.2)
                if consecutive_fail >= 3:
                    break
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select("div.news_wrap")
            if not items:
                break

            consecutive_fail = 0  # 성공했으면 리셋

        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            # ✅ 여기서 죽지 말고 다음 페이지/키워드로 넘어가게
            consecutive_fail += 1
            time.sleep(1.5)
            if consecutive_fail >= 3:
                break
            continue
        except Exception:
            consecutive_fail += 1
            time.sleep(1.0)
            if consecutive_fail >= 3:
                break
            continue

        for it in items:
            a = it.select_one("a.news_tit")
            if not a:
                continue

            title = a.get("title", "")
            link = a.get("href", "")

            # ✅ 도메인 기준 재배포 차단
            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary):
                continue

            press = it.select_one("a.info.press")
            source = press.get_text(strip=True) if press else source_name

            if _is_aggregator_source(source):
                continue

            # ✅ A안: '1일 전' 같은 상대시간 파싱 → 어제만 통과
            time_text = _extract_naver_search_item_time_text(it)
            published = _parse_naver_time_text_to_dt(time_text, tz)

            if (published is None) or (published.astimezone(tz).date() != yesterday):
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

        # ✅ 너무 빠르게 긁으면 차단/지연이 심해져서 아주 짧게 쉬기
        time.sleep(0.25)

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
