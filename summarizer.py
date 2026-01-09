# summarizer.py
# ------------------------------------------------------------
# ✅ 목적
# - "어제 기사 AI 브리핑"을 패턴 뻔함 없이 자연스럽게 3~4문장 요약
# - 절대 과장/추측/창작 금지: 입력 텍스트 안의 사실만 사용
# - 기사 많을 때: 상단(먼저 들어온/중요 카테고리) 기사에 가중치 부여
# - 특정 주제가 반복되면 "관련 기사가 여러 건 있었다" 수준으로만 언급(숫자 단정 금지)
# - ✅ 요청 반영: AI 브리핑에서는 '안경' 키워드 포함 기사를 제외(기사 리스트 자체는 유지)
# - ✅ 광고/이미지형(본문 텍스트가 빈약) 기사면 AI가 내용을 만들지 못하게,
#   기사별 summary는 '제목/원문요약 그대로(또는 제목 수준)'로 처리
# ------------------------------------------------------------

import re
import difflib
from typing import List, Optional, Dict, Tuple

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# -------------------------
# OpenAI Client
# -------------------------
def _get_client() -> Optional["OpenAI"]:
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


# -------------------------
# Utils
# -------------------------
def _norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    return s


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _get_article_text(a) -> str:
    """
    ✅ '실제 기사 내용' 기반 요약을 위해 가능한 긴 텍스트를 우선 사용
    파이프라인에서 fulltext/content를 넣어두면 그걸 사용.
    없으면 RSS summary 사용(이 경우 원문이 짧으면 한계가 있음)
    """
    for key in ("content", "body", "text", "fulltext", "article_text"):
        v = getattr(a, key, None)
        if isinstance(v, str) and v.strip():
            return _norm_text(v)

    v = getattr(a, "summary", "") or ""
    return _norm_text(v)


def _hangul_ratio(s: str) -> float:
    if not s:
        return 0.0
    hangul = len(re.findall(r"[가-힣]", s))
    return hangul / max(1, len(s))


def _is_low_info_content(text: str) -> bool:
    """
    광고/이미지형/텍스트 거의 없는 기사 판단용(가드레일)
    - 텍스트가 너무 짧거나
    - 한글/문장 정보가 거의 없거나
    - 이미지 파일 확장자/광고성 문구가 과다하면 low-info로 간주
    """
    t = (text or "").strip()
    if len(t) < 120:
        return True

    # 한글 비율이 너무 낮으면(이미지/숫자/짧은 캡션 위주) 요약 위험
    if _hangul_ratio(t) < 0.08 and len(t) < 400:
        return True

    # 이미지/광고 흔적
    ad_signals = [
        "전면광고", "광고", "AD", "홍보", "프로모션", "이벤트", "증정", "사은행사",
        "쿠폰", "할인", "포스터", "모집기간", "QR", "qr", "scan",
    ]
    if any(sig.lower() in t.lower() for sig in ad_signals) and len(t) < 500:
        return True

    # 이미지 파일 확장자 흔적이 많으면 텍스트가 빈약할 확률↑
    img_hits = len(re.findall(r"\.(jpg|jpeg|png|gif|webp)\b", t.lower()))
    if img_hits >= 2 and len(t) < 700:
        return True

    return False


def _fallback_overall(articles: List, max_chars: int = 340) -> str:
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."
    titles = [_norm_text(getattr(a, "title", "") or "") for a in articles[:4]]
    titles = [t for t in titles if t]
    if not titles:
        return "어제 기준으로 수집된 관련 기사가 있어 확인이 필요합니다."
    out = f"어제 주요 기사: {' / '.join(titles[:3])}"
    return _trim(out, max_chars)


