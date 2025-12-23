from datetime import datetime
from jinja2 import Environment, FileSystemLoader

from mailer import send_email_html


def build_yesterday_summary(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
):
    sentences = []

    if acuvue_articles:
        sentences.append(
            "어제 기사 중 ACUVUE 관련 내용으로는 "
            + " / ".join([a["title"] for a in acuvue_articles[:2]])
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
            "이와 함께 "
            + ", ".join(category_points)
            + " 관련 기사들이 확인되었습니다."
        )

    sentences.append(
        "전반적으로 시장 및 경쟁 환경의 변화가 "
        "향후 전략 수립 시 참고할 만한 흐름으로 판단됩니다."
    )

    return " ".join(sentences[:3])


def run_newsletter(
    acuvue_articles,
    company_articles,
    product_articles,
    trend_articles,
    eye_health_articles,
    to_addrs,
):
    today_date = datetime.now().strftime("%Y-%m-%d")

    yesterday_summary = build_yesterday_summary(
        acuvue_articles,
        company_articles,
        product_articles,
        trend_articles,
        eye_health_articles,
    )

    env = Environment(loader=FileSystemLoader("."))
    template = env.get_template("template_newsletter.html")

    html_body = template.render(
        today_date=today_date,
        yesterday_summary=yesterday_summary,
        acuvue_articles=acuvue_articles,
        company_articles=company_articles,
        product_articles=product_articles,
        trend_articles=trend_articles,
        eye_health_articles=eye_health_articles,
    )

    send_email_html(
        subject=f"[ACUVUE Daily News] 어제 기사 브리핑 - {today_date}",
        html_body=html_body,
        from_addr="newsletter@acuvue.com",
        to_addrs=to_addrs,
    )


if __name__ == "__main__":
    run_newsletter(
        acuvue_articles=[],
        company_articles=[],
        product_articles=[],
        trend_articles=[],
        eye_health_articles=[],
        to_addrs=["you@example.com"],
    )
