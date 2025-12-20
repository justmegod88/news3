import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import re

def normalize_url(url):
    """utm 파라미터 제거"""
    parsed = urlparse(url)
    clean = parsed._replace(query="")
    return urlunparse(clean)

def fetch_from_google_news(keyword):
    rss_url = (
        "https://news.google.com/rss/search?"
        f"q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
    )
    feed = feedparser.parse(rss_url)
    articles = []

    for e in feed.entries:
        articles.append({
            "title": e.title,
            "link": normalize_url(e.link),
            "published": getattr(e, "published_parsed", None),
            "source": e.source.title if hasattr(e, "source") else "Google News",
            "content": e.summary if hasattr(e, "summary") else ""
        })

    return articles

def fetch_from_naver_news(keyword, pages=5):
    articles = []

    for page in range(1, pages + 1):
        start = (page - 1) * 10 + 1
        url = (
            "https://search.naver.com/search.naver?"
            f"where=news&query={keyword}&start={start}"
        )
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, "html.parser")

        for item in soup.select(".news_tit"):
            link = normalize_url(item["href"])
            articles.append({
                "title": item["title"],
                "link": link,
                "published": None,  # ❗ 파싱 실패해도 버리지 않음
                "source": "NaverNews",
                "content": ""
            })

    return articles

