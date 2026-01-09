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
    filter_out_yakup_articles,
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


def _title_bucket_keys(title: str):
    """
    중복 후보 비교 대상을 좁히기 위한 버킷 키.
    너무 좁으면 중복을 못 잡아서, 토큰 2~3개 조합으로 넓게 잡음.
    """
    nt = _normalize_title(title)
    tokens = [x for x in nt.split() if len(x) >= 2]
    keys = set()

    if not tokens:
        return keys

    # 1) 앞 2개 토큰
    keys.add(" ".join(tokens[:2]))
    # 2) 앞 3개 토큰
    if len(tokens) >= 3:
        keys.add(" ".join(tokens[:3]))
    # 3) 앞 1개 토큰(완전 유사 타이틀 잡기용)
    keys.add(tokens[0])

    return keys


# =========================
# ✅ (B) 대표 기사(언론사) 선택 규칙
# =========================
INDUSTRY_SOURCES = {
    "안경신문",
    "옵티컬저널",
    "옵티칼저널",
    "안경계",
    "아이케어뉴스",
    "메디칼업저버",
    "의학신문",
    "헬스조선",
    "바이오타임즈",
}

TIER2_SOURCES = {
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
    return 99


def _pick_representative(group):
    def score(a):
        src = getattr(a, "source", "") or ""
        title = getattr(a, "title", "") or ""
        return (_source_priority(src), -len(_normalize_title(title)))
    return sorted(group, key=score)[0]


# =========================
# ✅ (C) 중복 제거 + 묶기
#     ✅ 요청 반영: 코어키워드 조건 제거
#     ✅ 수집된 기사끼리 유사도 >= 0.80 이면 그냥 중복으로 묶음
# =========================
def dedupe_and_group_articles(articles, threshold: float = 0.80):
    """
    반환: 대표 기사 리스트
    대표 기사에는 rep.duplicates = [{source, link, title}, ...] 가 생김
    """

    # 1) URL+제목 완전 동일 기준으로 1차 그룹핑
    exact_map = {}
    for a in articles:
        url_key = _normalize_url(getattr(a, "link", ""))
        title_key = _normalize_title(getattr(a, "title", ""))
        key = (url_key, title_key)
        exact_map.setdefault(key, []).append(a)

    stage1_groups = list(exact_map.values())

    # 2) 요약/제목 유사도 기반 그룹 병합
    #    - 버킷을 넓혀서(토큰 기반) 중복을 더 잘 잡음
    buckets = {}      # bucket_key -> list[group]
    merged_groups = []

    for grp in stage1_groups:
        base = grp[0]
        base_title = getattr(base, "title", "") or ""
        base_summary = getattr(base, "summary", "") or ""

        bucket_keys = _title_bucket_keys(base_title)
        cand_groups = []
        for k in bucket_keys:
            cand_groups.extend(buckets.get(k, []))

        # cand_groups 중복 제거(참조 중복)
        seen_ref = set()
        uniq_cands = []
        for g in cand_groups:
            gid = id(g)
            if gid in seen_ref:
                continue
            seen_ref.add(gid)
            uniq_cands.append(g)

        merged = False
        for existing_grp in uniq_cands:
            ex = existing_grp[0]
            ex_title = getattr(ex, "title", "") or ""
            ex_summary = getattr(ex, "summary", "") or ""

            # summary가 둘 다 있으면 summary로, 아니면 title 유사도로 보조
            if base_summary and ex_summary:
                sim = _similarity(base_summary, ex_summary)
            else:
                sim = _similarity(base_title, ex_title)

            # ✅ 핵심: 유사도만으로 병합
            if sim >= threshold:
                existing_grp.extend(grp)
                merged = True
                break

        if not merged:
            merged_groups.append(grp)
            for k in bucket_keys:
                buckets.setdefault(k, []).append(grp)

    # 3) 각 그룹에서 대표 선택 + duplicates 정보 생성
    representatives = []
    for grp in merged_groups:
        rep = _pick_representative(grp)
        dups = []
        for a in grp:
            if a is rep:
                continue
            dups.append({
                "source": getattr(a, "source", "") or "",
                "link": getattr(a, "link", "") or "",
                "title": getattr(a, "title", "") or "",
            })
        setattr(rep, "duplicates", dups)
        representatives.append(rep)

    return representatives


# =========================
# ✅ (D) 카테고리 간 중복 제거
# =========================
def remove_cross_category_duplicates(*category_lists):
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
# ✅ (E) 3~4문장 AI 브리핑
#     - 기사 0건이면 "요약 없음"
#     - 기사 1개면 "이와 함께" 같은 표현 제거
# =========================
def build_yesterday_summary_3to4(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
):
    total = (
        len(acuvue_articles)
        + len(company_articles)
        + len(product_articles)
        + len(trend_articles)
        + len(eye_health_articles)
    )

    if total == 0:
        return "어제는 수집된 기사가 없어 주요 이슈를 요약할 내용이 없습니다."

    sentences = []

    # ACUVUE 문장(있을 때만)
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

    # ✅ 문장 수/기사 수에 따라 자연스럽게
    if category_points:
        # ACUVUE 문장이 이미 있으면 "이와 함께", 없으면 그냥 시작
        prefix = "이와 함께 " if sentences else ""

        if total == 1:
            # 기사 1개면 단일 문장(어색한 연결어 제거)
            sentences.append(f"어제는 {category_points[0]} 관련 기사 1건이 확인되었습니다.")
        elif len(category_points) == 1:
            sentences.append(f"{prefix}{category_points[0]} 관련 기사가 확인되었습니다.")
        else:
            sentences.append(f"{prefix}{', '.join(category_points)} 관련 기사들이 확인되었습니다.")

    # 총평은 기사 충분할 때만
    if total >= 3:
        sentences.append(
            "전반적으로 시장 및 경쟁 환경의 변화가 향후 전략 수립 시 참고할 만한 흐름으로 판단됩니다."
        )

    return " ".join(sentences[:3])


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 1) 수집
    articles = fetch_all_articles(cfg)

    # 2) 약업신문 제외 + 투자/재무 제외
    articles = filter_out_yakup_articles(articles)
    articles = filter_out_finance_articles(articles)

    # 3) 날짜 필터: 어제 기사만
    articles = filter_yesterday_articles(articles, cfg)

    # 4) 1차 중복 제거(빠른 제거: URL+제목)  ← scrapers.py
    articles = deduplicate_articles(articles)

    # 5) 기사 요약(summary 채우기)
    refine_article_summaries(articles)

    # 6) 최종 안전 필터
    articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]

    # ✅ 7) 중복 제거(요청 반영): 유사도 0.80 이상이면 중복으로 묶기
    articles = dedupe_and_group_articles(articles, threshold=0.80)

    # 8) 분류
    categorized = categorize_articles(articles)

    # 9) 카테고리 간 중복 제거
    acuvue_list, company_list, product_list, trend_list, eye_health_list = remove_cross_category_duplicates(
        categorized.acuvue,
        categorized.company,
        categorized.product,
        categorized.trend,
        categorized.eye_health,
    )

    # 10) 어제 기사 AI 브리핑
    summary = build_yesterday_summary_3to4(
        acuvue_list,
        company_list,
        product_list,
        trend_list,
        eye_health_list,
    )

    # 11) 템플릿 렌더링
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

    # 12) 메일 제목
    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # 13) 발송
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
