import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union
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
    "목표주가", "시가총액", "ir", "주주", "오렌지",
]

YAKUP_BLOCK_HOSTS = [
    "yakup.com", "www.yakup.com",
    "yakup.co.kr", "www.yakup.co.kr",
]
YAKUP_BLOCK_TOKENS = ["약업", "약업신문", "약학신문", "yakup"]

ENTERTAINMENT_HINTS = [
    "연예", "연예인", "예능", "방송", "드라마", "영화",
    "배우", "아이돌", "가수", "뮤지컬", "공연", "문화",
    "유튜버", "크리에이터", "특훈", "스포츠", "매달", "선수",
    "화제", "논란", "근황", "게임", "스타트업",
    "팬미팅", "콘서트",
]

PERSONNEL_HINTS = [
    "인사", "임원 인사", "승진", "선임", "발탁",
    "대표이사", "사장", "부사장", "전무", "상무",
    "ceo", "cfo", "cto", "coo",
    "취임", "영입", "양성",
]

DAVICHI_SINGER_NAMES = ["강민경", "이해리"]
DAVICHI_SINGER_HINTS = [
    "가수", "음원", "신곡", "컴백", "앨범", "연예인", "개그맨", "연기", "배우", "뮤지컬", "뮤지션", "1위",
    "콘서트", "공연", "뮤직비디오", "강민경", "이해리", "개그", "듀오", "카메라", "드라마", "연극", "탤런트",
    "차트", "유튜브", "방송", "예능", "ost", "연예", "무대", "히든싱어", "가요", "음악", "시상식", "프로그램",
]

FACE_AGING_HINTS = [
    "얼굴", "피부", "주름", "리프팅", "안티에이징",
    "동안", "보톡스", "필러", "시술", "화장품", "뷰티", "카메라", "나이", "젊은데",
]

AD_SNIPPET_HINTS = [
    "모두가 속았다", "이걸 몰랐", "충격", "지금 확인", "알고 보니", "이유는?", "화제",
    "논란", "깜짝", "지금 다운로드", "지금 클릭", "지금 확인",
]

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

    if summary:
        if any(h in summary for h in AD_SNIPPET_HINTS):
            if not any(i in full for i in INDUSTRY_WHITELIST):
                return True

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


# ============================================================
# ✅ (NEW) Relative time verification for specific hosts (MSN)
#   - "1일 전" / "어제"만 통과
#   - "개월 전/년 전/시간 전/분 전/2일 전..." 모두 제외
#   - 통과 시 published를 '어제'로 덮어씀(필터 정확히 통과)
# ============================================================
RELATIVE_DATE_HOSTS = {
    "msn.com",
    "www.msn.com",
}

_REL_RE = re.compile(r"(\d+)\s*(년|개월|일|시간|분)\s*전")


def _fetch_html(url: str, timeout=(3.0, 6.0)) -> Optional[str]:
    if not url or not url.startswith("http"):
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return None
        # 간단 방어: 너무 큰 문서는 자름
        return (r.text or "")[:800_000]
    except Exception:
        return None


def _extract_relative_label(page_text: str) -> Optional[str]:
    """
    페이지 전체 텍스트에서 '8개월 전', '1일 전', '12시간 전', '어제' 같은 표기를 찾음.
    가장 먼저 발견되는 후보 하나만 반환.
    """
    if not page_text:
        return None

    t = re.sub(r"\s+", " ", page_text).strip()

    # "어제" 우선
    if "어제" in t:
        return "어제"

    # "N단위 전"
    m = _REL_RE.search(t)
    if m:
        return m.group(0)

    return None


def _is_relative_yesterday(label: str) -> bool:
    """
    유저 요구: '년전/개월전/일전/몇시간전' 표기되는 경우에는
    '1일 전(또는 어제)'만 수집. 나머지는 제외.
    """
    if not label:
        return False

    label = label.strip()

    if label == "어제":
        return True

    m = _REL_RE.match(label)
    if not m:
        return False

    n = int(m.group(1))
    unit = m.group(2)

    # ✅ 오직 1일 전만 통과
    if unit == "일" and n == 1:
        return True

    # 시간/분/개월/년/2일 전 등은 전부 제외
    return False


def _force_published_to_yesterday(now: dt.datetime) -> dt.datetime:
    """
    published를 '어제'로 덮어쓸 때 시간은 임의(정오)로 둬서 date() 비교 안정화.
    """
    y = (now.date() - dt.timedelta(days=1))
    return dt.datetime(y.year, y.month, y.day, 12, 0, 0, tzinfo=now.tzinfo)


def verify_relative_date_or_keep(published: dt.datetime, link: str, tz) -> Union[dt.datetime, None]:
    """
    반환:
      - dt.datetime: published를 덮어쓴 값(어제) 또는 그대로 유지(published)
      - None: 이 기사는 제외해야 함(상대시간이 1일 전/어제가 아님)
    """
    try:
        host = urlparse(link).netloc.lower()
    except Exception:
        host = ""

    # ✅ msn 계열만 검사 (원하면 여기에 도메인 추가)
    if host not in RELATIVE_DATE_HOSTS and not host.endswith(".msn.com"):
        return published

    now = _safe_now(tz)
    html_text = _fetch_html(link)
    if not html_text:
        # 본문 확인 실패: RSS 날짜를 일단 신뢰(보수적으로 "제외"하지 않음)
        return published

    soup = BeautifulSoup(html_text, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    label = _extract_relative_label(page_text)
    if not label:
        # 상대시간 표기 못 찾음: RSS 날짜 유지
        return published

    # 상대시간 표기가 있다면, 1일 전/어제만 통과
    if _is_relative_yesterday(label):
        return _force_published_to_yesterday(now)

    # 그 외(8개월 전/1년 전/몇시간 전/2일 전...)는 제외
    return None


# =========================
# Google News (✅ 안정화 + 상대시간 검증)
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
            if pub_val:
                published = parse_rss_datetime(pub_val, tz)
            else:
                published = _safe_now(tz)

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            if should_exclude_article(title, summary):
                continue

            # ✅ (NEW) msn 등 상대시간 도메인은 본문에서 "8개월 전" 등을 재검증
            verified = verify_relative_date_or_keep(published, link, tz)
            if verified is None:
                continue
            published = verified

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
# Naver News
# (참고: 네이버 검색결과는 published를 now로 두면 어제필터에서 다 빠짐.
#  지금은 기존 로직 유지. 원하면 여기에도 "1일 전" 파싱을 추가해줄게.)
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

            articles.append(
                Article(
                    title=title,
                    link=link,
                    published=_safe_now(tz),
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
