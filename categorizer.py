from dataclasses import dataclass
from typing import List
import re

from scrapers import Article


@dataclass
class CategorizedArticles:
    acuvue: List[Article]
    company: List[Article]      # 업체별 활동 (타사 렌즈 회사)
    product: List[Article]      # 제품 카테고리
    trend: List[Article]        # 업계 동향 (체인/매장 등)
    eye_health: List[Article]   # 눈 건강/캠페인


# =========================
# Keyword dictionaries
# =========================
ACUVUE = [
    "아큐브", "acuvue",
    "존슨앤드존슨 비전", "한국존슨앤드존슨 비전",
    "johnson", "j&j", "jnj", "johnson & johnson vision",
]

# 업체별 활동(타사) → 콘택트렌즈 회사만
COMPANY_BRANDS = [
    "쿠퍼비전", "쿠퍼 비전", "쿠퍼", "쿠퍼 렌즈", "쿠퍼비젼","coopervision","cooper vision",
    "알콘", "알콘렌즈", "알콘 렌즈", "데일리스토탈원", "에어옵틱스","알콘 콘택트렌즈","alcon",
    "바슈롬", "바슈롬렌즈","바슈롬 콘택트렌즈",
    "인터로조","미광","미광 콘택트렌즈",
    # 필요하면 추가
]

# 제품 카테고리
PRODUCT_KEYWORDS = [
    "난시렌즈", "난시 렌즈", "난시용 콘택트렌즈",
    "멀티포컬렌즈", "멀티포컬 렌즈","노안콘택트렌즈", "노안 콘택트렌즈", "노안 교정용 콘택트렌즈",
    "다초점콘택트렌즈", "다초점 콘택트렌즈",
    "실리콘하이드로겔","실리콘 콘택트렌즈",
]

# 업계 동향 → 체인 안경원, 매장, 시장, 협회/컨퍼런스 등
TREND_KEYWORDS = [
    "협회", "학회", "컨퍼런스", "포럼", "세미나", "박람회", "전시회",
    "체인", "체인점", "안경원", "매장", "출점", "오픈", "리뉴얼",
    "시장 동향","누진 렌즈","돋보기","안경광학과",
    
    # 대표 체인/브랜드명
    "다비치", "다비치 안경", "다비치 체인",
    "렌즈미", "오렌즈", "안경체인",
    "으뜸 50", "으뜸50", "으뜸 플러스", "으뜸플러스",
    "안경 진정성", "안경진정성",
    "하파크리스틴", "윙크렌즈",
    "호야", "자이스",
]

# 눈 건강/캠페인
EYE_HEALTH_KEYWORDS = [
    "눈 건강", "눈건강",
    "노안", "백내장", "라식", "스마일수술","라섹",
    "눈 건강 캠페인", "눈 피로",
    "난시", "원시", "근시", "시력저하",
]


# =========================
# Helpers
# =========================
def _normalize(text: str) -> str:
    """
    - 소문자화
    - 공백/특수문자 정리
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def contains_any(text: str, keywords: List[str]) -> bool:
    t = _normalize(text)
    for k in keywords:
        if _normalize(k) in t:
            return True
    return False


# =========================
# Categorizer (중복 방지: 한 기사=한 카테고리)
# 우선순위: ACUVUE → COMPANY → PRODUCT → TREND → EYE_HEALTH
# =========================
def categorize_articles(articles: List[Article]) -> CategorizedArticles:
    acuvue: List[Article] = []
    company: List[Article] = []
    product: List[Article] = []
    trend: List[Article] = []
    eye_health: List[Article] = []

    for a in articles:
        text = f"{a.title} {a.summary}"

        # 1) ACUVUE (있으면 최우선)
        if contains_any(text, ACUVUE):
            acuvue.append(a)

        # 2) 타사 렌즈 회사(아큐브 제외)
        elif (not contains_any(text, ACUVUE)) and contains_any(text, COMPANY_BRANDS):
            company.append(a)

        # 3) 제품 카테고리
        elif contains_any(text, PRODUCT_KEYWORDS):
            product.append(a)

        # 4) 업계 동향
        elif contains_any(text, TREND_KEYWORDS):
            trend.append(a)

        # 5) 눈 건강/캠페인
        elif contains_any(text, EYE_HEALTH_KEYWORDS):
            eye_health.append(a)

        # 어디에도 안 걸리면 버림(원하면 trend로 보내도 됨)

    return CategorizedArticles(
        acuvue=acuvue,
        company=company,
        product=product,
        trend=trend,
        eye_health=eye_health,
    )
