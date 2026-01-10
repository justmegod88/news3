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
# Executive overall brief (어제 기사 브리핑)
# =========================
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
    - 입력된 기사 리스트만 요약
    - 최대 3~4문장, 너무 길면 컷
    """
    if not articles:
        return "어제 기준으로 수집된 관련 기사가 없어 별도 공유 사항은 없습니다."

    client = _get_client()
    if client is None:
        return _fallback_summary(articles)

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
- 너무 길면 300자 내로 자연스럽게 줄여
- 쉼표로 길게 늘어놓지 말고 문장 3~4개로

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
        if len(text) > 340:
            text = text[:340].rstrip() + "…"
        return text or _fallback_summary(articles)
    except Exception:
        return _fallback_summary(articles)


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
    summary가 사실상 '내용 없음'에 가까운 문구인지 판별.
    (너무 공격적으로 잡지 않게 보수적으로 구성)
    """
    s = _norm_text(summary).lower()
    if not s:
        return True

    # 너무 짧고 의미 없는 흔한 문구들
    meaningless_patterns = [
        "자세한 내용", "자세히 보기", "자세히보기",
        "기사 보기", "기사보기", "원문 보기", "원문보기",
        "더보기", "보기", "바로가기",
        "사진", "이미지", "영상", "동영상",
        "관련 기사", "관련기사",
        "클릭", "확인",
    ]

    # 완전히 짧은 경우
    if len(s) < 12:
        return True

    for p in meaningless_patterns:
        if p in s and len(s) <= 30:
            return True

    # URL만 있거나 특수문자/기호 중심
    if re.fullmatch(r"(https?://\S+)", s):
        return True
    if len(re.sub(r"[a-z0-9가-힣]", "", s)) / max(len(s), 1) > 0.65:
        return True

    return False


def _is_summary_same_as_title(title: str, summary: str) -> bool:
    """
    summary가 title과 동일/사실상 동일인지.
    - 완전 동일
    - title 포함/역포함 + 길이 차이 거의 없음
    """
    t = _norm_text(title)
    s = _norm_text(summary)
    if not t or not s:
        return False

    if t == s:
        return True

    # 포함 관계 + 길이 차이 거의 없으면 동일 취급
    if t in s or s in t:
        if abs(len(t) - len(s)) <= 12:
            return True

    # 괄호/대괄호 제거 후 동일
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
    아주 가벼운 본문 텍스트 추출.
    - 정교한 본문 추출기 아님
    - 목적: "텍스트가 거의 없는 이미지 페이지" 판별 + 텍스트 요약 재료 확보
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
    "이미지만 있는 광고" 판정 (보수적):
    - 텍스트가 거의 없고(img는 존재) => 이미지 위주 페이지로 간주
    """
    t = _norm_text(text)
    # 텍스트가 매우 짧고 이미지가 있으면 광고/배너 가능성 큼
    if len(t) < 40 and img_count >= 1:
        return True
    # 텍스트가 거의 없다면(이미지 수 무관) 이미지/링크 모음일 가능성
    if len(t) < 20:
        return True
    return False


# =========================
# OpenAI prompts (3종)
# =========================
def _call_openai_2to3_sentences(client, prompt: str, max_chars: int = 220) -> str:
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
    # 1) summary가 길게 존재 -> 2~3문장 압축 요약
    return f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [요약문]을 "2~3문장"으로 압축하라.

규칙(매우 중요):
- [요약문]에 있는 사실만 유지 (새로운 사실/추측/해석/의미 부여 금지)
- 과장/홍보 문구 금지
- 기사 '출처(언론사)'를 제품/브랜드/제조사로 표현하지 말 것
- 안경테/렌즈/제품의 브랜드명은 [요약문]에 명확히 언급된 경우에만 사용
- 브랜드가 불명확하면 특정 주체를 단정하지 말 것
- 기사에 없는 단어 절대 사용 금지
- 2~3문장, 220자 이내

[제목]
{title}

[요약문]
{summary}

[출력]
""".strip()


