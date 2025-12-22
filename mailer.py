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
        print("SendGrid status:", response.status_code)
        print("From:", actual_from)
        print("To:", recipients)
    except Exception as e:
        print("SendGrid error:", e)
        raise
