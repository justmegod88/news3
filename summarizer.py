# summarizer.py
# ------------------------------------------------------------
# ✅ 목적
# - "어제 기사 AI 브리핑" 3~4문장 요약 (사실 기반, 과장/추측/창작 금지)
# - 기사별 summary를 뉴스레터용으로 다듬기
# - (핵심 개선)
#   1) 광고/지면 이미지형(is_image_ad=True)인 경우: 요약 생성 금지 → 제목 그대로
#   2) 전체 AI 브리핑 입력에서 광고/지면 이미지형 자동 제외
#   3) 전체 AI 브리핑 입력에서 '안경' 키워드 포함 기사 제외(요청 반영)
# ------------------------------------------------------------

import re
import difflib
from typing import List, Optional, Dict, Tuple

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def _get_client() -> Optional["OpenAI"]:
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def _norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    return s


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _get_article_text(a) -> str:
    """
    ✅ 기사 전문 텍스트를 우선 사용:
    - content/body/text/fulltext/article_text 순으로 탐색
    - 없으면 summary 사용
    """
    for key in ("content", "body", "text", "fulltext", "article_text"):
        v = getattr(a, key, None)
        if isinstance(v, str) and v.strip():
            return _norm_text(v)

    return _norm_text(getattr(a, "summary", "") or "")


# ------------------------------------------------------------
# Overall (3~4 sentences)
# ------------------------------------------------------------
TOPIC_PATTERNS: List[Tuple[str, List[str]]] = [
    ("AI·디지털", [
        "ai", "인공지능", "머신러닝", "딥러닝", "알고리즘", "데이터", "빅데이터",
        "디지털", "앱", "플랫폼", "자동화", "ar", "vr", "스마트", "웨어러블",
    ]),
    ("AI 안경·스마트글라스", [
        "ai 안경", "스마트 안경", "스마트글라스", "smart glasses",
        "메타 레이밴", "ray-ban meta", "rayban meta", "meta",
    ]),
    ("콘택트렌즈", [
        "콘택트렌즈", "콘택트 렌즈", "렌즈", "소프트렌즈", "원데이", "2주", "한달",
        "난시", "토릭", "멀티포컬", "근시", "도수", "착용", "피팅",
    ]),
    ("안과·눈건강", [
        "안과", "시력", "굴절", "눈 건강", "안질환", "건조", "각막",
    ]),
    ("유통·매장·업계", [
        "안경원", "매장", "체인", "유통", "판매", "시장", "업계", "협회", "전시",
    ]),
    ("규제·정책", [
        "규제", "정책", "법", "식약처", "허가", "인증", "가이드라인", "표준",
    ]),
]


def _detect_topics(text: str) -> List[str]:
    t = (text or "").lower()
    found = []
    for label, kws in TOPIC_PATTERNS:
        for kw in kws:
            if kw.lower() in t:
                found.append(label)
                break
    return found


def _topic_summary_line(topic: str, count: int) -> str:
    if count >= 4:
        return f"{topic} 관련 이슈가 다수 기사에서 반복 언급됐습니다."
    if count >= 2:
        return f"{topic} 관련 기사들이 여러 건 있었습니다."
    return ""


def _fallback_overall(articles: List, max_chars: int = 340) -> str:
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    titles = [_norm_text(getattr(a, "title", "") or "") for a in articles[:4]]
    titles = [t for t in titles if t]
    if not titles:
        return "어제 기준으로 수집된 관련 기사가 있어 확인이 필요합니다."

    out = f"어제 주요 기사: {' / '.join(titles[:3])}"
    return _trim(out, max_chars)


def _contains_glasses_keyword(a) -> bool:
    # ✅ 요청: AI 요약에서 '안경' 들어간 키워드 기사는 제외
    needles = ["안경"]
    hay = " ".join([
        str(getattr(a, "title", "") or ""),
        str(getattr(a, "summary", "") or ""),
        str(getattr(a, "content", "") or ""),
        str(getattr(a, "body", "") or ""),
        str(getattr(a, "text", "") or ""),
        str(getattr(a, "fulltext", "") or ""),
        str(getattr(a, "article_text", "") or ""),
    ])
    return any(n in hay for n in needles)


