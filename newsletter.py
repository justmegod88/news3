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


def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 1) 수집
    articles = fetch_all_articles(cfg)

    # 2) 제외 규칙 적용(투자/재무/실적 + 가수다비치/강민경/이해리 + 얼굴노안 등)
    articles = filter_out_finance_articles(articles)

    # 3) 날짜 필터: 어제 기사만
    articles = filter_yesterday_articles(articles, cfg)

    # 4) 중복 제거(느슨: URL 완전 동일만)
    articles = deduplicate_articles(articles)

    # 5) 요약/분류
    refine_article_summaries(articles)
    categorized = categorize_articles(articles)
    summary = summarize_overall(articles)

    # 6) 템플릿 렌더링
    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("template_newsletter.html")

    html = template.render(
        today_date=dt.datetime.now(tz).strftime("%Y-%m-%d"),
        yesterday_summary=summary,
        acuvue_articles=categorized.acuvue,
        company_articles=categorized.company,
        product_articles=categorized.product,
        trend_articles=categorized.trend,
        eye_health_articles=categorized.eye_health,
    )

    # 7) 메일 제목: 어제 기사 브리핑 - YYYY-MM-DD
    email = cfg["email"]
    yesterday_str = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    subject_prefix = email.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제 기사 브리핑 - {yesterday_str}"

    # 8) 발송
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
