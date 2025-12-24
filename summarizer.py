import re
from typing import List

# OpenAI 사용은 선택(없어도 동작)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def _get_client():
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def _fallback_summary(articles: List, max_chars: int = 320) -> str:
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    titles = [getattr(a, "title", "") for a in articles][:3]
    titles = [t.strip() for t in titles if t and t.strip()]
    if not titles:
        return "어제 기준으로 수집된 관련 기사가 있어 확인이 필요합니다."

    txt = " / ".join(titles)
    out = f"어제 주요 기사: {txt}"
    return out[:max_chars]


def summarize_overall(articles: List) -> str:
    """
    ✅ 임원용 '어제 기사 AI 브리핑'
    - 입력된 기사 리스트만 요약 (newsletter.py에서 1/2/3 중 하나만 넣도록 제어)
    - 최대 3~4문장, 너무 길면 컷
    """
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    client = _get_client()
    if client is None:
        return _fallback_summary(articles)

    # 기사 텍스트 구성(너무 길어지지 않게 상위 몇 개만)
    items = []
    for a in articles[:8]:
        t = getattr(a, "title", "") or ""
        s = getattr(a, "summary", "") or ""
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 240:
            s = s[:240] + "…"
        items.append(f"- {t} :: {s}")

    content = "\n".join(items)

    prompt = f"""
너는 콘택트렌즈/안경 업계 데일리 뉴스레터를 임원에게 보고하는 비서야.
아래 기사들만 근거로, 3~4문장으로 짧게 브리핑해줘.
규칙:
- 과장/추측 금지, 기사에 있는 내용만.
- 너무 길면 300자 내로 자연스럽게 줄여.
- 쉼표로 길게 늘어놓지 말고 문장 3~4개로.

[기사 목록]
{content}
""".strip()

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"\s+\n", "\n", text).strip()
        # 최종 길이 컷(메일 UI 안정)
        if len(text) > 340:
            text = text[:340].rstrip() + "…"
        return text or _fallback_summary(articles)
    except Exception:
        return _fallback_summary(articles)


def refine_article_summaries(articles: List) -> None:
    """
    기사별 summary가 너무 길면 UI가 무너지니 1차적으로 컷.
    (OpenAI 없어도 동작)
    """
    for a in articles:
        s = getattr(a, "summary", "") or ""
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 220:
            s = s[:220].rstrip() + "…"
        try:
            a.summary = s
        except Exception:
            pass
