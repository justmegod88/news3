import datetime as dt
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import re
import difflib

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    annotate_image_ads,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_out_finance_articles,
    filter_out_yakup_articles,
    deduplicate_articles,
    should_exclude_article,
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
# ✅ (C) 중복 제거 + 묶기
# =========================
def dedupe_and_group_articles(articles, threshold: float = 0.80):
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
# =========================
def build_yesterday_summary_3to4(acuvue_articles, company_articles, product_articles, trend_articles, eye_health_articles):
    merged = []
    for lst in [acuvue_articles, company_articles, product_articles, trend_articles, eye_health_articles]:
        merged.extend(lst or [])
    return summarize_overall(merged)


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # ✅ 어떤 에러가 나도 "메일 자체는" 가게 하는 fallback
    fallback_html = f"""<!doctype html><html lang="ko"><body>
    <h2>어제 기사 브리핑 - {yesterday_str}</h2>
    <p>자동 생성 과정에서 오류가 발생하여 상세 본문을 구성하지 못했습니다.</p>
    <p>GitHub Actions 로그를 확인해주세요.</p>
    </body></html>"""

    try:
        print("[STEP 1] fetch_all_articles")
        articles = fetch_all_articles(cfg)
        print(f"[DEBUG] fetched: {len(articles)}")

        print("[STEP 2] filter_out_yakup / finance")
        articles = filter_out_yakup_articles(articles)
        articles = filter_out_finance_articles(articles)
        print(f"[DEBUG] after yakup/finance: {len(articles)}")

        print("[STEP 3] filter_yesterday_articles")
        articles = filter_yesterday_articles(articles, cfg)
        print(f"[DEBUG] after yesterday: {len(articles)}")

        print("[STEP 4] deduplicate_articles (fast)")
        articles = deduplicate_articles(articles)
        print(f"[DEBUG] after dedup1: {len(articles)}")

        print("[STEP 4-1] annotate_image_ads (safe)")
        annotate_image_ads(articles, timeout_connect=3.0, timeout_read=5.0, max_checks=60)
        img_ads = sum(1 for a in articles if getattr(a, "is_image_ad", False))
        print(f"[DEBUG] image_ads marked: {img_ads} / total: {len(articles)}")

        print("[STEP 5] refine_article_summaries")
        refine_article_summaries(articles)
        empty_sum = sum(1 for a in articles if not (getattr(a, "summary", "") or "").strip())
        print(f"[DEBUG] empty summaries: {empty_sum}")

        print("[STEP 6] should_exclude_article")
        articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]
        print(f"[DEBUG] after safety filter: {len(articles)}")

        print("[STEP 7] dedupe_and_group_articles")
        articles = dedupe_and_group_articles(articles, threshold=0.80)
        print(f"[DEBUG] after dedup2(group): {len(articles)}")

        print("[STEP 8] categorize_articles")
        categorized = categorize_articles(articles)

        print("[STEP 9] remove_cross_category_duplicates")
        acuvue_list, company_list, product_list, trend_list, eye_health_list = remove_cross_category_duplicates(
            categorized.acuvue,
            categorized.company,
            categorized.product,
            categorized.trend,
            categorized.eye_health,
        )
        print("[DEBUG] category counts:",
              len(acuvue_list), len(company_list), len(product_list), len(trend_list), len(eye_health_list))

        print("[STEP 10] build_yesterday_summary_3to4")
        summary = build_yesterday_summary_3to4(acuvue_list, company_list, product_list, trend_list, eye_health_list)
        print(f"[DEBUG] overall summary length: {len(summary or '')}")

        print("[STEP 11] template render")
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

    except Exception as e:
        print("[ERROR] pipeline failed:", repr(e))
        html = fallback_html

    print("[STEP 12] send_email_html")
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )
    print("[DONE] send_email_html called")


if __name__ == "__main__":
    main()
