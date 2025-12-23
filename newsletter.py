import datetime as dt
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_out_finance_articles,
    deduplicate_articles,
)
from categorizer import categorize_articles
from summarizer import summarize_overall, refine_article_summaries
from mailer import send_email_html


def _pick_summary_articles(categorized):
    """
    ✅ 요약 규칙(우선순위 1→2→3, 4는 제외):
    1) company_articles(업체별 활동) 있으면 → 이것만
    2) 없으면 product_articles(제품) 있으면 → 이것만
    3) 없으면 trend_articles(업계동향) 있으면 → 이것만
    4) eye_health는 요약에서 항상 제외
    """
    if getattr(categorized, "company", None):
        if len(categorized.company) > 0:
            return categorized.company

    if getattr(categorized, "product", None):
        if len(categorized.product) > 0:
            return categorized.product

    if getattr(categorized, "trend", None):
        if len(categorized.trend) > 0:
            return categorized.trend

    return []


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 1) 수집
    articles = fetch_all_articles(cfg)

    # 2) 제외(투자/재무/실적 + 다비치가수/연예 + 얼굴노안 등)
    articles = filter_out_finance_articles(articles)

    # 3) 날짜 필터(어제 00:00~23:59 또는 네 로직)
    articles = filter_yesterday_articles(articles, cfg)

    # 4) 중복 제거(URL + 제목 등 네가 만든 dedup 로직)
    articles = deduplicate_articles(articles)

    # 5) 기사 개별 요약 정리(너무 긴 요약 컷)
    refine_article_summaries(articles)

    # 6) 분류
    categorized = categorize_articles(articles)

    # ✅ 7) “어제 기사 AI 브리핑”은 1/2/3 중 하나만 선택해서 요약 (4는 제외)
    summary_articles = _pick_summary_articles(categorized)
    yesterday_summary = summarize_overall(summary_articles)

    # 8) 템플릿 렌더
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("template_newsletter.html")

    html_body = template.render(
        today_date=dt.datetime.now(tz).strftime("%Y-%m-%d"),
        yesterday_summary=yesterday_summary,
        acuvue_articles=categorized.acuvue,
        company_articles=categorized.company,
        product_articles=categorized.product,
        trend_articles=categorized.trend,
        eye_health_articles=categorized.eye_health,
    )

    # 9) 제목(어제 날짜 포함)
    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # 10) 발송
    send_email_html(
        subject=subject,
        html_body=html_body,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
