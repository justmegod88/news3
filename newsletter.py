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
from summarizer import refine_article_summaries, summarize_overall
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

    keys.add(" ".join(tokens[:2]))
    if len(tokens) >= 3:
        keys.add(" ".join(tokens[:3]))
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
# ✅ (C) 기사 리스트용 중복 제거 + 묶기 (기존 유지: threshold=0.80)
# =========================
def dedupe_and_group_articles(articles, threshold: float = 0.73):
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
    buckets = {}
    merged_groups = []

    for grp in stage1_groups:
        base = grp[0]
        base_title = getattr(base, "title", "") or ""
        base_summary = getattr(base, "summary", "") or ""

        bucket_keys = _title_bucket_keys(base_title)
        cand_groups = []
        for k in bucket_keys:
            cand_groups.extend(buckets.get(k, []))

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
# ✅ (E) 브리핑(상단 요약) 전용 중복 제거: threshold=0.65 (요청 반영)
# =========================
def _brief_norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\[[^\]]+\]", " ", s)      # [단독]
    s = re.sub(r"\([^)]*\)", " ", s)       # (종합)
    s = re.sub(r"[^\w가-힣 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _brief_sim(a: str, b: str) -> float:
    a = _brief_norm(a)
    b = _brief_norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def dedupe_for_brief(articles, threshold: float = 0.70, max_keep: int = 10):
    """
    ✅ 브리핑(상단 AI 요약) 전용 중복 제거
    - "주제 같으면 제거" 목적이라 threshold를 0.70로 낮춤 (요청 반영)
    - summary가 있으면 summary로 비교, 없으면 title로 비교
    """
    kept = []
    for a in articles:
        t = getattr(a, "title", "") or ""
        s = getattr(a, "summary", "") or ""
        key_text = s if s.strip() else t

        dup = False
        for k in kept:
            kt = getattr(k, "title", "") or ""
            ks = getattr(k, "summary", "") or ""
            k_text = ks if ks.strip() else kt

            if _brief_sim(key_text, k_text) >= threshold:
                dup = True
                break

        if not dup:
            kept.append(a)

        if len(kept) >= max_keep:
            break

    return kept


# =========================
# ✅ (F) 브리핑 입력 후보 선택 (카테고리 분산 + 빈 summary 제외) + 브리핑 전용 dedupe(0.60)
# =========================
def _has_summary(a) -> bool:
    s = (getattr(a, "summary", "") or "").strip()
    return len(s) > 0


def select_articles_for_brief(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
    max_items: int = 10,
):
    """
    - 광고/단순 이미지로 summary가 빈 값인 기사는 제외
    - 카테고리별로 1~2개씩 분산 선택(맨 위 편향 완화)
    - 브리핑 전용 dedupe(주제 중복 제거): threshold=0.60 적용
    """
    pools = [
        ("ACUVUE", [a for a in (acuvue_articles or []) if _has_summary(a)]),
        ("Company", [a for a in (company_articles or []) if _has_summary(a)]),
        ("Trend", [a for a in (trend_articles or []) if _has_summary(a)]),
        ("Product", [a for a in (product_articles or []) if _has_summary(a)]),
        ("EyeHealth", [a for a in (eye_health_articles or []) if _has_summary(a)]),
    ]

    # 1) 라운드로빈 분산 선택
    selected = []
    idx = 0
    while len(selected) < max_items:
        added_any = False
        for _, lst in pools:
            if idx < len(lst) and len(selected) < max_items:
                selected.append(lst[idx])
                added_any = True
        if not added_any:
            break
        idx += 1

    # 2) URL+제목 동일 중복 제거(안전)
    seen = set()
    deduped = []
    for a in selected:
        key = (_normalize_url(getattr(a, "link", "")), _normalize_title(getattr(a, "title", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)

    # ✅ 3) 브리핑 전용 "주제 중복" 제거 (요청: 0.60)
    deduped = dedupe_for_brief(deduped, threshold=0.60, max_keep=max_items)

    return deduped[:max_items]


def build_yesterday_ai_brief(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
):
    picked = select_articles_for_brief(
        acuvue_articles,
        company_articles,
        product_articles,
        trend_articles,
        eye_health_articles,
        max_items=10,
    )

    if not picked:
        return "어제는 수집된 기사가 없어 주요 이슈를 요약할 내용이 없습니다."

    return summarize_overall(picked)


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

    # 4) 1차 중복 제거(빠른 제거: URL+제목)
    articles = deduplicate_articles(articles)

    # 5) 기사별 요약(summary 정제/생성)
    refine_article_summaries(articles)

    # 6) 최종 안전 필터
    articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]

    # ✅ 7) 기사 리스트용 중복 묶기(기존 유지: 0.60)
    articles = dedupe_and_group_articles(articles, threshold=0.60)

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

    # ✅ 10) 상단 브리핑(브리핑 전용 dedupe=0.60 적용된 picked로 요약)
    summary = build_yesterday_ai_brief(
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
