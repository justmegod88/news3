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


def _add_inline_logo(message: Mail, logo_path: str = "acuvue_logo.png", cid: str = "acuvue_logo") -> None:
    """
    로고 이미지를 CID(inline attachment)로 메일에 포함.
    HTML 템플릿에는 아래처럼 들어있어야 함:
      <img src="cid:acuvue_logo" ...>
    """
    if not os.path.exists(logo_path):
        raise FileNotFoundError(
            f"로고 파일을 찾을 수 없습니다: {logo_path}\n"
            f"레포 루트(최상위)에 '{os.path.basename(logo_path)}'가 있어야 합니다."
        )

    with open(logo_path, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode("utf-8")

    attachment = Attachment(
        FileContent(logo_b64),
        FileName(os.path.basename(logo_path)),
        FileType("image/png"),
        Disposition("inline"),
    )
    attachment.content_id = ContentId(cid)
    message.add_attachment(attachment)


def send_email_html(
    subject: str,
    html_body: str,
    from_addr: str,
    to_addrs: List[str],
    logo_path: Optional[str] = None,
):
    """SendGrid API를 이용해 HTML 메일 발송 (Outlook 안정: CID 로고 포함)"""
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

    # ✅ CID 로고 첨부 (기본: 레포 루트의 acuvue_logo.png)
    # 필요하면 logo_path 인자로 다른 경로 지정 가능
    _add_inline_logo(
        message,
        logo_path=logo_path or os.getenv("ACUVUE_LOGO_PATH", "acuvue_logo.png"),
        cid="acuvue_logo",
    )

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print("SendGrid status:", response.status_code)
    except Exception as e:
        print("SendGrid error:", e)
        raise
