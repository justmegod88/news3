import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, parse_qs
import re
import html
import json

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
    "매출", "영업이익", "순이익", "배당","부동산",
    "상장", "ipo", "공모", "증권", "리포트","선물",
    "목표주가", "시가총액", "ir", "주주","오렌지",
]

# ✅ 약업(야쿠프/약업신문) 도메인: (이제 날짜가 정확해지면 굳이 제외할 필요 없음)
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
    Google News RSS link에 url= 파라미터가 있으면 원문 링크로 교체.
    """
    try:
        qs = parse_qs(urlparse(link).query)
        if "url" in qs:
            return qs["url"][0]
    except Exception:
        pass
    return link


# =========================
# Date handling (핵심)
# =========================
AGGREGATOR_HOSTS = {
    "msn.com", "www.msn.com",
    "news.google.com",
}

NAVER_RELATIVE_ONLY_1DAY = True  # ✅ 사용자가 요청: 년전/개월전/일전/몇시간전 표시라면 "1일 전"만 수집


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _is_yesterday_dt(d: Optional[dt.datetime], tz) -> bool:
    if not d:
        return False
    try:
        dd = d.astimezone(tz).date() if d.tzinfo else d.date()
    except Exception:
        dd = d.date()
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return dd == y


def _attach_tz(d: dt.datetime, tz):
    if d.tzinfo is None:
        return d.replace(tzinfo=tz)
    return d.astimezone(tz)


def _parse_relative_korean(s: str, tz) -> Optional[dt.datetime]:
    """
    '3시간 전', '1일 전', '8개월 전', '2년 전' 등을 처리.
    ✅ 사용 조건: 상대시간 표시인 경우 "1일 전"만 통과시키려면
      - NAVER_RELATIVE_ONLY_1DAY=True 일 때: '1일 전'만 날짜로 변환, 나머지는 None
    """
    s = (s or "").strip()
    m = re.search(r"(\d+)\s*(분|시간|일|개월|년)\s*전", s)
    if not m:
        return None

    n = int(m.group(1))
    unit = m.group(2)

    if NAVER_RELATIVE_ONLY_1DAY:
        # ✅ "1일 전"만 허용 (년전/개월전/몇시간전/몇분전은 전부 버림)
        if not (unit == "일" and n == 1):
            return None

    now = _safe_now(tz)

    # "1일 전"만 쓰는 정책이면 아래는 사실상 day=1만 남음
    if unit == "분":
        return now - dt.timedelta(minutes=n)
    if unit == "시간":
        return now - dt.timedelta(hours=n)
    if unit == "일":
        return now - dt.timedelta(days=n)
    if unit == "개월":
        # 근사치(30일) - 정책상 보통 None 처리됨
        return now - dt.timedelta(days=30 * n)
    if unit == "년":
        # 근사치(365일) - 정책상 보통 None 처리됨
        return now - dt.timedelta(days=365 * n)
    return None


def _parse_absolute_date_any(s: str, tz) -> Optional[dt.datetime]:
    """
    '2026.01.10', '2026-01-10', '2026/01/10', '2026년 1월 10일' 등 파싱.
    """
    s = (s or "").strip()
    if not s:
        return None

    # YYYY.MM.DD / YYYY-MM-DD / YYYY/MM/DD
    m = re.search(r"(20\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return dt.datetime(y, mo, d, 0, 0, 0, tzinfo=tz)
        except Exception:
            return None

    # YYYY년 M월 D일
    m = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return dt.datetime(y, mo, d, 0, 0, 0, tzinfo=tz)
        except Exception:
            return None

    # dateutil (fuzzy) 마지막 시도
    try:
        d = date_parser.parse(s, fuzzy=True)
        return _attach_tz(d, tz)
    except Exception:
        return None


def _try_get_jsonld_dates(soup: BeautifulSoup, tz) -> Optional[dt.datetime]:
    """
    JSON-LD에서 datePublished/dateModified 뽑기
    """
    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    for sc in scripts:
        raw = sc.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # 일부 사이트는 json이 여러개 붙어있거나 깨져있음
            continue

        def iter_nodes(x):
            if isinstance(x, dict):
                yield x
                for v in x.values():
                    yield from iter_nodes(v)
            elif isinstance(x, list):
                for it in x:
                    yield from iter_nodes(it)

        for node in iter_nodes(data):
            if not isinstance(node, dict):
                continue
            for key in ("datePublished", "dateModified", "uploadDate"):
                if key in node and node[key]:
                    d = _parse_absolute_date_any(str(node[key]), tz)
                    if d:
                        return d
    return None


def _try_get_meta_dates(soup: BeautifulSoup, tz) -> Optional[dt.datetime]:
    """
    meta / time 태그에서 날짜 찾기
    """
    meta_keys = [
        ("property", "article:published_time"),
        ("property", "article:modified_time"),
        ("name", "article:published_time"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "date"),
        ("name", "parsely-pub-date"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateModified"),
    ]

    for attr, val in meta_keys:
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            d = _parse_absolute_date_any(tag.get("content"), tz)
            if d:
                return d

    # <time datetime="...">
    t = soup.find("time")
    if t:
        if t.get("datetime"):
            d = _parse_absolute_date_any(t.get("datetime"), tz)
            if d:
                return d
        txt = t.get_text(" ", strip=True)
        d = _parse_absolute_date_any(txt, tz)
        if d:
            return d

    return None


def _try_get_text_dates(soup: BeautifulSoup, tz) -> Optional[dt.datetime]:
    """
    본문 텍스트에서 날짜/상대시간 찾기
    - '입력 2026.01.10' 같은 패턴
    - '8개월 전' 같은 상대시간(정책상 "1일 전"만 허용 가능)
    """
    text = soup.get_text(" ", strip=True)
    if not text:
        return None

    # 상대시간 (정책 반영: "1일 전"만 허용)
    m = re.search(r"(\d+)\s*(분|시간|일|개월|년)\s*전", text)
    if m:
        rel = _parse_relative_korean(m.group(0), tz)
        if rel:
            return rel

    # '입력', '등록', '기사입력', '최종수정' 주변 날짜
    around_patterns = [
        r"(입력|등록|기사입력|게재|작성|업데이트|최종수정)\s*[:\-]?\s*(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})",
        r"(입력|등록|기사입력|게재|작성|업데이트|최종수정)\s*[:\-]?\s*(20\d{2}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
    ]
    for pat in around_patterns:
        m = re.search(pat, text)
        if m:
            d = _parse_absolute_date_any(m.group(2), tz)
            if d:
                return d

    # 최후: 텍스트 내 첫 날짜
    m = re.search(r"(20\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})", text)
    if m:
        d = _parse_absolute_date_any(m.group(0), tz)
        if d:
            return d

    return None


def _find_original_link_in_aggregator(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """
    MSN/재배포 페이지에서 '원문/출처' 링크 찾기 시도.
    - 성공하면 그 URL로 다시 들어가 날짜를 재추출할 수 있음
    """
    # 텍스트 기반
    candidates_text = ["원문", "원문보기", "기사 원문", "원문 보기", "출처", "원문 링크", "원문기사"]
    for a in soup.find_all("a", href=True):
        txt = (a.get_text(" ", strip=True) or "").strip()
        if not txt:
            continue
        if any(t in txt for t in candidates_text):
            href = a.get("href")
            if href and href.startswith("http"):
                return href

    # rel=canonical 우선
    canon = soup.find("link", attrs={"rel": "canonical"})
    if canon and canon.get("href") and canon.get("href").startswith("http"):
        return canon.get("href")

    # og:url
    og = soup.find("meta", attrs={"property": "og:url"})
    if og and og.get("content") and og.get("content").startswith("http"):
        return og.get("content")

    return None


def fetch_html(url: str, timeout=(4.0, 10.0)) -> Tuple[Optional[str], str]:
    """
    HTML 가져오고 최종 URL(리다이렉트 반영)도 반환
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text, r.url
    except Exception:
        return None, url


