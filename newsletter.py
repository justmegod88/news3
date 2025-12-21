import datetime as dt
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo
import re

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_out_finance_articles,
)
from categorizer import categorize_articles
from summarizer import summarize_overall, refine_article_summaries
from mailer import send_email_html


def _log(step: str, n: int):
    print(f"🧾 {step}: {n}건")


def _normalize_link(url: str) -> str:
    """
    링크 중복 제거용 정규화:
    - query/fragment 제거
    - host 소문자
    - trailing slash 정리
    """
    if not url:
        return ""
    try:
        p = urlparse(url)
        netloc = (p.netloc or "").lower()
        path = p.path or ""
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return urlunparse((p.scheme, netloc, path, "", "", ""))
    except Exception:
        return url or ""


def _normalize_title(title: str) -> str:
    """
    (링크가 비어있을 때만) 최소한의 제목 정규화:
    - 공백 정리
    - 따옴표/괄호 정도만 제거
    """
    t = (title or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[“”\"'’`]", "", t)
    t = re.sub(r"[$begin:math:display$$end:math:display$$begin:math:text$$end:math:text$<>]", "", t)
    return t


def dedup_articles_only_duplicates(articles):
    """
    ✅ 목적: '중복만' 삭제하고 기사 수는 최대 유지
    1) 링크 정규화 기준으로 중복 제거 (원칙)
    2) 링크가 비어있거나 정규화가 실패한 경우에만:
       (제목 정규화 + 언론사 + 발행일(date))이 완전히 같을 때만 제거
    """
    out = []
    seen_links = set()
    seen_fallback = set()

    for a in articles:
        link_raw = getattr(a, "link", "") or ""
        link_key = _normalize_link(link_raw)

        # 1) 링크가 있으면 링크로만 중복 제거
        if link_key:
            if link_key in seen_links:
                continue
            seen_links.add(link_key)
            out.append(a)
            continue

        # 2) 링크가 없을 때만 매우 제한적으로 중복 제거
        title_key = _normalize_title(getattr(a, "title", "") or "")
        source_key = (getattr(a, "source", "") or "").strip().lower()

        try:
            pub_date = getattr(a, "published").date()
        except Exception:
            pub_date = None

        fb_key = (title_key, source_key, pub_date)
        if title_key and source_key and pub_date is not None:
            if fb_key in seen_fallback:
                continue
            seen_fallback.add(fb_key)

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

    today = dt.datetime.now(tz=tz).date()
    yesterday = today - dt.timedelta(days=1)

    print("🚀 뉴스레터 생성 시작")

    # 1) 수집 (최대한 많이)
    all_articles = fetch_all_articles(cfg)
    _log("전체 수집(원본)", len(all_articles))

    # 2) 어제 하루만
    y_articles = filter_yesterday_articles(all_articles, cfg)
    _log(f"어제({yesterday.isoformat()}) 기사 필터 후", len(y_articles))

    # 3) 투자/재무 + 가수 다비치 제외 (필터는 최소만)
    y_articles = filter_out_finance_articles(y_articles)
    _log("투자/재무 + 가수다비치 제외 후", len(y_articles))

    # 4) ✅ 중복만 삭제 (과하게 안 지움)
    y_articles = dedup_articles_only_duplicates(y_articles)
    _log("중복 제거 후", len(y_articles))

    # 5) 요약 다듬기
    refine_article_summaries(y_articles)

    # 6) 분류
    categorized = categorize_articles(y_articles)

    # 7) 전체 브리핑
    yesterday_summary = summarize_overall(y_articles)

    # 8) HTML
    html_body = render_newsletter_html(cfg, categorized, yesterday_summary)

    # 9) 발송
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
