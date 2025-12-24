import datetime as dt
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import re
import difflib

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_out_finance_articles,
    deduplicate_articles,        # (scrapers.py의 URL+제목 dedup: 1차)
    should_exclude_article,      # ✅ 최종 안전 필터용
)
from categorizer import categorize_articles
from summarizer import refine_article_summaries
from mailer import send_email_html


# =========================
# ✅ (A) URL/제목 정규화
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
    t = re.sub(r"\[[^\]]+\]", " ", t)      # [단독]
    t = re.sub(r"\([^)]*\)", " ", t)       # (종합)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _normalize_text(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _similarity(a: str, b: str) -> float:
    a = _normalize_text(a)
    b = _normalize_text(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# =========================
# ✅ (B) 대표 기사(언론사) 선택 규칙
#     - 1순위: 업계지
#     - 2순위: 통신사/대형
#     - 3순위: 종합/기타
# =========================
INDUSTRY_SOURCES = {
    # 업계/전문지(예시) — 여기 네 뉴스레터 기준으로 계속 추가하면 됨
    "안경신문",
    "옵티컬저널",
    "옵티칼저널",
    "안경계",
    "아이케어뉴스",
    "메디칼업저버",
    "의학신문",
    "헬스조선",  # 필요 시
    "바이오타임즈",  # 필요 시
}

TIER2_SOURCES = {
    # 통신사/대형
    "연합뉴스",
    "뉴시스",
    "YTN",
    "SBS",
    "KBS",
    "MBC",
    "JTBC",
    "조선일보",
    "중앙일보",
    "동아일보",
    "한겨레",
    "경향신문",
}

def _source_priority(source: str) -> int:
    s = (source or "").strip()

    if s in INDUSTRY_SOURCES:
        return 1
    if s in TIER2_SOURCES:
        return 2
    if s:
        return 3
    return 99  # source 비어있으면 가장 뒤


def _pick_representative(group):
    """
    그룹(중복 묶음)에서 대표 기사 1개 선택:
      1) 업계지 우선
      2) 그 다음 통신사/대형
      3) 그 외
    동순위면 '제목 길이(정보량)'가 더 큰 걸 우선.
    """
    def score(a):
        src = getattr(a, "source", "") or ""
        title = getattr(a, "title", "") or ""
        return (_source_priority(src), -len(_normalize_title(title)))

    return sorted(group, key=score)[0]


# =========================
# ✅ (C) 중복 제거 + “외 n개 매체”용 묶기
#     규칙:
#       - (정규화 URL + 정규화 제목) 완전 동일 → 중복
#       - summary 유사도 >= 0.80 → 중복
# =========================
def dedupe_and_group_articles(articles, threshold: float = 0.80):
    """
    반환: 대표 기사 리스트
    대표 기사에는 a.duplicates = [{source, link, title}, ...] 가 생김
    """
    # 1) URL+제목 완전 동일 기준으로 1차 그룹핑
    exact_map = {}
    for a in articles:
        url_key = _normalize_url(getattr(a, "link", ""))
        title_key = _normalize_title(getattr(a, "title", ""))
        key = (url_key, title_key)
        exact_map.setdefault(key, []).append(a)

    stage1_groups = list(exact_map.values())

    # 2) 요약 유사도 기반으로 그룹 병합
    #    - 전체 n^2 방지: 제목 키 prefix 버킷으로 후보만 비교
    buckets = {}  # bucket_key -> list[group]
    merged_groups = []

    for grp in stage1_groups:
        # 그룹의 대표 임시(첫번째)로 비교 텍스트 준비
        base = grp[0]
        base_title = getattr(base, "title", "") or ""
        base_summary = getattr(base, "summary", "") or ""

        bucket_key = _normalize_title(base_title)[:40]
        cand_groups = buckets.get(bucket_key, [])

        merged = False
        for existing_grp in cand_groups:
            ex = existing_grp[0]
            ex_title = getattr(ex, "title", "") or ""
            ex_summary = getattr(ex, "summary", "") or ""

            # summary가 둘 다 있으면 summary로, 아니면 title 유사도로 보조
            if base_summary and ex_summary:
                sim = _similarity(base_summary, ex_summary)
            else:
                sim = _similarity(base_title, ex_title)

            if sim >= threshold:
                existing_grp.extend(grp)
                merged = True
                break

        if not merged:
            merged_groups.append(grp)
            cand_groups.append(grp)
            buckets[bucket_key] = cand_groups

    # 3) 각 그룹에서 대표 선택 + duplicates 정보 생성
    representatives = []
    for grp in merged_groups:
        rep = _pick_representative(grp)

        # rep.duplicates = rep 제외한 나머지
        dups = []
        for a in grp:
            if a is rep:
                continue
            dups.append({
                "source": getattr(a, "source", "") or "",
                "link": getattr(a, "link", "") or "",
                "title": getattr(a, "title", "") or "",
            })

        # rep에 속성 부여(동적)
        setattr(rep, "duplicates", dups)
        representatives.append(rep)

    return representatives


# =========================
# ✅ (D) 카테고리 간 중복 제거
#     - 대표 기사만 남기고(이미 대표만 존재),
#       우선순위대로 하나의 카테고리에만 남김
# =========================
def remove_cross_category_duplicates(*category_lists):
    """
    우선순위대로 하나의 카테고리에만 남김.
    (acuvue → company → product → trend → eye_health)
    """
    seen = set()
    out = []

    for lst in category_lists:
        new_lst = []
        for a in lst:
            url_key = _normalize_url(getattr(a, "link", ""))
            title_key = _normalize_title(getattr(a, "title", ""))
            key = (url_key, title_key)

            if key in seen:
                continue

            seen.add(key)
            new_lst.append(a)
        out.append(new_lst)

    return out


# =========================
# ✅ (E) 3~4문장 고정 AI 브리핑
# =========================
def build_yesterday_summary_3to4(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
):
    sentences = []

    if acuvue_articles:
        titles = [a.title for a in acuvue_articles[:2]]
        sentences.append(
            "어제 기사 중 ACUVUE 관련 내용으로는 "
            + " / ".join(titles)
            + " 등이 주요하게 다뤄졌습니다."
        )

    category_points = []
    if company_articles:
        category_points.append("경쟁사 및 업체별 활동")
    if product_articles:
        category_points.append("제품 카테고리별 이슈")
    if trend_articles:
        category_points.append("업계 전반 동향")
    if eye_health_articles:
        category_points.append("눈 건강 및 캠페인 관련 움직임")

    if category_points:
        sentences.append(
            "이와 함께 " + ", ".join(category_points) + " 관련 기사들이 확인되었습니다."
        )

    sentences.append(
        "전반적으로 시장 및 경쟁 환경의 변화가 향후 전략 수립 시 참고할 만한 흐름으로 판단됩니다."
    )

    return " ".join(sentences[:3])


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 1) 수집
    articles = fetch_all_articles(cfg)

    # 2) 제외 규칙 1차(투자/재무/실적 + 가수 다비치/강민경/이해리 + 얼굴노안 등)
    articles = filter_out_finance_articles(articles)

    # 3) 날짜 필터: 어제 기사만
    articles = filter_yesterday_articles(articles, cfg)

    # 4) 1차 중복 제거(빠른 제거: URL+제목)  ← scrapers.py
    articles = deduplicate_articles(articles)

    # 5) 기사 요약(summary 채우기)
    refine_article_summaries(articles)

    # ✅ (선택) 최종 안전 필터: 요약 후에도 살아남는 케이스 차단
    articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]

    # ✅ 6) 중복 제거(규칙 적용) + “외 n개 매체”용 묶기
    #    - URL/제목 동일 OR summary 유사도 0.85 이상
    #    - 대표 언론사: 업계지 1순위
    articles = dedupe_and_group_articles(articles, threshold=0.85)

    # 7) 분류
    categorized = categorize_articles(articles)

    # 8) 카테고리 간 중복 제거 (우선순위대로 하나만 남김)
    acuvue_list, company_list, product_list, trend_list, eye_health_list = remove_cross_category_duplicates(
        categorized.acuvue,
        categorized.company,
        categorized.product,
        categorized.trend,
        categorized.eye_health,
    )

    # 9) 어제 기사 AI 브리핑
    summary = build_yesterday_summary_2to3(
        acuvue_list,
        company_list,
        product_list,
        trend_list,
        eye_health_list,
    )

    # 10) 템플릿 렌더링
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("template_newsletter.html")

    html = template.render(
        today_date=dt.datetime.now(tz).strftime("%Y-%m-%d"),
        yesterday_summary=summary,
        acuvue_articles=acuvue_list,
        company_articles=company_list,
        product_articles=product_list,
        trend_articles=trend_list,
        eye_health_articles=eye_health_list,
    )

    # 11) 메일 제목
    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # 12) 발송
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
