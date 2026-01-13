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
    "주가", "주식", "증시", "투자", "재무", "실적",
    "매출", "영업이익", "순이익", "배당", "부동산",
    "상장", "ipo", "공모", "증권", "리포트", "선물",
    "목표주가", "시가총액", "ir", "주주", "관련주",
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
    "배우", "아이돌", "가수", "뮤지컬", "공연", "문화",
    "유튜버", "크리에이터", "특훈", "스포츠", "매달", "선수",
    "화제", "논란", "근황", "게임", "스타트업",
    "팬미팅", "콘서트", "인간극장", "극장",
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
    "가수", "음원", "신곡", "컴백", "앨범", "연예인", "개그맨", "연기", "배우",
    "뮤지컬", "뮤지션", "1위", "콘서트", "공연", "뮤직비디오",
    "강민경", "이해리", "개그", "듀오", "카메라", "드라마", "연극", "탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예", "무대", "히든싱어", "가요",
    "음악", "시상식", "프로그램",
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

# 기타 문구 (그외 삭제하고 싶은 워딩)
ETC_HINTS = [
    "테슬라","자동차".
]

# 광학/렌즈 업계 화이트리스트
INDUSTRY_WHITELIST = [
    "안경", "안경원", "안경사", "호야", "에실로", "자이스", "노안 렌즈", "노안 교정",
    "렌즈", "콘택트", "콘택트렌즈", "오렌즈", "하피크리스틴",
    "안과", "검안", "시력", "콘택트 렌즈", "contact lens",
    "아큐브", "acuvue",
    "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬", "쿠퍼 비젼",
    "인터로조",
    "쿠퍼", "렌즈미", "안경진정성",
]

# ✅ (추가) 무신사/K패션 같은 "패션 잡음" 차단용
# - 단, INDUSTRY_WHITELIST가 있으면 살림
FASHION_HINTS = [
    "무신사", "k패션", "패션", "의류", "룩북", "컬렉션", "오프화이트",
    "스타일", "코디", "브랜드", "쇼핑", "온라인몰", "패션플랫폼", "편집숍"
]


# =========================
# Utils
# =========================
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _has_industry_whitelist(full_norm: str) -> bool:
    return any(i in full_norm for i in INDUSTRY_WHITELIST)


def _host_no_port(host: str) -> str:
    h = (host or "").lower().strip()
    if ":" in h:
        h = h.split(":", 1)[0]
    return h


def _strip_www(host: str) -> str:
    h = _host_no_port(host)
    return h[4:] if h.startswith("www.") else h


def _is_aggregator_host(host: str) -> bool:
    h = _host_no_port(host)
    if not h:
        return False
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


def _build_press_host_map(cfg) -> dict:
    """
    config.yaml의 news_sources에서 host->name 매핑 생성
    예: {"chosun.com": "조선일보", "hankyung.com": "한국경제", ...}
    """
    m = {}
    for src in (cfg.get("news_sources") or []):
        name = (src.get("name") or "").strip()
        host = (src.get("host") or "").strip()
        if not name or not host:
            continue
        m[_strip_www(host)] = name
    return m


def _source_from_url(url: str, press_map: dict, fallback: str = "") -> str:
    """
    URL 도메인 기반으로 source(언론사)를 결정.
    1) cfg news_sources host와 매칭되면 name 반환
    2) 아니면 도메인 자체 반환 (예: hankyung.com)
    3) URL이 없으면 fallback
    """
    if not url:
        return fallback or ""
    host = _strip_www(urlparse(url).netloc)
    if not host:
        return fallback or ""
    return press_map.get(host, host)


