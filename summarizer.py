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


def _count_sentences(s: str) -> int:
    if not s:
        return 0
    parts = re.split(r"[.!?。！？]", s)
    parts = [p.strip() for p in parts if p.strip()]
    return len(parts)


def _is_image_file_url(url: str) -> bool:
    try:
        path = urlparse(url or "").path.lower()
    except Exception:
        path = (url or "").lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


def _is_meaningless_summary(summary: str) -> bool:
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

    if len(re.sub(r"[a-z0-9가-힣]", "", s)) / max(len(s), 1) > 0.65:
        return True

    return False


def _is_summary_same_as_title(title: str, summary: str) -> bool:
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
    t = _norm_text(text)
    if len(t) < 40 and img_count >= 1:
        return True
    if len(t) < 20:
        return True
    return False


# =========================
# OpenAI calls / prompts (❗원문 그대로)
# =========================
def _call_openai_2to3_sentences(client, prompt: str, max_chars: int = 220) -> str:
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = (r.choices[0].message.content or "").strip()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _prompt_compress_long_summary(title: str, summary: str) -> str:
    return f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [요약문]을 "2~3문장"으로 압축하라.

규칙(매우 중요):
- [요약문]에 있는 사실만 유지 (새로운 사실/추측/해석 금지)
- 과장/홍보 문구 금지
- 기사에 없는 단어 절대 사용 금지
- 2~3문장, 220자 이내

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
- 200자 이내
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
- 안경테/렌즈/제품의 브랜드명은 본문에 명확히 언급된 경우에만 사용
- 브랜드가 불명확하면 특정 주체를 단정하지 말 것
- 기사에 없는 단어 절대 사용 금지
- 2~3문장, 220자 이내
- 가능한 한 팩트(무엇/누가/무슨 내용/어떤 조치)를 중심으로

[제목]
{title}

[기사 본문]
{body_text}

[출력]
""".strip()


# =========================
# ✅ A. 기사별 summary 정제/생성 (최종 확정)
# =========================
def refine_article_summaries(articles: List) -> None:
    """
    ✅ 요약 정책(확정본)

    1) summary가 길다
       - 260자 이상 OR 문장 수 > 3
       → 압축 프롬프트

    2) summary가 title과 동일/사실상 동일
       → title-only 프롬프트

    3) summary가 없음/무의미
       3-1) 이미지만 있는 광고 → 빈값
       3-2) 본문 텍스트 → body 프롬프트

    공통:
    - OpenAI 없으면 의미 생성 없이 문장 2~3개만 유지
    - 최종 summary는 220자 이내
    """
    client = _get_client()

    LONG_SUMMARY_THRESHOLD = 260
    MAX_SUMMARY_CHARS = 220

    for a in articles:
        title = _norm_text(getattr(a, "title", "") or "")
        summary_raw = getattr(a, "summary", "") or ""
        summary = _norm_text(summary_raw)
        link = (getattr(a, "link", "") or "").strip()

        # 이미지 링크 → 광고
        if _is_image_file_url(link):
            a.summary = ""
            continue

        # 3) summary 없음/무의미
        if not summary or _is_meaningless_summary(summary):
            html = _fetch_html(link)
            if not html:
                a.summary = ""
                continue

            body_text, img_count = _extract_text_and_imgcount(html)
            if _is_image_only_ad_page(body_text, img_count):
                a.summary = ""
                continue

            if client:
                prompt = _prompt_summarize_from_body(title, body_text)
                summary = _call_openai_2to3_sentences(client, prompt, MAX_SUMMARY_CHARS)
            else:
                sentences = re.split(r"(?<=[.!?。！？])\s+", body_text)
                summary = " ".join(sentences[:3])

            a.summary = summary[:MAX_SUMMARY_CHARS]
            continue

        # 2) summary == title
        if _is_summary_same_as_title(title, summary):
            if client:
                prompt = _prompt_title_only(title)
                summary = _call_openai_2to3_sentences(client, prompt, 200)
            else:
                summary = title

            a.summary = summary[:MAX_SUMMARY_CHARS]
            continue

        # 1) summary가 길다 (🔧 문장 수 조건 포함)
        if len(summary) >= LONG_SUMMARY_THRESHOLD or _count_sentences(summary) > 3:
            if client:
                prompt = _prompt_compress_long_summary(title, summary)
                summary = _call_openai_2to3_sentences(client, prompt, MAX_SUMMARY_CHARS)
            else:
                sentences = re.split(r"(?<=[.!?。！？])\s+", summary)
                summary = " ".join(sentences[:3])

        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

        a.summary = summary
