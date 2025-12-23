import datetime as dt
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import re

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_out_finance_articles,
    deduplicate_articles,        # (scrapers.py의 URL+제목 dedup)
    should_exclude_article,      # ✅ 최종 안전 필터용
)
from categorizer import categorize_articles
from summarizer import refine_article_summaries
from mailer import send_email_html


# =========================
# ✅ (A) 카테고리 간 중복 제거용 키
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
# ✅ (B) 2~3문장 고정 AI 브리핑 (아큐브 + 카테고리 동시 반영)
# =========================
def build_yesterday_summary_2to3(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
):
    sentences = []

    # 1) ACUVUE 기사 (있으면 무조건 포함)
    if acuvue_articles:
        titles = [a.title for a in acuvue_articles[:2]]
        sentences.append(
            "어제 기사 중 ACUVUE 관련 내용으로는 "
            + " / ".join(titles)
            + " 등이 주요하게 다뤄졌습니다."
        )

    # 2) 존재하는 카테고리만 함께 언급
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

    # 3) 공통 마무리
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

    # 4) 중복 제거(강화: URL + 제목 정규화 기준)  ← scrapers.py에 이미 구현됨
    articles = deduplicate_articles(articles)

    # 5) 기사 요약(각 기사 summary 채우기)
    refine_article_summaries(articles)

    # ✅ (선택) 최종 안전 필터: 요약 후에도 혹시 살아남는 “가수 다비치” 같은 케이스 차단
    # (요약을 생성하면서 summary에 힌트가 생기는 경우가 있어, 한 번 더 거르는 게 안전)
    articles = [a for a in articles if not should_exclude_article(a.title, a.summary)]

    # 6) 분류
    categorized = categorize_articles(articles)

    # 7) ✅ 카테고리 간 중복 제거 (우선순위대로 하나만 남김)
    acuvue_list, company_list, product_list, trend_list, eye_health_list = remove_cross_category_duplicates(
        categorized.acuvue,
        categorized.company,
        categorized.product,
        categorized.trend,
        categorized.eye_health,
    )

    # 8) ✅ 어제 기사 AI 브리핑: 2~3문장 고정 + 아큐브 있으면 무조건 포함 + 카테고리도 같이
    summary = build_yesterday_summary_2to3(
        acuvue_list,
        company_list,
        product_list,
        trend_list,
        eye_health_list,
    )

    # 9) 템플릿 렌더링
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

    # 10) 메일 제목: 어제 기사 브리핑 - YYYY-MM-DD
    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # 11) 발송
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