# ✅ 핵심 변경: is_naver 파라미터 추가
def should_exclude_article(title: str, summary: str = "", is_naver: bool = False) -> bool:
    full = _normalize(title + " " + summary)

    # ✅ (추가) 무신사/K패션 잡음 제거
    # - 화이트리스트(콘택트렌즈 등) 있으면 살림 (기존 유지)
    if any(h in full for h in FASHION_HINTS):
        if not _has_industry_whitelist(full):
            return True

    # ✅ 1) 투자/재무: 화이트리스트 있어도 무조건 제거 (요청 반영)
    if any(k in full for k in FINANCE_KEYWORDS):
        return True

    # ✅ 2) 얼굴/뷰티 노안: 화이트리스트 있어도 무조건 제거 (요청 반영)
    if "노안" in full and any(k in full for k in FACE_AGING_HINTS):
        return True

    # 3) 가수 다비치 (기존 유지: 화이트리스트 있으면 살림)
    if any(n in full for n in DAVICHI_SINGER_NAMES):
        return True
    if "다비치" in full or "davichi" in full:
        if any(h in full for h in DAVICHI_SINGER_HINTS):
            if not _has_industry_whitelist(full):
                return True

    # ✅ 4) 연예/예능/오락: 화이트리스트 있어도 무조건 제거 (요청 반영)
    if any(h in full for h in ENTERTAINMENT_HINTS):
        return True

    # 5) 타 업계 인사 / 승진 (기존 유지: 화이트리스트 있으면 살림)
    if any(h in full for h in PERSONNEL_HINTS):
        if not _has_industry_whitelist(full):
            return True

    # ✅ 6) 포털 광고/낚시형 요약: 화이트리스트 있어도 무조건 제거 (요청 반영)
    if summary:
        if any(h in summary for h in AD_SNIPPET_HINTS):
            return True

   # 기타 문구 (그외 삭제하고 싶은 워딩)
    if summary:
        if any(h in summary for h in ETC_HINTS):
            return True


    # ✅ 7) 요약이 너무 짧은 카드형 문구 제거
    # ✅ 네이버 기사(is_naver=True)에는 적용하지 않음 (기존 유지)
    if (not is_naver) and summary and len(summary) < 40:
        if not _has_industry_whitelist(full):
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
# ✅ Newsletter publish anchor
# =========================
def _get_newsletter_anchor(cfg, tz) -> dt.datetime:
    now = _safe_now(tz)
    h = cfg.get("newsletter_publish_hour", None)
    try:
        if h is None:
            return now
        h = int(h)
        if h < 0 or h > 23:
            return now
        anchor = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if now < anchor:
            anchor = anchor - dt.timedelta(days=1)
        return anchor
    except Exception:
        return now


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
# ✅ Naver relative/absolute time parse
# =========================
_NAVER_REL_RE = re.compile(r"^\s*(\d+)\s*(초|분|시간|일)\s*전\s*$")
_NAVER_ABS_RE = re.compile(r"^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*$")


