# mailer.py (Brevo SMTP 버전, CID 로고 inline 유지)

import os
from typing import List, Optional
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage


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


def _attach_inline_logo(msg_root: MIMEMultipart, cid: str = "acuvue_logo") -> None:
    """
    HTML에서 <img src="cid:acuvue_logo"> 로 표시될 inline 로고 첨부
    Outlook 호환 위해 Content-ID / Content-Disposition 세팅
    """
    logo_path = _find_logo_path()
    if not logo_path:
        print("[mailer] WARNING: acuvue_logo.png not found (assets/ or root). Logo will not show in Outlook.")
        return

    with open(logo_path, "rb") as f:
        img = MIMEImage(f.read(), _subtype="png")

    # Content-ID는 <> 로 감싸는 게 관례
    img.add_header("Content-ID", f"<{cid}>")
    img.add_header("Content-Disposition", "inline", filename=os.path.basename(logo_path))

    msg_root.attach(img)
    print(f"[mailer] inline logo attached: {logo_path} (cid={cid})")


def send_email_html(
    subject: str,
    html_body: str,
    from_addr: str,
    to_addrs: List[str],
):
    """
    Brevo SMTP로 HTML 메일 발송 (CID inline 로고 포함)

    필요 환경변수 (GitHub Secrets 권장):
      - BREVO_SMTP_USER: Brevo 로그인 이메일
      - BREVO_SMTP_PASSWORD: Brevo SMTP Key (비밀번호 역할)

    선택 환경변수:
      - FROM_EMAIL: 표시될 From 주소 (없으면 from_addr 또는 BREVO_SMTP_USER 사용)
      - FROM_NAME: 표시될 From 이름 (없으면 기본 "News Bot")
      - OVERRIDE_TO: 테스트용 수신자 강제 (콤마 구분)
      - BREVO_SMTP_HOST: 기본 smtp-relay.brevo.com
      - BREVO_SMTP_PORT: 기본 587
    """

    smtp_user = os.getenv("BREVO_SMTP_USER")
    smtp_pass = os.getenv("BREVO_SMTP_PASSWORD")
    if not smtp_user or not smtp_pass:
        raise RuntimeError("BREVO_SMTP_USER / BREVO_SMTP_PASSWORD 환경변수가 설정되어 있지 않습니다.")

    smtp_host = os.getenv("BREVO_SMTP_HOST", "smtp-relay.brevo.com")
    smtp_port = int(os.getenv("BREVO_SMTP_PORT", "587"))

    # ✅ From 처리: (환경변수 우선) FROM_EMAIL > from_addr > smtp_user
    actual_from_email = os.getenv("FROM_EMAIL") or from_addr or smtp_user
    from_name = os.getenv("FROM_NAME", "News Bot")

    # ✅ To 처리: 테스트용 override 가능
    override_to = os.getenv("OVERRIDE_TO")
    if override_to:
        recipients = [x.strip() for x in override_to.split(",") if x.strip()]
    else:
        recipients = to_addrs

    if not recipients:
        raise RuntimeError("수신자(to_addrs)가 비어 있습니다.")

    # ===== MIME 구성 (inline 이미지 위해 mixed/related 구조) =====
    msg_root = MIMEMultipart("related")
    msg_root["Subject"] = subject
    msg_root["From"] = f"{from_name} <{actual_from_email}>"
    msg_root["To"] = ", ".join(recipients)

    msg_alt = MIMEMultipart("alternative")
    msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg_root.attach(msg_alt)

    # ✅ 로고 CID 첨부
    _attach_inline_logo(msg_root, cid="acuvue_logo")

    # ===== SMTP 발송 =====
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(actual_from_email, recipients, msg_root.as_string())

        print("Brevo SMTP status: SENT")
        print("SMTP Host:", smtp_host, "Port:", smtp_port)
        print("From:", actual_from_email)
        print("To:", recipients)

    except Exception as e:
        print("Brevo SMTP error:", e)
        raise
