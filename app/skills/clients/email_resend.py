"""Resend email skill."""
from __future__ import annotations

from typing import Any

from ..base import SkillError, require_env
from .http_util import arequest_json


async def email_send(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    env = require_env("RESEND_API_KEY")
    to = args.get("to")
    subject = (args.get("subject") or "").strip()
    html = args.get("html") or args.get("text") or ""
    from_addr = (args.get("from") or "").strip()
    import os

    if not from_addr:
        from_addr = (os.environ.get("RESEND_FROM") or "").strip()
    if not to or not subject or not from_addr:
        raise SkillError("invalid_args", "need_to_subject_from")
    if isinstance(to, str):
        to = [to]
    status, data = await arequest_json(
        "POST",
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {env['RESEND_API_KEY']}"},
        body={"from": from_addr, "to": to, "subject": subject, "html": html},
        timeout=30.0,
    )
    if status not in (200, 201):
        raise SkillError("email_send_failed", f"resend_{status}:{data}")
    return {"provider": "resend", "id": (data or {}).get("id"), "to": to, "subject": subject}