def _parse_naver_time_text_to_published(time_text: str, anchor: dt.datetime, tz) -> Optional[dt.datetime]:
    if not time_text:
        return None

    t = time_text.strip()

    m = _NAVER_REL_RE.match(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "초":
            return anchor - dt.timedelta(seconds=n)
        if unit == "분":
            return anchor - dt.timedelta(minutes=n)
        if unit == "시간":
            return anchor - dt.timedelta(hours=n)
        if unit == "일":
            return anchor - dt.timedelta(days=n)

    m = _NAVER_ABS_RE.match(t)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            return dt.datetime(y, mo, d, 12, 0, 0, tzinfo=tz)
        except Exception:
            return None

    return None


def _extract_naver_time_text(it) -> str:
    cand = it.select("span.info")
    for s in cand:
        txt = s.get_text(" ", strip=True)
        if _NAVER_REL_RE.match(txt) or _NAVER_ABS_RE.match(txt):
            return txt

    cand2 = it.select("a.info, span.info_group span")
    for s in cand2:
        txt = s.get_text(" ", strip=True)
        if _NAVER_REL_RE.match(txt) or _NAVER_ABS_RE.match(txt):
            return txt

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
# Google News
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

            if _is_aggregator_source(source):
                continue

            if should_exclude_article(title, summary, is_naver=False):
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
# Naver News (HTML)
# =========================
def fetch_from_naver_news(keyword, source_name, tz, pages=8, cfg=None):
    base = "https://search.naver.com/search.naver"
    headers = {"User-Agent": "Mozilla/5.0"}
    articles = []

    press_map = _build_press_host_map(cfg or {})
    anchor = _get_newsletter_anchor(cfg, tz) if cfg else _safe_now(tz)

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

            host = urlparse(link).netloc.lower() if link else ""
            if _is_aggregator_host(host):
                continue

            summary_tag = it.select_one("div.news_dsc")
            summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""

            if should_exclude_article(title, summary, is_naver=True):
                continue

            press = it.select_one("a.info.press")
            if press:
                source = press.get_text(strip=True)
            else:
                # press 태그가 없으면 링크 도메인으로라도 "실제 언론사/도메인" 표기
                source = _source_from_url(link, press_map, fallback=source_name)

            if _is_aggregator_source(source):
                continue

            time_text = _extract_naver_time_text(it)
            published = _parse_naver_time_text_to_published(time_text, anchor, tz)
            if published is None:
                published = _safe_now(tz)

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
# ✅ NAVER News OpenAPI
# =========================
NAVER_OPENAPI_URL = "https://openapi.naver.com/v1/search/news.json"


def _parse_naver_openapi_pubdate(pubdate: str, tz) -> Optional[dt.datetime]:
    if not pubdate:
        return None
    try:
        d = dt.datetime.strptime(pubdate, "%a, %d %b %Y %H:%M:%S %z")
        return d.astimezone(tz)
    except Exception:
        try:
            d = date_parser.parse(pubdate)
            if d.tzinfo is None:
                d = d.replace(tzinfo=tz)
            return d.astimezone(tz)
        except Exception:
            return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(s: str) -> str:
    s = s or ""
    s = html.unescape(s)
    return _TAG_RE.sub("", s).strip()


def fetch_from_naver_openapi(keyword: str, source_name: str, tz, pages: int = 10, cfg=None) -> List[Article]:
    """
    ✅ 네이버 OpenAPI 기반 뉴스 수집
    - pubDate는 절대시간이므로 그대로 사용
    - ✅ source는 "네이버뉴스"가 아니라 원문 도메인/매핑된 언론사명으로 설정 (요청 반영)
    """
    if cfg is None:
        raise ValueError("cfg is required for Naver OpenAPI (needs client id/secret)")

    client_id = (cfg.get("naver_client_id") or "").strip()
    client_secret = (cfg.get("naver_client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Missing naver_client_id / naver_client_secret in config.yaml")

    press_map = _build_press_host_map(cfg)

    display = int(cfg.get("naver_api_display", 100))
    if display <= 0 or display > 100:
        display = 100

    max_pages = int(pages) if pages else 10
    if max_pages < 1:
        max_pages = 1

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    articles: List[Article] = []

    for i in range(max_pages):
        start = 1 + i * display
        if start > 1000:
            break

        params = {
            "query": keyword,
            "display": display,
            "start": start,
            "sort": "date",  # 최신순
        }

        r = requests.get(NAVER_OPENAPI_URL, headers=headers, params=params, timeout=10)

        if cfg.get("debug"):
            print(f"[NAVER OPENAPI] kw='{keyword}' start={start} status={r.status_code}")

        if r.status_code != 200:
            if cfg.get("debug"):
                print("[NAVER OPENAPI] ERROR:", r.text[:200])
            break

        data = r.json()
        items = data.get("items", []) or []
        if not items:
            break

        for it in items:
            title = _strip_html_tags(it.get("title", ""))
            desc = _strip_html_tags(it.get("description", ""))

            origin = (it.get("originallink") or "").strip()
            link = (origin or it.get("link") or "").strip()
            if not link:
                continue

            published = _parse_naver_openapi_pubdate(it.get("pubDate", ""), tz)
            if not published:
                continue

            # ✅ 여기서 "진짜 언론사"로 source 설정
            # 1) originallink 우선
            # 2) cfg news_sources host와 매핑되면 언론사 name
            # 3) 아니면 도메인 자체
            source = _source_from_url(origin or link, press_map, fallback=source_name)

            if _is_aggregator_host(urlparse(link).netloc):
                continue
            if _is_aggregator_source(source):
                continue

            if should_exclude_article(title, desc, is_naver=True):
                continue

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=published,
                    source=source,
                    summary=desc,
                    image_url=None,
                    is_naver=True,
                )
            )

        if cfg.get("debug_one_page"):
            break

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
                # ✅ OpenAPI 우선
                try:
                    if (cfg.get("naver_client_id") and cfg.get("naver_client_secret")):
                        all_articles += fetch_from_naver_openapi(
                            kw, src["name"], tz, naver_pages, cfg=cfg
                        )
                    else:
                        all_articles += fetch_from_naver_news(
                            kw, src["name"], tz, naver_pages, cfg=cfg
                        )
                except Exception as e:
                    if cfg.get("debug"):
                        print("[NAVER OPENAPI] fallback due to:", repr(e))
                    all_articles += fetch_from_naver_news(
                        kw, src["name"], tz, naver_pages, cfg=cfg
                    )
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    tz = _get_tz(cfg)
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return [a for a in articles if a.published.date() == y]


def filter_out_finance_articles(articles):
    # ✅ is_naver 유지해서 재필터링 (기존 코멘트 반영)
    return [
        a for a in articles
        if not should_exclude_article(a.title, a.summary, is_naver=getattr(a, "is_naver", False))
    ]


def filter_out_yakup_articles(articles):
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
