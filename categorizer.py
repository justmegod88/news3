from dataclasses import dataclass
from typing import List
from scrapers import Article


@dataclass
class CategorizedArticles:
    acuvue: List[Article]
    company: List[Article]      # 업체별 활동 (타사 렌즈 회사)
    product: List[Article]
    trend: List[Article]        # 업계 동향 (체인/매장 등)
    eye_health: List[Article]


# 업체별 활동(타사) → 콘택트렌즈 회사만
COMPANY_BRANDS = [
    "쿠퍼비전", "쿠퍼 비전", "쿠퍼", "쿠퍼 렌즈",
    "알콘", "알콘렌즈", "데일리스", "에어옵틱스",
    "바슈롬", "바슈롬렌즈",
    "인터로조", "클라렌",
]

# 제품 카테고리
PRODUCT_KEYWORDS = [
    "난시렌즈", "난시 렌즈",
    "멀티포컬렌즈", "멀티포컬 렌즈",
    "다초점 렌즈", "다초점렌즈",
   
]

# 업계 동향 → 체인 안경원, 매장, 시장, 협회/컨퍼런스 등
TREND_KEYWORDS = [
    "협회", "학회", "컨퍼런스", "포럼", "세미나", "박람회", "전시회",
    "체인", "체인점", "안경원", "매장", "출점", "오픈", "리뉴얼", 
    "시장 동향", "콘택트렌즈", "콘택트 렌즈",
    "소프트렌즈", "소프트 렌즈",
    "시력교정렌즈", "시력 교정 렌즈",
    # 대표 체인/브랜드명
    "다비치", "다비치 안경", "다비치 체인",
    "오렌즈", "렌즈미", 
    "으뜸50", "으뜸 플러스", "안경 체인",
    
]

# 눈 건강/캠페인
EYE_HEALTH_KEYWORDS = [
    "눈 건강", "안구건조", "근시", "원시", "난시", "블루라이트",
    "캠페인", "안질환", "시력 관리", "자외선", "눈관리", "눈 보호",
]


def contains_any(text: str, keywords: List[str]) -> bool:
    return any(k in text for k in keywords)


def categorize_articles(articles: List[Article]) -> CategorizedArticles:
    acuvue: List[Article] = []
    company: List[Article] = []
    product: List[Article] = []
    trend: List[Article] = []
    eye_health: List[Article] = []

    for a in articles:
        text = a.title + " " + a.summary

        # 1) ACUVUE 기사 (별도 섹션)
        if "아큐브" in text:
            acuvue.append(a)

        # 2) 업체별 활동 (타사 렌즈 회사만)
        #    아큐브는 제외하고, 쿠퍼/알콘/바슈롬/인터로조 계열만 포함
        if "아큐브" not in text and contains_any(text, COMPANY_BRANDS):
            company.append(a)

        # 3) 제품 카테고리별 (난시·멀티포컬·다초점 등)
        if contains_any(text, PRODUCT_KEYWORDS):
            product.append(a)

        # 4) 업계 동향 (협회/컨퍼런스/체인/안경 등)
        if contains_any(text, TREND_KEYWORDS):
            trend.append(a)

        # 5) 눈 건강·캠페인
        if contains_any(text, EYE_HEALTH_KEYWORDS):
            eye_health.append(a)

    return CategorizedArticles(
        acuvue=acuvue,
        company=company,
        product=product,
        trend=trend,
        eye_health=eye_health,
    )
