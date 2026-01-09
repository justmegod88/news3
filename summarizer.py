import re
import difflib
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


def _fallback_summary(articles: List, max_chars: int = 340) -> str:
    """OpenAI 미사용 시: 과장/총평 없이 '팩트(타이틀)'만 간단 나열."""
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    items = []
    for a in articles[:4]:
        t = (getattr(a, "title", "") or "").strip()
        src = (getattr(a, "source", "") or "").strip()
        if not t:
            continue
        items.append(f"- {t}" + (f" ({src})" if src else ""))

    if not items:
        return "어제 기준으로 수집된 관련 기사가 있어 확인이 필요합니다."

    out = "어제 확인된 주요 기사(타이틀 기준):\n" + "\n".join(items)
    return out[:max_chars]

def _importance_score(title: str, summary: str) -> int:
    text = f"{title} {summary}".lower()
    score = 0

    # 규제/리콜/안전/허가/소송 등 '임원 보고' 우선
    priority_terms = [
        "리콜", "회수", "위해", "부작용", "안전", "경고", "규제", "제재", "과징금",
        "식약처", "fda", "허가", "승인", "임상", "임상시험", "논문",
        "소송", "합의", "위반", "개정", "정책", "가이드라인",
        "인수", "합병", "파트너십", "계약", "출시", "런칭", "신제품", "단종",
    ]
    for k in priority_terms:
        if k in text:
            score += 2

    # 핵심 브랜드/경쟁사 언급은 가중
    brand_terms = ["acuvue", "아큐브", "존슨앤드존슨", "알콘", "쿠퍼비전", "바슈롬", "인터로조", "클라렌"]
    for k in brand_terms:
        if k.lower() in text:
            score += 1

    return score


def summarize_overall(articles: List) -> str:
    """
    ✅ 임원용 '어제 기사 AI 브리핑' (요청 반영)

    - 3~4문장
    - **팩트만**, 과장/추측/총평 금지
    - 기사 수가 많으면 중요 기사 위주로만 (단, 선택 후 **원래 순서 유지**)
    - 이미지 파일로 바로 연결되는 단순 광고(is_image_ad=True)는 제외
    """
    # 0) 이미지 광고 제외
    clean = [a for a in (articles or []) if not getattr(a, "is_image_ad", False)]

    if not clean:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    # 1) 너무 많으면 중요 기사만 선택(원래 순서 유지)
    if len(clean) > 12:
        scored = []
        for idx, a in enumerate(clean):
            t = getattr(a, "title", "") or ""
            s = getattr(a, "summary", "") or ""
            scored.append((idx, _importance_score(t, s)))
        # 점수 상위 8개 선택 (동점이면 먼저 나온 기사 우선)
        top = sorted(scored, key=lambda x: (-x[1], x[0]))[:8]
        keep_idx = set(i for i, _ in top)
        clean = [a for i, a in enumerate(clean) if i in keep_idx]

    client = _get_client()
    if client is None:
        return _fallback_summary(clean)

    # 2) 기사 텍스트 구성(너무 길어지지 않게 상위 n개만)
    items = []
    for a in clean[:8]:
        t = getattr(a, "title", "") or ""
        s = getattr(a, "summary", "") or ""
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 260:
            s = s[:260] + "…"
        # summary가 비어있으면 타이틀만
        if s:
            items.append(f"- {t} :: {s}")
        else:
            items.append(f"- {t}")

    content = "\n".join(items)

    prompt = f"""
너는 콘택트렌즈/안경 업계 데일리 뉴스레터를 임원에게 보고하는 비서다.
아래 '기사 목록'에 적힌 내용만 근거로, 임원 보고용으로 3~4문장 브리핑을 작성해라.

필수 규칙:
- 과장/추측/의견/총평 금지 ("~로 판단된다" 같은 표현 금지)
- 기사에 적힌 사실만 간결히.
- 문장마다 서로 다른 핵심 포인트 1개씩 (중복 금지).
- 불필요한 수식어(매우/큰/획기적 등) 금지.
- 3~4문장, 최대 420자 이내.

기사 목록:
{content}
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "너는 사실 기반으로만 요약하는 한국어 비서다."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        # 혹시 모델이 문장 수를 초과하면 간단히 컷
        if len(text) > 520:
            text = text[:520].rstrip() + "…"
        return text or _fallback_summary(clean)
    except Exception:
        return _fallback_summary(clean)

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
- 2~3문장, 사실 중심
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
    ✅ 기사별 summary 처리 규칙 (요청 반영)

    - RSS/검색 결과에서 가져온 summary를 **임의로 늘리거나 '지어서' 확장하지 않음**
      → 짧으면 짧은대로 그대로 노출
    - 공백/HTML 흔적 정도만 정리
    - 너무 길면(메일 폭) 220자만 컷
    - 링크가 이미지(광고 배너 등)로 바로 떨어지는 경우(is_image_ad=True)는
      summary를 굳이 만들지 않고 타이틀 중심으로 노출(현재 summary가 있으면 그대로 유지)
    """
    for a in articles:
        summary = getattr(a, "summary", "") or ""

        # 이미지 광고는 summary 생성/재작성 하지 않음
        if getattr(a, "is_image_ad", False):
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 220:
                summary = summary[:220].rstrip() + "…"
            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # 일반 기사: summary 원문 유지(확장 금지), 정리만
        summary = re.sub(r"\s+", " ", summary).strip()

        if len(summary) > 220:
            summary = summary[:220].rstrip() + "…"

        try:
            a.summary = summary
        except Exception:
            pass
