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
    Article,                     # ✅ 강제 기사 추가용
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
# ✅ (C) 기사 리스트용 중복 제거 + 묶기
# =========================
def dedupe_and_group_articles(articles, threshold: float = 0.78):
    exact_map = {}
    for a in articles:
        url_key = _normalize_url(getattr(a, "link", ""))
        title_key = _normalize_title(getattr(a, "title", ""))
        key = (url_key, title_key)
        exact_map.setdefault(key, []).append(a)

    stage1_groups = list(exact_map.values())

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
            key = (
                _normalize_url(getattr(a, "link", "")),
                _normalize_title(getattr(a, "title", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            new_lst.append(a)
        out.append(new_lst)
    return out


# =========================
# ✅ (E/F) 브리핑 관련 함수들 (기존 유지)
# =========================
def _brief_norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\[[^\]]+\]", " ", s)
    s = re.sub(r"\([^)]*\)", " ", s)
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
    kept = []
    for a in articles:
        key_text = (a.summary or "").strip() or (a.title or "")
        if any(_brief_sim(key_text, (k.summary or "").strip() or (k.title or "")) >= threshold for k in kept):
            continue
        kept.append(a)
        if len(kept) >= max_keep:
            break
    return kept


def build_yesterday_ai_brief(acuvue, company, product, trend, eye):
    picked = dedupe_for_brief(acuvue + company + product + trend + eye, threshold=0.70, max_keep=10)
    if not picked:
        return "어제는 수집된 기사가 없어 주요 이슈를 요약할 내용이 없습니다."
    return summarize_overall(picked)


# =========================
# ✅ MAIN
# =========================
def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 1) 수집
    articles = fetch_all_articles(cfg)

    # 2) 필터
    articles = filter_out_yakup_articles(articles)
    articles = filter_out_finance_articles(articles)

    # 3) 날짜
    articles = filter_yesterday_articles(articles, cfg)

    # 4) 1차 dedup
    articles = deduplicate_articles(articles)

    # 5) 요약
    refine_article_summaries(articles)

    # 6) 최종 안전 필터
    articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]

    # =========================
    # 🚨 [임시] 강제 기사 추가 (오늘 발송용)
    # =========================
    now_kst = dt.datetime.now(tz)

    forced_articles = [
        Article(
            title="AI눈 장착하니, 불량률 1%에서 0.01%로 줄었다",
            link="https://n.news.naver.com/article/016/0002584370?sid=101",
            published=now_kst,
            source="네이버뉴스",
            summary="네이버전 오송 공장, 라인곳곳 고해상동 카메라 설치 AI가 0.1초만에 불량 렌즈 판독",
            image_url=None,
            is_naver=True,
        )
    
    ]

    articles.extend(forced_articles)
    # =========================

    # 7) 그룹 dedup
    articles = dedupe_and_group_articles(articles, threshold=0.80)

    # 8) 분류
    categorized = categorize_articles(articles)

    # 9) 카테고리 간 중복 제거
    acuvue, company, product, trend, eye = remove_cross_category_duplicates(
        categorized.acuvue,
        categorized.company,
        categorized.product,
        categorized.trend,
        categorized.eye_health,
    )

    # 10) 브리핑
    summary = build_yesterday_ai_brief(acuvue, company, product, trend, eye)

    # 11) 렌더링
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("template_newsletter.html")
    html = template.render(
        today_date=dt.datetime.now(tz).strftime("%Y-%m-%d"),
        yesterday_summary=summary,
        acuvue_articles=acuvue,
        company_articles=company,
        product_articles=product,
        trend_articles=trend,
        eye_health_articles=eye,
    )

    # 12) 제목
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject = f"{cfg['email'].get('subject_prefix', '[Daily News]')} 어제 기사 브리핑 - {yesterday_str}"

    # 13) 발송
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=cfg["email"]["from"],
        to_addrs=cfg["email"]["to"],
    )


if __name__ == "__main__":
    main()
