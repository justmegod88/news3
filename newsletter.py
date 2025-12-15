import datetime as dt
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_by_keywords,
    filter_out_finance_articles,
)
from categorizer import categorize_articles
from summarizer import summarize_overall, refine_article_summaries
from mailer import send_email_html


def render_newsletter_html(cfg, categorized, yesterday_summary: str) -> str:
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    now = dt.datetime.now(tz=tz)
    today_str = now.strftime("%Y-%m-%d (%a)")

    env = Environment(
        loader=FileSystemLoader("."),
        autoescape=True,
    )
    template = env.get_template("template_newsletter.html")

    html = template.render(
        today_date=today_str,
        yesterday_summary=yesterday_summary,
        acuvue_articles=categorized.acuvue,
        company_articles=categorized.company,
        product_articles=categorized.product,
        trend_articles=categorized.trend,
        eye_health_articles=categorized.eye_health,
    )
    return html


def main():
    cfg = load_config("config.yaml")

    # 1) 전체 기사 수집
    all_articles = fetch_all_articles(cfg)
    # 2) 어제 기사만
    y_articles = filter_yesterday_articles(all_articles, cfg)
    # 3) 키워드 필터 적용
    y_kw_articles = filter_by_keywords(y_articles, cfg)
    # 3-1) (요청사항 #2) 투자/재무/실적 중심 기사 제외
    y_kw_articles = filter_out_finance_articles(y_kw_articles)

    # 4) 각 기사 개별 요약을 GPT로 다듬기 (제목과 다른, 깔끔한 1~3문장)
    refine_article_summaries(y_kw_articles)

    # 5) 카테고리 분류
    categorized = categorize_articles(y_kw_articles)

    # 6) 어제 기사 브리핑 생성 (임원 보고용)
    yesterday_summary = summarize_overall(y_kw_articles)

    # 7) HTML 렌더링
    html_body = render_newsletter_html(cfg, categorized, yesterday_summary)

    # 8) 메일 발송
    email_conf = cfg["email"]
    subject_prefix = email_conf.get("subject_prefix", "[Daily News]")
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    now = dt.datetime.now(tz=tz)
    # 최근 24시간 기준으로 제목 표기
    start = (now - dt.timedelta(hours=24)).strftime("%m/%d %H:%M")
    end = now.strftime("%m/%d %H:%M")
    subject = f"{subject_prefix} 최근 24시간 기사 브리핑 – {start}~{end}"

    send_email_html(
        subject=subject,
        html_body=html_body,
        from_addr=email_conf["from"],
        to_addrs=email_conf["to"],
    )


if __name__ == "__main__":
    main()