def extract_published_from_article_page(url: str, tz) -> Tuple[Optional[dt.datetime], str]:
    """
    ✅ 날짜 문제 해결 핵심:
    - 원문 페이지에서 날짜를 최대한 뽑아서 published를 '진짜 날짜'로 덮어쓴다.
    - 날짜를 못 뽑으면 None (=> 어제 필터에서 탈락시키는 방향)
    - MSN 같은 재배포는 원문 링크를 찾아 한 번 더 따라가 본다.
    """
    html_text, final_url = fetch_html(url)
    if not html_text:
        return None, final_url

    soup = BeautifulSoup(html_text, "html.parser")

    # 1) JSON-LD
    d = _try_get_jsonld_dates(soup, tz)
    if d:
        return _attach_tz(d, tz), final_url

    # 2) meta/time
    d = _try_get_meta_dates(soup, tz)
    if d:
        return _attach_tz(d, tz), final_url

    # 3) text 기반
    d = _try_get_text_dates(soup, tz)
    if d:
        return _attach_tz(d, tz), final_url

    # 4) 재배포(예: msn)일 가능성 → 원문 링크 찾아 재시도
    if _host(final_url) in AGGREGATOR_HOSTS or "msn." in _host(final_url):
        orig = _find_original_link_in_aggregator(soup, final_url)
        if orig and orig != final_url:
            html_text2, final_url2 = fetch_html(orig)
            if html_text2:
                soup2 = BeautifulSoup(html_text2, "html.parser")
                d2 = _try_get_jsonld_dates(soup2, tz) or _try_get_meta_dates(soup2, tz) or _try_get_text_dates(soup2, tz)
                if d2:
                    return _attach_tz(d2, tz), final_url2
            return None, final_url2

    return None, final_url


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
# Google News (✅ 날짜 강제 검증 버전)
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
            published = parse_rss_datetime(pub_val, tz) if pub_val else None

            source = (
                getattr(getattr(e, "source", None), "title", "")
                or press2
                or source_name
            )

            if should_exclude_article(title, summary):
                continue

            # ✅ 날짜 문제 해결 핵심:
            # - RSS 날짜가 어제가 아니면 무조건 원문 날짜 재추출
            # - 재배포/집계 도메인(msn 등)은 RSS가 어제여도 원문 날짜 재추출
            host = _host(link)
            need_page_check = (not _is_yesterday_dt(published, tz)) or (host in AGGREGATOR_HOSTS or host.endswith("msn.com"))

            if need_page_check:
                page_dt, final_url = extract_published_from_article_page(link, tz)
                # link도 최종 URL로 갱신(리다이렉트/원문 이동 반영)
                link = final_url

                # ✅ 날짜 못 뽑으면 과감히 버림 (이게 과거기사 유입 방지의 핵심)
                if page_dt:
                    published = page_dt
                else:
                    continue

            # ✅ 최종: 어제 기사만 통과
            if not _is_yesterday_dt(published, tz):
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
# Naver News (✅ '1일 전'만 통과 + 날짜 정확화)
# =========================
def _extract_naver_item_datetime(it: BeautifulSoup, tz) -> Optional[dt.datetime]:
    """
    네이버 뉴스 검색 결과에서 날짜/상대시간을 추출
    - '8개월 전' 같이 상대시간이면 정책상 '1일 전'만 허용
    - '2026.01.10' 같이 절대날짜면 그 날짜 사용
    """
    # 네이버는 보통 a.info.press 옆에 span.info / a.info 형태로 시간이 노출됨
    info_nodes = it.select("span.info")
    # press는 첫 span.info일 때가 많아서, 여러개 중에서 시간 후보를 찾는다
    candidates = []
    for n in info_nodes:
        txt = n.get_text(" ", strip=True)
        if txt:
            candidates.append(txt)

    # 후보 중에서 상대시간/날짜로 보이는 것만 우선 처리
    for c in candidates:
        if re.search(r"\d+\s*(분|시간|일|개월|년)\s*전", c):
            return _parse_relative_korean(c, tz)
        if re.search(r"20\d{2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}", c) or re.search(r"20\d{2}\s*년", c):
            return _parse_absolute_date_any(c, tz)

    # 최후: 텍스트 전체에서 한 번 더
    txt_all = it.get_text(" ", strip=True)
    rel = _parse_relative_korean(txt_all, tz)
    if rel:
        return rel
    absd = _parse_absolute_date_any(txt_all, tz)
    if absd:
        return absd

    return None


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

            published = _extract_naver_item_datetime(it, tz)

            # ✅ 네이버도 날짜를 못 찾으면 버림
            if not published:
                continue

            # ✅ 최종: 어제만 통과
            if not _is_yesterday_dt(published, tz):
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
                all_articles += fetch_from_naver_news(kw, src["name"], tz, naver_pages)
            else:
                q = f"{kw} site:{src['host']}" if src.get("host") else kw
                all_articles += fetch_from_google_news(q, src["name"], tz)

    return all_articles


