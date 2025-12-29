import datetime as dt
import re

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None

PRIMARY_DATE_PATTERNS = [
    r"(입력|등록|작성|기사입력|게재|등록일|작성일)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(입력|등록|작성|기사입력|게재|등록일|작성일)\s*[:：]?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
    r"(입력|등록|작성|기사입력|게재|등록일|작성일)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*\d{1,2}:\d{2}",
    r"(수정|업데이트|최종수정)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(수정|업데이트|최종수정)\s*[:：]?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
]

FALLBACK_DATE_PATTERNS = [
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",                 # 2025-12-28 / 2025.12.28
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",         # 2025년 12월 28일
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\.",                # 2025.12.28.
    r"(\d{4})-(\d{2})-(\d{2})T",                               # 2025-12-28T...
]

def _now_kst() -> dt.datetime:
    return dt.datetime.now(KST) if KST else dt.datetime.now()

def _is_valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        dt.date(y, m, d)
        return 2000 <= y <= (_now_kst().year + 1)
    except Exception:
        return False

def _score_candidate(y: int, m: int, d: int) -> int:
    today = _now_kst().date()
    cand = dt.date(y, m, d)
    delta = abs((today - cand).days)
    return max(0, 100000 - delta)

def extract_best_date(text: str):
    """
    성공하면 dt.date 반환, 실패하면 None
    """
    if not text:
        return None

    for pat in PRIMARY_DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            nums = [g for g in m.groups() if g and re.fullmatch(r"\d{1,4}", g)]
            if len(nums) >= 3:
                y, mo, da = map(int, nums[-3:])
                if _is_valid_ymd(y, mo, da):
                    return dt.date(y, mo, da)

    cands = []
    for pat in FALLBACK_DATE_PATTERNS:
        for m in re.finditer(pat, text):
            y, mo, da = map(int, m.groups())
            if _is_valid_ymd(y, mo, da):
                cands.append((y, mo, da))

    if not cands:
        return None

    best = max(cands, key=lambda t: _score_candidate(*t))
    return dt.date(*best)

def is_exact_yesterday(text: str) -> bool:
    cand = extract_best_date(text or "")
    if not cand:
        return False
    y = _now_kst().date() - dt.timedelta(days=1)
    return cand == y
