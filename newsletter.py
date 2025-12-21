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

    # 1. 수집
    articles = fetch_all_articles(cfg)

    # 2. 제외
    articles = filter_out_finance_articles(articles)

    # 3. 날짜 (어제)
    articles = filter_yesterday_articles(articles, cfg)

    # 4. 중복 (느슨)
    articles = deduplicate_articles(articles)

    # 5. 요약
    refine_article_summaries(articles)

    categorized = categorize_articles(articles)
    summary = summarize_overall(articles)

    env = Environment(loader=FileSystemLoader("."))
    template = env.get_template("template_newsletter.html")

    html = template.render(
        today_date=(dt.datetime.now(tz).strftime("%Y-%m-%d")),
        yesterday_summary=summary,
        acuvue_articles=categorized.acuvue,
        company_articles=categorized.company,
        product_articles=categorized.product,
        trend_articles=categorized.trend,
        eye_health_articles=categorized.eye_health,
    )

    email = cfg["email"]
    
     # 어제 날짜 문자열
     yesterday = (dt.datetime.now(tz).date() - dt.timedelta(days=1)).strftime("%Y-%m-%d")

     subject = (
    f"{email.get('subject_prefix', '[Daily News]')} "
    f"어제 기사 브리핑_{yesterday}"
)
    send_email_html(
        subject=subject,
        html_body=html,
        from_addr=email["from"],
        to_addrs=email["to"],
    )


if __name__ == "__main__":
    main()
