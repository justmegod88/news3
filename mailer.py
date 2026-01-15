import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import json


def send_email_html(subject, html_body, from_addr, to_addrs):
    """
    ✅ SendGrid 메일 발송 + 디버그 강화 버전
    - status_code, headers(x-message-id 포함), body, to/from 로그
    - 202인데 실제로 안 오는 경우 원인 파악용
    """
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        print("❌ SENDGRID_API_KEY 환경변수가 없습니다.")
        return

    # 수신자 목록 처리
    recipients = []
    if isinstance(to_addrs, str):
        recipients = [to_addrs]
    elif isinstance(to_addrs, list):
        recipients = to_addrs
    else:
        print("❌ to_addrs 형식이 잘못되었습니다:", to_addrs)
        return

    # 발신자 확인
    actual_from = from_addr or os.getenv("DEFAULT_FROM_EMAIL", "")
    if not actual_from:
        print("❌ 발신자(from) 주소가 없습니다.")
        return

    message = Mail(
        from_email=actual_from,
        to_emails=recipients,
        subject=subject,
        html_content=html_body,
    )

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)

        print("📤 [SendGrid] 메일 발송 요청 완료")
        print("  ▶ Status:", response.status_code)
        print("  ▶ From:", actual_from)
        print("  ▶ To:", recipients)
        print("  ▶ Subject:", subject)
        print("  ▶ HTML length:", len(html_body))

        # ✅ Header 상세 (x-message-id 확인용)
        try:
            headers_dict = dict(response.headers)
            print("  ▶ Headers:", json.dumps(headers_dict, ensure_ascii=False))
            if "x-message-id" in headers_dict:
                print("  ▶ x-message-id:", headers_dict["x-message-id"])
        except Exception as e:
            print("  ⚠️ 헤더 출력 오류:", e)

        # ✅ Body (에러 설명 등)
        try:
            body_text = (
                response.body.decode("utf-8", errors="ignore")
                if hasattr(response.body, "decode")
                else str(response.body)
            )
            if body_text:
                print("  ▶ Response body snippet:", body_text[:300])
        except Exception as e:
            print("  ⚠️ Body 출력 오류:", e)

        # ✅ 성공/실패 표시
        if response.status_code == 202:
            print("✅ SendGrid가 요청을 정상 접수했습니다. (202)")
        else:
            print("⚠️ SendGrid 응답 코드:", response.status_code)

    except Exception as e:
        print("❌ SendGrid 메일 발송 중 오류 발생:", repr(e))
