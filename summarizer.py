import re
from typing import List, Optional
from urllib.parse import urlparse

# OpenAI 사용은 선택(없어도 동작)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# 본문 확인(조건부)용
import requests
from bs4 import BeautifulSoup


# =========================
# OpenAI client
# =========================
def _get_client():
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


# =========================
# Helpers
# =========================
def _norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    return s


def _is_image_file_url(url: str) -> bool:
    try:
        path = urlparse(url or "").path.lower()
    except Exception:
        path = (url or "").lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


def _is_meaningless_summary(summary: str) -> bool:
    """
    summary가 사실상 '내용 없음'에 가까운 문구인지 판별(보수적).
    """
    s = _norm_text(summary).lower()
    if not s:
        return True

    meaningless_patterns = [
        "자세한 내용", "자세히 보기", "자세히보기",
        "기사 보기", "기사보기", "원문 보기", "원문보기",
        "더보기", "보기", "바로가기",
        "사진", "이미지", "영상", "동영상",
        "관련 기사", "관련기사",
        "클릭", "확인",
    ]

    if len(s) < 12:
        return True

    for p in meaningless_patterns:
        if p in s and len(s) <= 30:
            return True

    if re.fullmatch(r"(https?://\S+)", s):
        return True

    # 특수문자 비중이 너무 높은 경우
    if len(re.sub(r"[a-z0-9가-힣]", "", s)) / max(len(s), 1) > 0.65:
        return True

    return False


def _is_summary_same_as_title(title: str, summary: str) -> bool:
    """
    summary가 title과 동일/사실상 동일인지.
    """
    t = _norm_text(title)
    s = _norm_text(summary)
    if not t or not s:
        return False

    if t == s:
        return True

    if t in s or s in t:
        if abs(len(t) - len(s)) <= 12:
            return True

    t2 = re.sub(r"[\[\(].*?[\]\)]", "", t).strip()
    s2 = re.sub(r"[\[\(].*?[\]\)]", "", s).strip()
    if t2 and s2 and t2 == s2:
        return True

    return False


def _fetch_html(url: str, timeout=(3.0, 6.0)) -> Optional[str]:
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct.startswith("image/"):
            return None
        return r.text or None
    except Exception:
        return None


def _extract_text_and_imgcount(html: str, max_chars: int = 3000) -> tuple[str, int]:
    """
    가벼운 본문 텍스트 추출 (광고 판별 + 요약 재료 확보 목적)
    """
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    img_count = len(soup.find_all("img"))

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"

    return text, img_count


def _is_image_only_ad_page(text: str, img_count: int) -> bool:
    """
    "이미지만 있는 광고" 판정(보수적).
    """
    t = _norm_text(text)
    if len(t) < 40 and img_count >= 1:
        return True
    if len(t) < 20:
        return True
    return False


# =========================
# ✅ NEW: 2~3문장 + 글자수 강제 컷(네이버 OpenAPI 포함 전체 공통 적용)
# =========================
_SENT_SPLIT_RE = re.compile(r"(?<=[\.\?\!…])\s+|(?<=다\.)\s+|(?<=니다\.)\s+|(?<=요\.)\s+")

def _enforce_2to3_sentences(text: str, max_sentences: int = 3, max_chars: int = 105) -> str:
    """
    - 모델이 길게 쓰거나 문장 수가 늘어나는 경우를 방지하기 위한 최종 안전망.
    - 1~3문장 범위로만 잘라서 반환 (가능한 한 원문 보존).
    """
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if not s:
        return s

    parts = [p.strip() for p in _SENT_SPLIT_RE.split(s) if p.strip()]
    if parts:
        s = " ".join(parts[:max_sentences]).strip()

    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"

    return s


