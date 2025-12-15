import os
from typing import List

from scrapers import Article

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


def _get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def summarize_overall(articles: List[Article]) -> str:
    """어제 기사 전체를 2~3문장으로 브리핑 (임원 보고용 톤)"""
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    client = _get_client()
    if client is None:
        titles = "; ".join(a.title for a in articles[:5])
        return f"어제 총 {len(articles)}건의 관련 기사가 수집되었습니다. 주요 기사 제목은 다음과 같습니다: {titles}"

    bullet_lines = []
    for a in articles:
        bullet_lines.append(f"- [{a.source}] {a.title} / {a.summary[:120]}")
    joined = "\n".join(bullet_lines)

    prompt = (
        "너는 콘택트렌즈 브랜드 ACUVUE의 한국 법인 직원이다. "
        "다음은 어제자 콘택트렌즈 및 안경업계 관련 기사 목록이다. "
        "임원(사장 포함)이 아침에 한 번에 이해할 수 있도록, "
        "사실에 기반해 과장 없이 2~3문장으로만 정리해라. "
        "ACUVUE 관련 내용을 우선적으로 언급하고, "
        "경쟁사/체인 안경원/시장 동향과 눈 건강·캠페인 기사 중 "
        "ACUVUE가 전략적으로 참고할 만한 흐름만 간결하게 요약해라. "
        "기사 내용에 명시된 사실만 요약하고 절대 추측, 과장하지 마라."
        "기사가 이미자나 배너, 슬러건 설명 수준이라면 광고로 표시한다 (예: 난시엔, 아큐브 광고)."
        "말투는 한국어 존댓말로 정중하게 작성해라. "
        "개별 기사에 대한 평가는 하지 말고, 전체 흐름과 시사점만 짚어라.\n\n"
        f"어제 기사 목록:\n{joined}"
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=250,
    )

    return resp.choices[0].message.content.strip()


def refine_article_summaries(articles: List[Article]) -> None:
    """
    각 기사별로 GPT를 이용해 '요약'을 다시 작성.
    - 1~3문장
    - 제목 그대로 반복하지 않기
    - 언론사/기자/URL/매체명 언급 금지
    - 존댓말, 사실만 간결하게
    """
    if not articles:
        return

    client = _get_client()
    if client is None:
        # OpenAI 설정이 안 되어 있으면 기존 RSS 요약 그대로 사용
        return

    for a in articles:
        base_summary = a.summary or ""
        # 제목과 요약이 거의 같은 경우 대비
        if not base_summary or a.title.strip() in base_summary:
            base_summary = f"{a.title}. {base_summary}"


        prompt = (
            "다음은 콘택트렌즈/안경 업계 관련 기사입니다. "
            "제목과 RSS 요약을 바탕으로, 주어진 정보 안에서 사실만"
            "한국어 존댓말로 1~3문장 요약을 작성하세요.\n"
            "- 기사 내용에 명시된 사실만 요약하고 절대 추측, 과장, 전망, 의견, 평가, 해석하지 마세요.\n"
            "- 본문 요약이 이미지나 배너 슬러건 설명 수준이라면 광고로 표시한다.\n"
            "- 언론사 이름, 기자 이름, URL, 매체명(예: 신아일보, 빅데이터뉴스 등)은 절대 적지 마세요.\n"
            "- 불필요한 특수문자나 기호는 사용하지 말고, 자연스러운 문장으로만 써 주세요.\n\n"
            f"기사 제목: {a.title}\n\n"
            f"RSS 요약: {base_summary}\n"
        )


        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a concise Korean news summarizer."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=180,
            )
            new_summary = resp.choices[0].message.content.strip()
            if new_summary:
                a.summary = new_summary
        except Exception as e:
            # 에러가 나면 그냥 기존 요약 유지
            print(f"[WARN] article summary refine failed: {e}")
