import os
from typing import List

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


def send_email_html(
    subject: str,
    html_body: str,
    from_addr: str,
    to_addrs: List[str],
):
    """SendGrid API를 이용해 HTML 메일 발송"""
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

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print("SendGrid status:", response.status_code)
    except Exception as e:
        print("SendGrid error:", e)
        raise
