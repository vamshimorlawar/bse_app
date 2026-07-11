"""Thin wrapper around the Telegram Bot API for sending alert messages."""

import os

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotConfigured(Exception):
    pass


def send_message(chat_id: str, text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN is not set")

    resp = requests.post(
        TELEGRAM_API.format(token=token),
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")


def format_alert(announcement: dict) -> str:
    company = announcement.get("company") or announcement.get("scrip_code")
    category = announcement.get("category") or "Announcement"
    subcategory = announcement.get("subcategory")
    subject = announcement.get("subject") or announcement.get("headline") or "(no subject)"
    date = announcement.get("date") or ""
    pdf_url = announcement.get("pdf_url")
    detail_url = announcement.get("detail_url")

    lines = [
        f"<b>{_escape(company)}</b> ({announcement.get('scrip_code')})",
        f"{_escape(category)}" + (f" / {_escape(subcategory)}" if subcategory else ""),
        _escape(subject),
        date,
    ]
    if pdf_url:
        lines.append(f'<a href="{pdf_url}">PDF attachment</a>')
    if detail_url:
        lines.append(f'<a href="{detail_url}">Stock page</a>')

    return "\n".join(line for line in lines if line)


def _escape(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