# =========================
# OpenAI calls / prompts
# =========================
def _call_openai_2to3_sentences(client, prompt: str, max_chars: int = 105) -> str:
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = (r.choices[0].message.content or "").strip()
    text = re.sub(r"\s+\n", "\n", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _prompt_compress_long_summary(title: str, summary: str) -> str:
    return f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [요약문]을 "2~3문장"으로 압축하라.

규칙(매우 중요):
- [요약문]에 있는 사실만 유지 (새로운 사실/추측/해석/의미 부여 금지)
- 과장/홍보 문구 금지
- 기사 '출처(언론사)'를 제품/브랜드/제조사로 표현하지 말 것
- 기사에 '출시'라는 단어를 명확히 언급한 경우만 사용, 아니면 사용 절대 금지
- 안경테/렌즈/제품의 브랜드명은 [요약문]에 명확히 언급된 경우에만 사용
- 브랜드가 불명확하면 특정 주체를 단정하지 말 것
- 기사에 없는 단어 절대 사용 금지
- 2~3문장, 105자 이내

[제목]
{title}

[요약문]
{summary}

[출력]
""".strip()


def _prompt_title_only(title: str) -> str:
    return f"""
너는 뉴스 요약을 보조하는 편집자다.
⚠️ 이 작업은 매우 제한적인 작업이다.

아래 [제목]에 포함된 정보만을 사용해
문장을 2~3문장으로 "정리"하라.

🚫 절대 규칙 (위반 금지 / 정말 중요):
- 제목에 명시되지 않은 사실, 배경, 원인, 결과를 절대 추가하지 말 것
- 기사에 '출시'라는 단어를 명확히 언급한 경우만 사용, 아니면 사용 절대 금지
- 기사 본문을 추측하거나 일반적인 맥락을 보완하지 말 것
- “~로 보인다”, “~할 것으로 예상된다”, “~의미가 있다” 같은 해석 금지
- 제목에 없는 숫자/주체/행위/시점/목적을 새로 만들지 말 것
- 제목에 없는 단어를 의미상 확장하여 사용하지 말 것
- 기사에 없는 단어 절대 사용 금지

✅ 허용되는 작업:
- 제목에 있는 정보를 문법적으로만 나누어 문장으로 표현
- 하나의 긴 제목을 2~3개의 짧은 문장으로 분리
- 동일 의미 내에서 조사/어순 정도만 자연스럽게 조정

출력:
- 사실 진술형 문장만
- 2~3문장
- 105자 이내
- 과장/해석/평가 표현 금지

[제목]
{title}

[출력]
""".strip()


def _prompt_summarize_from_body(title: str, body_text: str) -> str:
    return f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [기사 본문]에 명시된 내용만 근거로 2~3문장 요약을 작성하라.

규칙(매우 중요):
- 과장/추측/해석 금지, 본문에 있는 사실만
- 기사 '출처(언론사)'를 제품/브랜드/제조사로 표현하지 말 것
- 기사에 '출시'라는 단어를 명확히 언급한 경우만 사용, 아니면 사용 절대 금지
- 안경테/렌즈/제품의 브랜드명은 본문에 명확히 언급된 경우에만 사용
- 브랜드가 불명확하면 특정 주체를 단정하지 말 것
- 기사에 없는 단어 절대 사용 금지
- 2~3문장, 105자 이내
- 가능한 한 팩트(무엇/누가/무슨 내용/어떤 조치)를 중심으로

[제목]
{title}

[기사 본문]
{body_text}

[출력]
""".strip()


# =========================
# ✅ A. 기사별 summary 정제/생성
# =========================
def refine_article_summaries(articles: List) -> None:
    """
    ✅ 각 기사 summary 정책(확정본)

    1) summary가 길게 존재 -> OpenAI로 2~3문장 "압축 요약"
    2) summary가 title과 동일(사실상 동일) -> OpenAI로 2~3문장 (제목 정보 범위 내 / 추측 절대 금지)
    3) summary가 아예 없음(또는 의미없는 수준) -> 본문 확인
       3-1) 이미지만 있는 광고 -> summary는 "빈값"
       3-2) 본문 텍스트(+이미지) -> OpenAI로 2~3문장 요약
    공통: 최종 summary는 105자 내

    ✅ 추가(요청 반영):
    - 네이버(OpenAPI/HTML 포함: a.is_naver=True)는 "길이와 상관없이" OpenAI 압축 요약을 한 번 더 적용
      (구글 기사처럼 항상 AI 요약을 태우고, 최종 105자/2~3문장 강제)
    """
    client = _get_client()

    LONG_SUMMARY_THRESHOLD = 150
    MAX_SUMMARY_CHARS = 105

    for a in articles:
        title = _norm_text(getattr(a, "title", "") or "")
        summary_raw = getattr(a, "summary", "") or ""
        summary = _norm_text(summary_raw)
        link = (getattr(a, "link", "") or "").strip()

        # ✅ 네이버(OpenAPI 포함) 판별 플래그
        is_naver = bool(getattr(a, "is_naver", False))

        # 링크가 이미지 파일이면: 광고/배너로 보고 summary는 빈값
        if _is_image_file_url(link):
            try:
                a.summary = ""
            except Exception:
                pass
            continue

        # 3) summary 없음/무의미 -> 본문 확인
        if not summary or _is_meaningless_summary(summary):
            html = _fetch_html(link)
            if not html:
                # 본문을 못 가져오면 추측 금지 -> 빈값
                try:
                    a.summary = ""
                except Exception:
                    pass
                continue

            body_text, img_count = _extract_text_and_imgcount(html)

            # 3-1) 이미지만 광고 -> 빈값
            if _is_image_only_ad_page(body_text, img_count):
                try:
                    a.summary = ""
                except Exception:
                    pass
                continue

            # 3-2) 본문 텍스트 -> AI 요약(가능하면)
            if client is not None:
                try:
                    prompt = _prompt_summarize_from_body(title, body_text)
                    summary = _call_openai_2to3_sentences(client, prompt, max_chars=MAX_SUMMARY_CHARS)
                except Exception:
                    # 실패 시: 본문 일부를 그대로(추측 없이) 표시
                    summary = _norm_text(body_text)[:MAX_SUMMARY_CHARS].rstrip()
            else:
                summary = _norm_text(body_text)[:MAX_SUMMARY_CHARS].rstrip()

            # ✅ NEW: 최종 2~3문장 + 105자 강제
            summary = _enforce_2to3_sentences(summary, max_sentences=3, max_chars=MAX_SUMMARY_CHARS)

            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # 2) summary == title -> 제목 정보만으로 2~3문장(추측 절대 금지)
        if _is_summary_same_as_title(title, summary):
            if client is not None:
                try:
                    prompt = _prompt_title_only(title)
                    summary = _call_openai_2to3_sentences(client, prompt, max_chars=105)
                except Exception:
                    summary = title
            else:
                summary = title

            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

            # ✅ NEW: 최종 2~3문장 + 105자 강제
            summary = _enforce_2to3_sentences(summary, max_sentences=3, max_chars=MAX_SUMMARY_CHARS)

            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # 1) summary가 길면 -> 압축 요약
        if len(summary) >= LONG_SUMMARY_THRESHOLD:
            if client is not None:
                try:
                    prompt = _prompt_compress_long_summary(title, summary)
                    summary = _call_openai_2to3_sentences(client, prompt, max_chars=MAX_SUMMARY_CHARS)
                except Exception:
                    summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"
            else:
                summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

        # ✅ 추가(요청 반영 핵심)
        # - 네이버(OpenAPI 포함)는 길이와 상관없이 OpenAI로 "2~3문장 압축"을 한 번 더 적용
        # - 구글 기사처럼 항상 AI 요약을 태우고 싶다는 요구사항 대응
        if is_naver and client is not None:
            try:
                # summary가 이미 짧더라도 "문장형 2~3문장"으로 정리하기 위해 한 번 더 압축
                prompt = _prompt_compress_long_summary(title, summary)
                summary = _call_openai_2to3_sentences(client, prompt, max_chars=MAX_SUMMARY_CHARS)
            except Exception:
                # 실패 시 기존 summary 유지(로직 변경 최소화)
                pass

        # 공통: 최종 컷
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

        # ✅ NEW: 최종 2~3문장 + 105자 강제
        summary = _enforce_2to3_sentences(summary, max_sentences=3, max_chars=MAX_SUMMARY_CHARS)

        try:
            a.summary = summary
        except Exception:
            pass




# =========================
# ✅ B. 전체 브리핑 fallback + 문장수
# =========================
def _fallback_overall(articles: List, max_chars: int = 360) -> str:
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    items = []
    for a in articles[:3]:
        t = (getattr(a, "title", "") or "").strip()
        s = (getattr(a, "summary", "") or "").strip()
        if s:
            s = re.sub(r"\s+", " ", s)
            s = s[:120].rstrip() + ("…" if len(s) > 120 else "")
            items.append(f"- {t}: {s}")
        else:
            items.append(f"- {t}")
    out = "어제 주요 이슈:\n" + "\n".join(items)
    return out[:max_chars]


def _auto_sentence_target(n_articles: int) -> int:
    # 1개: 1문장, 2개: 2문장, 3개 이상: 최대 3문장
    if n_articles <= 1:
        return 1
    if n_articles == 2:
        return 2
    return 3



# =========================
# ✅ B. 전체 브리핑 (총평 + 이슈 묶기형 / 나열 금지 / 추측 금지)
# =========================
def summarize_overall(articles: List) -> str:
    """
    ✅ 임원용 "어제 기사 AI 브리핑" (이슈 묶기형)
    - 1문장: 총평(어제 핵심 흐름/경향)  ※ 단, 입력에 근거한 범위 내에서만
    - 2~3문장: 서로 다른 이슈 단위 요약 (기사 1개=1문장 나열 금지)
    - 과장/추측 금지
    - 임원보고용 공손한 말투
    """
    # 전망/평가 금지 (특히 "~로 보인다/~할 듯" 금지)
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    client = _get_client()
    if client is None:
        return _fallback_overall(articles)

    # 입력 정리 (너무 길면 안정적으로 컷)
    items = []
    for a in articles[:10]:
        t = (getattr(a, "title", "") or "").strip()
        s = (getattr(a, "summary", "") or "").strip()
        s = re.sub(r"\s+", " ", s).strip()

        if len(s) > 150:
            s = s[:150].rstrip() + "…"

        # summary가 빈 값이면(광고/텍스트 없음) 전체 요약 재료로 쓰지 않음
        if not s:
            continue

        items.append(f"- 제목: {t}\n  요약: {s}")

    if not items:
        return "어제는 수집된 기사 중 텍스트 요약이 가능한 항목이 없어, 주요 이슈를 요약할 내용이 없습니다."

    target_sentences = _auto_sentence_target(len(items))

    prompt = f"""
너는 콘택트렌즈/안경 업계 데일리 뉴스레터를 임원에게 보고하는 비서다.
아래 [기사 제목/요약]만을 근거로 '어제 기사 AI 브리핑'을 작성하라.

🚫 절대 규칙 (가장 중요):
- 아래 입력에 없는 사실/숫자/주체/브랜드/원인/결과를 절대 추가하지 말 것
- 과장/추측/전망/평가 금지
  * 금지 예: "~로 보인다", "~할 것으로 예상", "~가능성이 높다", "~시사한다", "~의미가 크다"
- 기사에 '출시'라는 단어를 명확히 언급한 경우만 사용, 아니면 사용 절대 금지
- 트렌드/경향 언급은 가능하나, 반드시 입력에서 관찰되는 범위로만 표현할 것
  * 허용 예: "관련 보도가 이어졌다", "○○ 주제가 다수 기사에서 반복됐다"
  * 금지 예: "시장 확대/축소로 이어질 것", "전략적으로 중요해질 것" (미래/해석)

✅ 출력 형식(중요):
- 총 {target_sentences}문장 (문장 수 정확히 지킬 것)
- 1문장째: 전체 총평(어제 핵심 흐름/경향을 1문장으로)
- 2~{target_sentences}문장째: 서로 다른 '이슈' 단위로 요약
- 유사한 기사/동일 사건은 하나의 이슈로 묶어서 1문장으로만 작성
- 문장마다 특정 기사 1개를 그대로 옮겨 적는 '나열형' 금지 (반드시 이슈 묶기 → 이슈 요약 형태)
- 전체 420자 이내, 문장은 짧고 단정하게

[기사 제목/요약]
{chr(10).join(items)}
""".strip()

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"\s+\n", "\n", text).strip()
        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            return _fallback_overall(articles)

        if len(text) > 420:
            text = text[:420].rstrip() + "…"

        # ✅ NEW: 문장 수가 늘어지는 경우를 방지(최대 3문장 범위로만 안전 컷)
        # (target_sentences는 1~3이므로, 상한 3으로만 강제)
        text = _enforce_2to3_sentences(text, max_sentences=3, max_chars=420)

        return text
    except Exception:
        return _fallback_overall(articles)
