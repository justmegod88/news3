import datetime as dt
import re

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None


# 1) "입력/등록/작성/기사입력/수정" 근처 날짜 후보
PRIMARY_DATE_PATTERNS = [
    r"(입력|등록|작성|기사입력)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(입력|등록|작성|기사입력)\s*[:：]?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
    r"(입력|등록|작성|기사입력)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*\d{1,2}:\d{2}",
    r"(수정)\s*[:：]?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(수정)\s*[:：]?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
]

# 2) 전체 텍스트에서 날짜 후보
FALLBACK_DATE_PATTERNS = [
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})",
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
]


def _now_kst() -> dt.datetime:
    if KST:
        return dt.datetime.now(KST)
    return dt.datetime.now()


def _is_valid_ymd(y: int, m: int, d: int) -> bool:
    if y < 2000 or y > (_now_kst().year + 1):
        return False
    if m < 1 or m > 12:
        return False
    if d < 1 or d > 31:
        return False
    try:
        dt.date(y, m, d)
        return True
    except Exception:
        return False


def _score_candidate(y: int, m: int, d: int) -> int:
    """
    '그럴듯한 날짜' 선택을 위해 점수화:
    - 오늘에 가까울수록 점수 높게
    """
    today = _now_kst().date()
    cand = dt.date(y, m, d)
    delta = abs((today - cand).days)
    # 가까울수록 높은 점수
    return max(0, 100000 - delta)


def extract_best_date(text: str):
    """
    2) 입력/등록/작성/기사입력/수정 등에서 우선 추출
    3) 없으면 전체에서 날짜 후보들 중 '가장 그럴듯한' 날짜 선택
    5) 실패하면 None
    """
    if not text:
        return None

    # (A) 우선 패턴
    for pat in PRIMARY_DATE_PATTERNS:
        m = re.search(pat, text)
        if m:
            # 그룹 구성 차이 대응
            nums = [g for g in m.groups() if g and re.fullmatch(r"\d{1,4}", g)]
            if len(nums) >= 3:
                y, mo, da = map(int, nums[-3:])
                if _is_valid_ymd(y, mo, da):
                    return dt.date(y, mo, da)

    # (B) 전체 후보 수집
    cands = []
    for pat in FALLBACK_DATE_PATTERNS:
        for m in re.finditer(pat, text):
            y, mo, da = map(int, m.groups())
            if _is_valid_ymd(y, mo, da):
                cands.append((y, mo, da))

    if not cands:
        return None

    # 가장 그럴듯한(오늘과 가까운) 날짜 선택
    best = max(cands, key=lambda t: _score_candidate(*t))
    return dt.date(*best)


def is_exact_yesterday(text: str) -> bool:
    """
    4) KST 기준 어제 날짜와 (연/월/일) 완전 일치하면 True
    5) 추출 실패하면 무조건 False
    """
    cand = extract_best_date(text or "")
    if not cand:
        return False
    y = _now_kst().date() - dt.timedelta(days=1)
    return cand == y
