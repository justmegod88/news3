import re
from typing import List

# OpenAI 사용은 선택(없어도 동작)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# 본문 가져오기(요약 비어있을 때만 사용)
import requests
from bs4 import BeautifulSoup


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
    - 입력된 기사 리스트만 요약
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
        if len(text) > 340:
            text = text[:340].rstrip() + "…"
        return text or _fallback_summary(articles)
    except Exception:
        return _fallback_summary(articles)


# =========================
# ✅ 기사별 summary 정책 (요청 반영)
# =========================
def _norm_text(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"[\"'“”‘’]", "", s)
    return s


def _is_image_only_page(html_text: str) -> bool:
    """
    본문이 사실상 이미지/광고만 있는 페이지인지 대략 판별.
    - 텍스트가 거의 없고(img는 있는데) => 이미지-only로 본다
    """
    if not html_text:
        return True

    soup = BeautifulSoup(html_text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()

    imgs = soup.find_all("img")

    # 텍스트가 매우 짧고 이미지가 있으면 이미지-only로 간주
    if len(text) < 40 and len(imgs) >= 1:
        return True
    # 텍스트가 거의 없으면 이미지-only로 간주
    if len(text) < 20:
        return True

    return False


def _extract_main_text_from_url(
    url: str,
    timeout_connect: float = 3.0,
    timeout_read: float = 6.0,
    max_chars: int = 2500,
) -> str:
    """
    기사 본문 텍스트를 '대충' 뽑아오는 안전한 함수.
    (정교한 본문 추출기는 아니고, 요약에 쓸 정도로만)
    """
    if not url:
        return ""

    if not (url.startswith("http://") or url.startswith("https://")):
        return ""

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        r = requests.get(url, headers=headers, timeout=(timeout_connect, timeout_read))
        r.raise_for_status()
    except Exception:
        return ""

    ct = (r.headers.get("Content-Type") or "").lower()
    if ct.startswith("image/"):
        return ""

    html_text = r.text or ""
    if _is_image_only_page(html_text):
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _compress_summary(client, title: str, summary: str) -> str:
    """
    summary가 너무 길 때만: OpenAI로 2~3문장 압축 (추가 사실 금지)
    """
    title = _norm_text(title)
    summary = _norm_text(summary)

    prompt = f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [요약문]을 2~3문장으로 압축해라.

규칙:
- 원문(요약문)에 있는 사실만 유지 (추가 추측/추가 정보 금지)
- 2~3문장
- 220자 이내
- 홍보/과장 금지

[제목]
{title}

[요약문]
{summary}

[출력]
""".strip()

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = (r.choices[0].message.content or "").strip()
    text = _norm_text(text)
    if len(text) > 220:
        text = text[:220].rstrip() + "…"
    return text


def _summarize_from_body(client, title: str, body_text: str) -> str:
    """
    summary가 비어있고 본문 텍스트가 있을 때만: OpenAI로 2~3문장 생성
    """
    title = _norm_text(title)
    body_text = _norm_text(body_text)

    prompt = f"""
너는 업계 데일리 뉴스레터 편집자다.
아래 [기사 본문]만 근거로 2~3문장으로 사실 중심 요약을 작성하라.

규칙:
- 과장/추측 금지, 홍보 문구 금지
- 숫자/기관/제품명/규제 등 핵심 팩트 위주
- 2~3문장
- 220자 이내

[제목]
{title}

[기사 본문]
{body_text}

[출력]
""".strip()

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    text = (r.choices[0].message.content or "").strip()
    text = _norm_text(text)
    if len(text) > 220:
        text = text[:220].rstrip() + "…"
    return text


def refine_article_summaries(articles: List) -> None:
    """
    ✅ 기사별 summary 규칙 (너가 요청한 그대로)

    * summary가 너무 짧으면 → 짧은대로 유지
    * summary가 비어있으면 →
        - 본문 들어가서 이미지 파일만 있으면: 타이틀만 노출(= summary 빈값 유지)
        - 본문 텍스트 있으면: AI로 2~3문장 생성
    * title이랑 비슷하면 → 그대로(재작성 금지)
    * summary가 너무 길면 → OpenAI로 2~3문장 (압축)
    """
    client = _get_client()

    SHORT_KEEP_THRESHOLD = 60   # 짧으면 유지
    LONG_THRESHOLD = 220        # 길면 압축 대상
    HARD_CUT = 220              # OpenAI 없을 때 안전 컷

    for a in articles:
        title = getattr(a, "title", "") or ""
        summary = getattr(a, "summary", "") or ""
        link = (getattr(a, "link", "") or "").strip()

        title_n = _norm_text(title)
        summary_n = _norm_text(summary)

        # 1) summary가 너무 짧으면: 그대로 유지 (AI 금지)
        if summary_n and len(summary_n) < SHORT_KEEP_THRESHOLD:
            try:
                a.summary = summary_n
            except Exception:
                pass
            continue

        # 2) summary가 비어있으면: 본문 들어가서 처리
        if not summary_n:
            body_text = _extract_main_text_from_url(link)

            # 2-A) 이미지/광고 위주(텍스트 없음)면: summary는 빈 값 유지(타이틀만 노출)
            if not body_text:
                try:
                    a.summary = ""
                except Exception:
                    pass
                continue

            # 2-B) 본문 텍스트 있으면: AI로 2~3문장 생성 (가능할 때만)
            if client is not None:
                try:
                    summary_n = _summarize_from_body(client, title_n, body_text)
                except Exception:
                    # 실패 시: 지어내지 않음. 본문 일부 그대로 사용
                    summary_n = _norm_text(body_text)
                    if len(summary_n) > HARD_CUT:
                        summary_n = summary_n[:HARD_CUT].rstrip() + "…"
            else:
                # OpenAI 없으면: 본문 일부 그대로 사용(생성 X)
                summary_n = _norm_text(body_text)
                if len(summary_n) > HARD_CUT:
                    summary_n = summary_n[:HARD_CUT].rstrip() + "…"

            try:
                a.summary = summary_n
            except Exception:
                pass
            continue

        # 3) title이랑 비슷하면 → 그대로 (아무 처리 안 함)

        # 4) summary가 너무 길면: OpenAI로 2~3문장 압축
        if len(summary_n) > LONG_THRESHOLD:
            if client is not None:
                try:
                    summary_n = _compress_summary(client, title_n, summary_n)
                except Exception:
                    summary_n = summary_n[:HARD_CUT].rstrip() + "…"
            else:
                summary_n = summary_n[:HARD_CUT].rstrip() + "…"

        # 최종 저장
        try:
            a.summary = summary_n
        except Exception:
            pass