def _prompt_title_only(title: str) -> str:
    # 2) summary=title -> 제목 정보 범위 내에서만 2~3문장 정리 (추측 절대 금지)
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
    # 3) summary 없음 -> 본문 텍스트 기반 2~3문장 요약
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
# ✅ Main: refine per-article summary (네 정책 1/2/3 구현)
# =========================
def refine_article_summaries(articles: List) -> None:
    """
    ✅ 각 기사 summary 정책 (네가 확정한 버전)

    1) summary가 길게 존재 -> OpenAI로 2~3문장 "압축 요약"
    2) summary가 title과 동일(사실상 동일) -> OpenAI로 2~3문장 (제목 정보 범위 내에서만 / 추측 절대 금지)
    3) summary가 아예 없음(또는 의미없는 수준) -> 본문 확인
       3-1) 이미지만 있는 광고 -> summary는 "빈값"
       3-2) 본문 텍스트(+이미지) -> OpenAI로 2~3문장 요약

    공통: 최종 summary는 220자 내로 유지
    """
    client = _get_client()

    # 정책 파라미터(필요하면 조절)
    LONG_SUMMARY_THRESHOLD = 260   # "길다" 기준(원문 summary가 이 이상이면 압축 요약)
    MAX_SUMMARY_CHARS = 220

    for a in articles:
        title = _norm_text(getattr(a, "title", "") or "")
        summary_raw = getattr(a, "summary", "") or ""
        summary = _norm_text(summary_raw)
        link = (getattr(a, "link", "") or "").strip()

        # (추가 보호) 링크가 이미지 파일 자체면: 광고/배너 가능성이 높으므로 summary는 빈값
        # ※ 너가 "빈값" 선택했으니 title로 덮지 않고 빈값 유지
        if _is_image_file_url(link):
            try:
                a.summary = ""
            except Exception:
                pass
            continue

        # 3) summary 없음(또는 의미없는 수준) -> 본문 확인
        if not summary or _is_meaningless_summary(summary):
            # 본문 1회 확인
            html = _fetch_html(link)
            if not html:
                # 본문을 못 가져오면 판단 불가 -> 빈값 유지(추측 금지)
                try:
                    a.summary = ""
                except Exception:
                    pass
                continue

            body_text, img_count = _extract_text_and_imgcount(html)

            # 3-1) 이미지만 있는 광고 -> summary 빈값
            if _is_image_only_ad_page(body_text, img_count):
                try:
                    a.summary = ""
                except Exception:
                    pass
                continue

            # 3-2) 본문 텍스트 존재 -> AI 요약(가능할 때만)
            if client is not None:
                try:
                    prompt = _prompt_summarize_from_body(title, body_text)
                    new_sum = _call_openai_2to3_sentences(client, prompt, max_chars=MAX_SUMMARY_CHARS)
                    summary = new_sum
                except Exception:
                    # 실패 시: 본문 일부를 그대로(추측 없이) 짧게 표시
                    summary = _norm_text(body_text)[:MAX_SUMMARY_CHARS].rstrip()
            else:
                # OpenAI 없으면: 본문 일부를 그대로(추측 없이) 짧게 표시
                summary = _norm_text(body_text)[:MAX_SUMMARY_CHARS].rstrip()

            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # 2) summary가 title과 동일(사실상 동일) -> 제목 정보 범위 내에서만 2~3문장
        if _is_summary_same_as_title(title, summary):
            if client is not None:
                try:
                    prompt = _prompt_title_only(title)
                    summary = _call_openai_2to3_sentences(client, prompt, max_chars=200)
                except Exception:
                    # 실패 시: 제목 그대로(추측 금지)
                    summary = title
            else:
                # OpenAI 없으면 제목 그대로(추측 금지)
                summary = title

            # 최종 길이 컷
            if len(summary) > MAX_SUMMARY_CHARS:
                summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

            try:
                a.summary = summary
            except Exception:
                pass
            continue

        # 1) summary가 길게 존재 -> 2~3문장 압축 요약
        if len(summary) >= LONG_SUMMARY_THRESHOLD:
            if client is not None:
                try:
                    prompt = _prompt_compress_long_summary(title, summary)
                    summary = _call_openai_2to3_sentences(client, prompt, max_chars=MAX_SUMMARY_CHARS)
                except Exception:
                    # 실패 시 컷
                    summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"
            else:
                summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

        # 공통: 최종 길이 컷
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"

        try:
            a.summary = summary
        except Exception:
            pass
