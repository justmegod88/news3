import os
import base64
from typing import List, Optional

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail,
    Attachment,
    FileContent,
    FileName,
    FileType,
    Disposition,
    ContentId,
)


def _find_logo_path() -> Optional[str]:
    """
    로고 파일 위치를 유연하게 찾음:
    - ./assets/acuvue_logo.png
    - ./acuvue_logo.png
    """
    candidates = [
        os.path.join("assets", "acuvue_logo.png"),
        "acuvue_logo.png",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _make_inline_png_attachment(path: str, cid: str) -> Attachment:
    with open(path, "rb") as f:
        data = f.read()
    encoded = base64.b64encode(data).decode("utf-8")

    att = Attachment()
    att.file_content = FileContent(encoded)
    att.file_type = FileType("image/png")
    att.file_name = FileName(os.path.basename(path))
    att.disposition = Disposition("inline")
    att.content_id = ContentId(cid)  # 템플릿에서 src="cid:acuvue_logo"
    return att


def _mask_key(k: str) -> str:
    if not k:
        return ""
    k = k.strip()
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:4]}...{k[-4:]}"


def send_email_html(
    subject: str,
    html_body: str,
    from_addr: str,
    to_addrs: List[str],
):
    """SendGrid API를 이용해 HTML 메일 발송 (로고 CID inline 포함)"""

    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        raise RuntimeError("SENDGRID_API_KEY 환경변수가 설정되어 있지 않습니다.")

    actual_from = os.getenv("SENDGRID_FROM", from_addr)

    override_to = os.getenv("SENDGRID_TO")
    if override_to:
        recipients = [x.strip() for x in override_to.split(",") if x.strip()]
    else:
        recipients = to_addrs

    # ✅ 디버그(중요): Actions 로그에서 “진짜 여기까지 왔는지” 확인용
    print("[mailer] send_email_html() called")
    print("[mailer] subject:", subject)
    print("[mailer] from:", actual_from)
    print("[mailer] to:", recipients)
    print("[mailer] html chars:", len(html_body or ""))
    print("[mailer] SENDGRID_API_KEY:", _mask_key(api_key))

    message = Mail(
        from_email=actual_from,
        to_emails=recipients,
        subject=subject,
        html_content=html_body,
    )

    # ✅ 로고 CID 첨부
    logo_path = _find_logo_path()
    if logo_path:
        message.attachment = _make_inline_png_attachment(logo_path, "acuvue_logo")
        print(f"[mailer] inline logo attached: {logo_path} (cid=acuvue_logo)")
    else:
        print("[mailer] WARNING: acuvue_logo.png not found (assets/ or root). Logo will not show in Outlook.")

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)

        # ✅ 여기 3줄이 핵심: SendGrid에 “요청이 접수”됐는지 추적
        print("[mailer] SendGrid status:", response.status_code)
        # headers는 dict처럼 옴. 있으면 request-id / message-id 같은 식별자가 찍힘
        try:
            headers = dict(response.headers or {})
        except Exception:
            headers = {}

        if headers:
            print("[mailer] SendGrid headers:")
            for k in sorted(headers.keys()):
                v = headers.get(k)
                # 너무 길면 컷
                if isinstance(v, str) and len(v) > 200:
                    v = v[:200] + "…"
                print(f"  - {k}: {v}")
        else:
            print("[mailer] SendGrid headers: (empty)")

    except Exception as e:
        print("[mailer] SendGrid error:", repr(e))
        raise
