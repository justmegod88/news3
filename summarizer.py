import re
import difflib
from typing import List
from urllib.parse import urlparse  # ✅ 추가: 이미지 확장자 판별용

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
- 과장/추측 금지, 기사에 명시된 사실만 사용
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


# =========================
# ✅ 제목/요약 겹침 방지용 (핵심 개선)
# =========================
def _norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    return s


def _is_too_similar(title: str, summary: str, threshold: float = 0.78) -> bool:
    t = _norm_text(title)
    s = _norm_text(summary)

    if not t or not s:
        return True

    # summary가 title을 포함/역포함하거나 너무 짧으면 재작성
    if t in s or s in t:
        return True
    if len(s) < 60:
        return True

    ratio = difflib.SequenceMatcher(None, t, s).ratio()
    return ratio >= threshold


def _rewrite_summary(client, title: str, raw_summary: str) -> str:
    title = _norm_text(title)
    raw_summary = _norm_text(raw_summary)

    prompt = f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [제목]과 [원문요약]을 바탕으로, 뉴스레터에 넣을 '2~3문장 요약'을 작성해라.

규칙:
- 제목 문구를 그대로 반복하지 말고, 다른 표현으로 바꿔 쓸 것
- 2~3문장, 사실만,
- 기사 '출처(언론사)'는 제품, 브랜드, 제조사로 표현하지 말 것
- 안경테,렌즈, 제품의 브랜드명은 기사 본문에 명확히 언급된 경우에만 사용
- 브랜드가 불명확한 경우 특정 주체를 단정하지 말것
- 기사에 없는 단어 절대 사용 금지
- 과장/추측 금지, 홍보 문구 금지
- 220자 이내(가능하면 160~200자)

[제목]
{title}

[원문요약]
{raw_summary}

[출력]
""".strip()

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = (r.choices[0].message.content or "").strip()
    text = re.sub(r"\s+\n", "\n", text).strip()
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > 220:
        text = text[:220].rstrip() + "…"
    return text


def refine_article_summaries(articles: List) -> None:
    """
    ✅ 기사별 summary를 뉴스레터용으로 다듬기
    - 기본: 길이 컷(220자)
    - 개선: summary가 title과 너무 비슷하면(OpenAI 가능 시) 2~3문장으로 재작성

    ✅ (이번 수정) 링크가 이미지 파일(jpg/png 등)로 끝나면:
    - summary를 제목(title)과 동일하게 고정
    - OpenAI 재작성/가공 없이 그대로 노출
    """
    client = _get_client()

    for a in articles:
        title = getattr(a, "title", "") or ""
        summary = getattr(a, "summary", "") or ""
        link = getattr(a, "link", "") or ""

        title = _norm_text(title)
        summary = _norm_text(summary)

        # ✅ 핵심: 링크가 이미지 파일이면 summary는 제목 그대로
        path = urlparse(link).path.lower()
        if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            try:
                a.summary = title
            except Exception:
                pass
            continue

        if client is not None and _is_too_similar(title, summary):
            try:
                summary = _rewrite_summary(client, title, summary)
            except Exception:
                pass  # 실패하면 기존 summary 유지

        # 최종 길이 컷
        if len(summary) > 220:
            summary = summary[:220].rstrip() + "…"

        try:
            a.summary = summary
        except Exception:
            pass
