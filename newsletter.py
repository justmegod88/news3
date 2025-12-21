import yaml
from datetime import datetime, timedelta
from scrapers import fetch_from_google_news, fetch_from_naver_news
from categorizer import categorize_article
from summarizer import summarize_article, summarize_yesterday
from mailer import send_email

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def contains_excluded(text, excludes):
    text = text.lower()
    return any(x.lower() in text for x in excludes)

def main():
    cfg = load_config()
    all_articles = []

    print("🔍 기사 수집 시작")

    for k in cfg["keywords_with_priority"]:
        keyword = k["keyword"]
        priority = k["priority"]

        g_articles = fetch_from_google_news(keyword)
        n_articles = fetch_from_naver_news(
            keyword,
            pages=cfg["naver_pages"]
        )

        for a in g_articles + n_articles:
            a["priority"] = priority
            a["keyword"] = keyword
            all_articles.append(a)

    print(f"📥 총 수집 기사 수: {len(all_articles)}")

    # 제외 키워드 필터
    filtered = [
        a for a in all_articles
        if not contains_excluded(
            a["title"] + a.get("content", ""),
            cfg["exclude_keywords"]
        )
    ]
    print(f"🧹 제외 키워드 필터 후: {len(filtered)}")

    # URL 기준 중복 제거 + priority 높은 것 유지
    unique = {}
    for a in filtered:
        key = a["link"]
        if key not in unique or a["priority"] > unique[key]["priority"]:
            unique[key] = a

    articles = list(unique.values())
    print(f"🔁 중복 제거 후: {len(articles)}")

    # 분류
    categorized = categorize_article(articles)
    print("📂 카테고리별 수:")
    for k, v in categorized.items():
        print(f"  - {k}: {len(v)}")

    # 요약
    for group in categorized.values():
        for a in group:
            a["summary"] = summarize_article(a)

    yesterday_summary = summarize_yesterday(articles)

    send_email(categorized, yesterday_summary, cfg)

if __name__ == "__main__":
    main()