def summarize_overall(articles: List) -> str:
    """
    ✅ 임원용 '어제 기사 AI 브리핑' (3~4문장)
    - 제공된 기사 텍스트만 근거
    - 광고/지면 이미지형(is_image_ad=True) 자동 제외
    - '안경' 키워드 포함 기사 자동 제외(요청)
    """
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    # ✅ 광고/이미지형 & '안경' 포함 기사 제외
    cleaned = []
    for a in articles:
        if getattr(a, "is_image_ad", False):
            continue
        if _contains_glasses_keyword(a):
            continue
        cleaned.append(a)

    if not cleaned:
        return "어제 기준으로 공유 가능한 텍스트 기사(광고/이미지형 및 '안경' 키워드 제외)가 없어 별도 공유 사항은 없습니다."

    client = _get_client()
    if client is None:
        # OpenAI 없이도 최소 요약(제목/주제 기반)
        topics_count: Dict[str, int] = {}
        for a in cleaned:
            txt = " ".join([getattr(a, "title", "") or "", getattr(a, "summary", "") or "", getattr(a, "content", "") or ""])
            for tp in _detect_topics(txt):
                topics_count[tp] = topics_count.get(tp, 0) + 1

        topic_lines = []
        for tp, c in sorted(topics_count.items(), key=lambda x: -x[1]):
            line = _topic_summary_line(tp, c)
            if line:
                topic_lines.append(line)
        topic_lines = topic_lines[:1]

        top_titles = [_norm_text(getattr(a, "title", "") or "") for a in cleaned[:3]]
        top_titles = [t for t in top_titles if t]

        base = []
        if top_titles:
            base.append(f"상단 기사 중심으로 보면, '{top_titles[0]}' 등 주요 이슈가 확인됩니다.")
        if topic_lines:
            base.append(topic_lines[0])
        base.append("자세한 내용은 각 기사 원문을 확인해 주세요.")

        out = " ".join([s for s in base if s])
        return _trim(out, 340)

    # OpenAI 사용: 상단 기사일수록 Priority 높게
    items = []
    topics_count: Dict[str, int] = {}

    for idx, a in enumerate(cleaned[:10], start=1):
        title = _norm_text(getattr(a, "title", "") or "")
        source = _norm_text(getattr(a, "source", "") or "")
        text = _get_article_text(a)
        text = _trim(text, 900)

        for tp in _detect_topics(f"{title} {text}"):
            topics_count[tp] = topics_count.get(tp, 0) + 1

        items.append(
            f"[Priority {idx}]\n"
            f"Title: {title}\n"
            f"Source: {source}\n"
            f"Text: {text}\n"
        )

    trend_hints = []
    for tp, c in sorted(topics_count.items(), key=lambda x: -x[1]):
        line = _topic_summary_line(tp, c)
        if line:
            trend_hints.append(line)
    trend_hints = trend_hints[:2]

    content = "\n".join(items)
    trend_block = "\n".join([f"- {t}" for t in trend_hints]) if trend_hints else "- (특정 주제 반복 언급은 입력 기사에서 두드러지지 않음)"

    system = (
        "너는 콘택트렌즈/안경 업계 데일리 뉴스레터를 임원에게 보고하는 비서다. "
        "반드시 제공된 텍스트만 근거로 요약한다."
    )

    user = f"""
아래 기사 텍스트만 근거로, 임원 보고용으로 3~4문장으로 요약해줘.

필수 규칙(매우 중요):
1) 절대 과장/추측/창작 금지. 텍스트에 없는 정보는 쓰지 말 것.
2) 숫자/비율/성과 등은 텍스트에 명시된 경우에만 사용.
3) 문장형으로만 작성(불릿/번호/따옴표/제목 나열 금지).
4) Priority 숫자가 작은 기사(위쪽)가 더 중요하다. 중요한 내용은 앞쪽 문장에 배치.
5) 기사량이 많거나 유사 주제가 반복되면, 아래 힌트를 참고해
   "관련 기사가 여러 건 있었다/다수 있었다" 수준으로만 언급(정확한 건수 단정 금지).

반복 주제 힌트(참고용):
{trend_block}

[기사 텍스트]
{content}
""".strip()

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=220,
        )
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"\s+\n", "\n", text).strip()
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > 340:
            text = text[:340].rstrip() + "…"
        return text or _fallback_overall(cleaned)
    except Exception:
        return _fallback_overall(cleaned)


# ------------------------------------------------------------
# Per-article summary refine
# ------------------------------------------------------------
def _is_too_similar(title: str, summary: str, threshold: float = 0.78, min_len: int = 75) -> bool:
    t = _norm_text(title)
    s = _norm_text(summary)

    if not t and not s:
        return False

    if not s or len(s) < min_len:
        return True

    if not t:
        return False

    if t in s or s in t:
        return True

    ratio = difflib.SequenceMatcher(None, t, s).ratio()
    return ratio >= threshold


def _rewrite_summary(client, title: str, raw_text: str) -> str:
    title = _norm_text(title)
    raw_text = _norm_text(raw_text)

    system = "너는 업계 데일리 뉴스레터 편집자다. 제공된 텍스트만 근거로 요약한다."
    user = f"""
아래 [제목]과 [기사텍스트]만 근거로, 뉴스레터에 넣을 2~3문장 요약을 작성해라.

규칙:
- 제목 문구를 그대로 반복하지 말고 다른 표현으로 바꿔 쓸 것
- 2~3문장, 사실 중심
- 과장/추측/홍보 문구 금지
- 텍스트에 없는 내용은 쓰지 말 것
- 220자 이내(가능하면 160~200자)
- 문장형으로만(불릿/번호/따옴표 금지)

[제목]
{title}

[기사텍스트]
{_trim(raw_text, 1200)}
""".strip()

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=180,
    )
    text = (r.choices[0].message.content or "").strip()
    text = re.sub(r"\s+\n", "\n", text).strip()
    text = re.sub(r"\s+", " ", text).strip()

    return _trim(text, 220)


def refine_article_summaries(articles: List) -> None:
    """
    ✅ 기사별 summary를 뉴스레터용으로 다듬기
    - 기본: 220자 컷
    - summary가 title과 너무 비슷/너무 짧으면(OpenAI 가능 시) 2~3문장으로 재작성
    - ✅ 광고/지면 이미지형(is_image_ad=True)이면 요약 생성 금지 → 제목 그대로
    """
    client = _get_client()

    for a in articles:
        title = _norm_text(getattr(a, "title", "") or "")
        summary = _norm_text(getattr(a, "summary", "") or "")

        # ✅ 핵심: 광고/지면 이미지형은 "요약하지 말고 제목 그대로"
        if getattr(a, "is_image_ad", False):
            fixed = title or summary or ""
            fixed = _trim(_norm_text(fixed), 220)
            try:
                a.summary = fixed
            except Exception:
                pass
            continue

        if client is not None and _is_too_similar(title, summary):
            try:
                raw_text = _get_article_text(a)
                summary = _rewrite_summary(client, title, raw_text)
            except Exception:
                pass

        summary = _trim(_norm_text(summary), 220)

        try:
            a.summary = summary
        except Exception:
            pass
