import datetime as dt
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

from scrapers import (
    load_config,
    fetch_all_articles,
    filter_yesterday_articles,
    filter_by_keywords,
)
from categorizer import categorize_articles
from summarizer import summarize_overall, refine_article_summaries
from mailer import send_email_html


def _log_counts(step: str, items):
    try:
        print(f"🧾 {step}: {len(items)}")
    except Exception:
        print(f"🧾 {step}: (count unknown)")


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


def _contains_excluded(text: str, excludes) -> bool:
    if not excludes:
        return False
    t = (text or "").lower()
    for x in excludes:
        s = str(x).strip().lower()
        if s and s in t:
            return True
    return False


def _apply_exclude_keywords(articles, excludes):
    if not excludes:
        return articles
    out = []
    for a in articles:
        text = f"{a.title} {a.summary or ''}".lower()
        if _contains_excluded(text, excludes):
            continue
        out.append(a)
    return out


def _select_best_by_priority(articles, cfg):
    """
    같은 링크가 여러 키워드로 잡힌 경우를 대비:
    - keywords_with_priority 기반으로 대표 기사 선택
    (이미 scrapers에서 링크 중복 제거를 하지만, 안전망으로 한번 더)
    """
    kwp = cfg.get("keywords_with_priority") or []
    pr_map = {}
    for it in kwp:
        if isinstance(it, dict) and it.get("keyword"):
            try:
                pr_map[str(it["keyword"]).lower()] = int(it.get("priority", 0))
            except Exception:
                pr_map[str(it["keyword"]).lower()] = 0

    def score(a):
        text = f"{a.title} {a.summary or ''}".lower()
        best = 0
        for k, p in pr_map.items():
            if k and k in text:
                best = max(best, p)
        return best

    best_by_link = {}
    for a in articles:
        link = a.link
        s = score(a)
        if link not in best_by_link or s > best_by_link[link][0]:
            best_by_link[link] = (s, a)

    return [v[1] for v in best_by_link.values()]


def _cap_sections(categorized, cfg):
    caps = cfg.get("max_articles_per_section", {}) or {}
    def cap(lst, n):
        try:
            n = int(n)
        except Exception:
            return lst
        return lst[:n] if n > 0 else lst

    categorized.acuvue = cap(categorized.acuvue, caps.get("acuvue"))
    categorized.company = cap(categorized.company, caps.get("company"))
    categorized.product = cap(categorized.product, caps.get("product"))
    categorized.trend = cap(categorized.trend, caps.get("trend"))
    categorized.eye_health = cap(categorized.eye_health, caps.get("eye_health"))
    return categorized


def main():
    cfg = load_config("config.yaml")

    print("🚀 뉴스레터 생성 시작")

    # 1) 전체 기사 수집
    all_articles = fetch_all_articles(cfg)
    _log_counts("전체 수집(원본)", all_articles)

    # 2) 최근 24시간 이내 기사만
    y_articles = filter_yesterday_articles(all_articles, cfg)
    _log_counts("최근 24시간", y_articles)

    # 3) 키워드 필터 적용
    y_kw_articles = filter_by_keywords(y_articles, cfg)
    _log_counts("키워드 필터 후", y_kw_articles)

    # 4) 추가 제외키워드 적용(재무/투자/실적 등 운영자 제어)
    excludes = cfg.get("exclude_keywords", []) or []
    y_kw_articles = _apply_exclude_keywords(y_kw_articles, excludes)
    _log_counts("exclude_keywords 적용 후", y_kw_articles)

    # 5) 대표 기사 선택(키워드 priority 기반)
    y_kw_articles = _select_best_by_priority(y_kw_articles, cfg)
    _log_counts("priority 대표 선정 후", y_kw_articles)

    # 6) 각 기사 요약을 GPT로 다듬기
    refine_article_summaries(y_kw_articles)

    # 7) 카테고리 분류
    categorized = categorize_articles(y_kw_articles)
    print("📦 카테고리별 수집 결과")
    print(f"  - ACUVUE: {len(categorized.acuvue)}")
    print(f"  - 업체별 활동(타사): {len(categorized.company)}")
    print(f"  - 제품 카테고리: {len(categorized.product)}")
    print(f"  - 업계 동향: {len(categorized.trend)}")
    print(f"  - 눈 건강/캠페인: {len(categorized.eye_health)}")

    # 8) 섹션별 상한 적용
    categorized = _cap_sections(categorized, cfg)

    # 9) 전체 브리핑 생성
    yesterday_summary = summarize_overall(y_kw_articles)

    # 10) HTML 렌더링
    html_body = render_newsletter_html(cfg, categorized, yesterday_summary)

    # 11) 메일 발송
    email_conf = cfg["email"]
    subject_prefix = email_conf.get("subject_prefix", "[Daily News]")

    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    now = dt.datetime.now(tz=tz)
    start_dt = now - dt.timedelta(hours=24)

    start = start_dt.strftime("%m/%d %H:%M")
    end = now.strftime("%m/%d %H:%M")
    subject = f"{subject_prefix} 최근 24시간 기사 브리핑 – {start}~{end}"

    send_email_html(
        subject=subject,
        html_body=html_body,
        from_addr=email_conf["from"],
        to_addrs=email_conf["to"],
    )

    print("✅ 발송 완료")


if __name__ == "__main__":
    main()&
