# date_filter.py
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

KST = ZoneInfo("Asia/Seoul")

PRIMARY_PATTERNS = [
    r"(?:입력|등록|기사입력|작성)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
]

SECONDARY_PATTERNS = [
    r"(?:수정|업데이트)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
]

def _make_date(y, m, d) -> Optional[datetime]:
    try:
        return datetime(int(y), int(m), int(d), tzinfo=KST)
    except Exception:
        return None

def extract_date_from_text(text: str) -> Optional[datetime]:
    if not text:
        return None

    # 1️⃣ 입력/등록/작성 우선
    for pat in PRIMARY_PATTERNS:
        m = re.search(pat, text)
        if m:
            dt = _make_date(*m.groups())
            if dt:
                return dt

    # 2️⃣ 수정/업데이트, 한글 날짜
    for pat in SECONDARY_PATTERNS[:2]:
        m = re.search(pat, text)
        if m:
            dt = _make_date(*m.groups())
            if dt:
                return dt

    # 3️⃣ 전체 텍스트 fallback (첫 번째 등장 날짜)
    for m in re.finditer(SECONDARY_PATTERNS[2], text):
        dt = _make_date(*m.groups())
        if dt:
            return dt

    return None

def is_exact_yesterday(text: str) -> bool:
    now = datetime.now(KST)
    yesterday = (now - timedelta(days=1)).date()

    article_dt = extract_date_from_text(text)
    if article_dt is None:
        return False  # ❗ 실패 시 무조건 제외

    return article_dt.date() == yesterday*