# -------------------------
# Topic grouping (lightweight)
# -------------------------
TOPIC_PATTERNS: List[Tuple[str, List[str]]] = [
    ("AI·디지털", [
        "ai", "인공지능", "머신러닝", "딥러닝", "알고리즘", "데이터", "빅데이터",
        "디지털", "앱", "플랫폼", "자동화", "ar", "vr", "스마트", "웨어러블",
    ]),
    ("AI 안경·스마트글라스", [
        "ai 안경", "스마트 안경", "스마트글라스", "smart glasses", "glasses with ai",
        "메타 레이밴", "ray-ban meta", "rayban meta", "메타", "meta",
    ]),
    ("콘택트렌즈", [
        "콘택트렌즈", "콘택트 렌즈", "렌즈", "소프트렌즈", "원데이", "2주", "한달",
        "난시", "토릭", "멀티포컬", "근시", "도수", "착용", "피팅",
    ]),
    ("안경·검안·안과", [
        "안경", "검안", "안과", "시력", "굴절", "눈 건강", "안질환", "건조", "각막",
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


# -------------------------
# AI Overall brief (3~4 sentences)
# -------------------------
AI_BRIEF_EXCLUDE_KEYWORDS = ["안경"]  # ✅ 요청: AI 브리핑에서만 제외


def _should_exclude_from_ai_brief(a) -> bool:
    hay = " ".join([
        getattr(a, "title", "") or "",
        getattr(a, "summary", "") or "",
        _get_article_text(a) or "",
    ])
    hay = _norm_text(hay)
    return any(k in hay for k in AI_BRIEF_EXCLUDE_KEYWORDS)


def summarize_overall(articles: List) -> str:
    """
    ✅ 임원용 '어제 기사 AI 브리핑' (3~4문장)
    - 기사 텍스트만 근거
    - 상단 기사일수록 중요
    - ✅ AI 브리핑에서만 '안경' 포함 기사는 제외
    - ✅ 본문 빈약/광고형이면 모델이 추측하지 않도록 입력에 힌트 제공
    """
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    # ✅ AI 브리핑에서만 필터
    filtered = [a for a in articles if not _should_exclude_from_ai_brief(a)]
    if not filtered:
        return "어제는 '안경' 관련 키워드를 제외하면, 요약에 반영할 만한 기사가 충분히 수집되지 않았습니다."

    client = _get_client()
    if client is None:
        # OpenAI 없으면: 제목/주제 기반 최소 요약(추측 금지)
        topics_count: Dict[str, int] = {}
        for a in filtered:
            txt = " ".join([getattr(a, "title", "") or "", getattr(a, "summary", "") or ""])
            for tp in _detect_topics(txt):
                topics_count[tp] = topics_count.get(tp, 0) + 1

        topic_lines = []
        for tp, c in sorted(topics_count.items(), key=lambda x: -x[1]):
            line = _topic_summary_line(tp, c)
            if line:
                topic_lines.append(line)
        topic_lines = topic_lines[:1]

        top_titles = [_norm_text(getattr(a, "title", "") or "") for a in filtered[:3]]
        top_titles = [t for t in top_titles if t]

        parts = []
        if top_titles:
            parts.append(f"상단 기사 중심으로 '{top_titles[0]}' 등 주요 이슈가 확인됩니다.")
        if topic_lines:
            parts.append(topic_lines[0])
        parts.append("기사 원문 텍스트가 제한적인 항목은 제목 수준으로만 확인했습니다.")
        return _trim(" ".join(parts), 340)

    # 모델 입력 구성
    items = []
    topics_count: Dict[str, int] = {}

    for idx, a in enumerate(filtered[:10], start=1):
        title = _norm_text(getattr(a, "title", "") or "")
        source = _norm_text(getattr(a, "source", "") or "")
        text = _get_article_text(a)
        text = _trim(text, 900)

        low_info = _is_low_info_content(text)
        low_info_note = (
            "NOTE: 본문 텍스트가 매우 제한적/광고·이미지형일 수 있음. 추측 금지, 텍스트에 있는 사실만."
            if low_info else
            "NOTE: 제공된 텍스트 범위 내에서만 요약."
        )

        for tp in _detect_topics(f"{title} {text}"):
            topics_count[tp] = topics_count.get(tp, 0) + 1

        items.append(
            f"[Priority {idx}]\n"
            f"Title: {title}\n"
            f"Source: {source}\n"
            f"{low_info_note}\n"
            f"Text: {text}\n"
        )

    trend_hints = []
    for tp, c in sorted(topics_count.items(), key=lambda x: -x[1]):
        line = _topic_summary_line(tp, c)
        if line:
            trend_hints.append(line)
    trend_hints = trend_hints[:2]

    content = "\n".join(items)
    trend_block = "\n".join([f"- {t}" for t in trend_hints]) if trend_hints else "- (특정 주제 반복 언급은 두드러지지 않음)"

    system = (
        "너는 콘택트렌즈/안경 업계 데일리 뉴스레터를 임원에게 보고하는 비서다. "
        "반드시 제공된 기사 텍스트만 근거로 요약하며, 추측/과장/창작을 절대 하지 않는다."
    )

    user = f"""
아래 기사 텍스트만 근거로, 임원 보고용으로 3~4문장 요약을 작성해줘.

필수 규칙(매우 중요):
1) 절대 과장/추측/창작 금지. 기사 텍스트에 없는 정보는 쓰지 말 것.
2) 숫자/비율/성과 등은 텍스트에 명시된 경우에만 사용.
3) 문장형으로만 작성(불릿/번호/따옴표/제목 나열 금지).
4) Priority 숫자가 작은 기사(위쪽)가 더 중요하다. 중요한 내용은 앞쪽 문장에 배치.
5) 기사량이 많거나 유사 주제가 반복되면, 아래 '반복 주제 힌트'를 참고해서
   "관련 기사가 여러 건 있었다/다수 있었다" 수준으로만 언급(정확한 건수 단정 금지).
6) 본문 텍스트가 제한적인 항목은 '제목/텍스트에 보이는 범위'까지만 언급하고,
   신제품 출시/성과/계획 등은 텍스트에 없으면 쓰지 말 것.

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
            temperature=0.1,
            max_tokens=240,
        )
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"\s+\n", "\n", text).strip()
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > 340:
            text = text[:340].rstrip() + "…"
        return text or _fallback_overall(filtered)
    except Exception:
        return _fallback_overall(filtered)


# -------------------------
# Per-article summary refinement
# -------------------------
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
- 제목 문구를 그대로 반복하지 말고, 다른 표현으로 바꿔 쓸 것
- 2~3문장, 사실 중심
- 과장/추측/홍보 문구 금지
- 텍스트에 없는 내용은 쓰지 말 것
- 220자 이내(가능하면 160~200자)
- 문장형으로만(불릿/번호/따옴표 금지)
- 기사텍스트가 부족하면 '텍스트에 구체 내용이 제한적' 수준으로만 요약

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
        temperature=0.1,
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
    - summary가 title과 너무 비슷/짧으면(OpenAI 가능 시) 2~3문장으로 재작성
    - ✅ 단, 본문 텍스트가 빈약(광고/이미지형)하면 AI 재작성 금지:
        -> 기존 summary가 있으면 그대로, 없으면 title을 summary로 사용
    """
    client = _get_client()

    for a in articles:
        title = _norm_text(getattr(a, "title", "") or "")
        summary = _norm_text(getattr(a, "summary", "") or "")

        raw_text = _get_article_text(a)
        low_info = _is_low_info_content(raw_text)

        # ✅ 광고/이미지형이면 "만들어내는 요약" 방지: 제목/원문요약 수준으로만
        if low_info:
            if summary:
                summary = _trim(summary, 220)
            else:
                summary = _trim(title or "기사 요약 정보가 제한적입니다.", 220)
            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # ✅ 텍스트가 충분할 때만 LLM 재작성
        if client is not None and _is_too_similar(title, summary):
            try:
                summary = _rewrite_summary(client, title, raw_text)
            except Exception:
                pass

        summary = _trim(summary or title, 220)

        try:
            a.summary = summary
        except Exception:
            pass
