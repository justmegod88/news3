import datetime as dt
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,   # ✅ 어제 하루(고정) + 네이버 날짜만
    filter_out_finance_articles, # ✅ 투자/재무/실적 + 가수다비치 제외
)
from categorizer import categorize_articles
from summarizer import summarize_overall, refine_article_summaries
from mailer import send_email_html


def _log(step: str, n: int):
    print(f"🧾 {step}: {n}건")


def _normalize_link(url: str) -> str:
    if not url:
        return url
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="", fragment=""))
    except Exception:
        return url


def dedup_articles_by_link(articles):
    """
    ✅ 뉴스레터에서만 중복 제거:
    - 링크 기준으로만 중복 제거 (공격적 제거 X)
    """
    seen = set()
    out = []
    for a in articles:
        link = _normalize_link(getattr(a, "link", "") or "")
        key = link if link else (getattr(a, "title", ""), getattr(a, "source", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def render_newsletter_html(cfg, categorized, yesterday_summary: str) -> str:
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    now = dt.datetime.now(tz=tz)
    today_str = now.strftime("%Y-%m-%d (%a)")

    env = Environment(loader=FileSystemLoader("."), autoescape=True)
    template = env.get_template("template_newsletter.html")

    return template.render(
        today_date=today_str,
        yesterday_summary=yesterday_summary,
        acuvue_articles=categorized.acuvue,
        company_articles=categorized.company,
        product_articles=categorized.product,
        trend_articles=categorized.trend,
        eye_health_articles=categorized.eye_health,
    )


def main():
    cfg = load_config("config.yaml")
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))

    # 기준 날짜: 어제(달력 기준)
    today = dt.datetime.now(tz=tz).date()
    yesterday = today - dt.timedelta(days=1)

    print("🚀 뉴스레터 생성 시작")

    # 1) 전체 기사 수집(키워드 기반으로 최대한)
    all_articles = fetch_all_articles(cfg)
    _log("전체 수집(원본)", len(all_articles))

    # 2) 어제 하루(00:00~23:59)만 포함
    y_articles = filter_yesterday_articles(all_articles, cfg)
    _log(f"어제({yesterday.isoformat()}) 기사 필터 후", len(y_articles))

    # 3) 투자/재무/실적 + 가수 다비치 제외
    y_articles = filter_out_finance_articles(y_articles)
    _log("투자/재무 + 가수다비치 제외 후", len(y_articles))

    # 4) 뉴스레터에서만 링크 기준 중복 제거
    y_articles = dedup_articles_by_link(y_articles)
    _log("중복 제거 후", len(y_articles))

    # 5) 요약 다듬기
    refine_article_summaries(y_articles)

    # 6) 카테고리 분류
    categorized = categorize_articles(y_articles)
    print("📦 카테고리별")
    print(f"  - ACUVUE: {len(categorized.acuvue)}")
    print(f"  - 업체별 활동(타사): {len(categorized.company)}")
    print(f"  - 제품 카테고리: {len(categorized.product)}")
    print(f"  - 업계 동향: {len(categorized.trend)}")
    print(f"  - 눈 건강/캠페인: {len(categorized.eye_health)}")

    # 7) 전체 브리핑 생성
    yesterday_summary = summarize_overall(y_articles)

    # 8) HTML 렌더링
    html_body = render_newsletter_html(cfg, categorized, yesterday_summary)

    # 9) 메일 발송
    email_conf = cfg["email"]
    subject_prefix = email_conf.get("subject_prefix", "[Daily News]")
    subject = f"{subject_prefix} 어제({yesterday.isoformat()}) 기사 브리핑"

    send_email_html(
        subject=subject,
        html_body=html_body,
        from_addr=email_conf["from"],
        to_addrs=email_conf["to"],
    )

    print("✅ 발송 완료")


if __name__ == "__main__":
    main()