def filter_yesterday_articles(articles, cfg):
    """
    ✅ 지금은 fetch 단계에서 이미 '어제만' 통과시키지만,
    기존 파이프라인과의 호환을 위해 유지.
    """
    tz = _get_tz(cfg)
    y = _safe_now(tz).date() - dt.timedelta(days=1)
    return [a for a in articles if a.published and a.published.astimezone(tz).date() == y]


def filter_out_finance_articles(articles):
    return [a for a in articles if not should_exclude_article(a.title, a.summary)]


def filter_out_yakup_articles(articles):
    """
    (선택) 이제 날짜가 정확해지면, 약업신문을 굳이 제외할 필요가 없어짐.
    그래도 호출하는 코드가 남아있을 수 있으니,
    기본은 '그대로 통과'로 두고, 필요할 때만 아래 continue 로직을 켜서 사용해.
    """
    out = []
    for a in articles:
        # ✅ 기본: 통과
        out.append(a)

        # --- 필요하면 아래 블록을 활성화 ---
        # host = urlparse(a.link).netloc.lower() if getattr(a, "link", None) else ""
        # src = (getattr(a, "source", "") or "").lower()
        # title = (getattr(a, "title", "") or "").lower()
        # if host in YAKUP_BLOCK_HOSTS:
        #     continue
        # if any(t in src for t in YAKUP_BLOCK_TOKENS) or any(t in title for t in YAKUP_BLOCK_TOKENS):
        #     continue
        # out.append(a)

    return out
