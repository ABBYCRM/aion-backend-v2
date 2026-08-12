"""Hedra (https://www.hedra.com) client — v3 API.

Hedra is a unified API for 30+ image models and 20+ video models.
Per https://www.hedra.com/developers:
  - Base: https://api.hedra.com
  - Auth: `Authorization: Key <key_id>:<secret>` (NOT X-Api-Key)
  - Submit: POST /v3/models/<model_slug> with JSON body {"input": {...}}
  - Poll:   GET /v3/models/<model_slug>/jobs/<job_id>
  - Output: GET <output_url> from the job response

Available model slugs (sample, from https://www.hedra.com/all-models):
  Image:  nano-banana-2, nano-banana-pro, nano-banana, imagen4,
          seedream-5.0-lite, seedream-4.5, seedream-4.0, dreamina-3.1,
          gpt-image-2-high, gpt-image-2-medium, gpt-image-2-low, gpt-image-1.5,
          flux-2-flex, flux-2-max, flux-2-pro, flux-kontext-max,
          flux-kontext-pro, flux-1.1-ultra, flux-1.1-pro
  Video:  kling-v3, kling-2.5-turbo, veo-3.1, sora-2-pro,
          grok-imagine, hedra-avatar, omnia, seedance-2.0, seedance-1.5-pro,
          omnihuman-1.5, seedance-2.5
  Audio:  (Hedra has voice/TTS models too — see hedra.com/models)

This client is REST-only (no SDK on PyPI). It uses stdlib urllib via
http_util. The key is read from settings.hedra_api_key.

The two main skills we expose:
  - hedra.image — text-to-image, any of the 18+ image model slugs
  - hedra.video — text-to-video AND image-to-video, any video model

Both are submit-and-poll. poll=false returns the job_id immediately;
poll=true blocks until status in (succeeded, failed, cancelled) or
poll_timeout_seconds elapses.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

from .http_util import arequest_json

logger = logging.getLogger(__name__)

HEDRA_BASE = "https://api.hedra.com"
HEDRA_TIMEOUT = 30.0


class HedraError(Exception):
    """Base for Hedra client errors."""


class HedraAuthError(HedraError):
    """Invalid or missing API key."""


class HedraUpstreamError(HedraError):
    """Hedra returned 5xx or a non-parseable response."""


def _auth_header(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Key {api_key}",
        "Accept": "application/json",
        "User-Agent": "AION-HedraClient/1.0",
    }


# ---- v3 image generation ----

async def hedra_image(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Skill: hedra.image — generate an image using any Hedra-hosted model.

    Inputs:
      prompt (required): the text prompt
      model (optional, default "nano-banana-2"): one of
        nano-banana-2, nano-banana-pro, nano-banana, imagen4,
        seedream-5.0-lite, seedream-4.5, seedream-4.0, dreamina-3.1,
        gpt-image-2-high, gpt-image-2-medium, gpt-image-2-low, gpt-image-1.5,
        flux-2-flex, flux-2-max, flux-2-pro, flux-kontext-max,
        flux-kontext-pro, flux-1.1-ultra, flux-1.1-pro
      aspect_ratio (optional, default "1:1"): "1:1", "16:9", "9:16", "4:3", "3:4"
      n (optional, default 1): how many images to generate
      reference_url (optional): if set, the model uses this as a style/character
        reference. Behavior depends on the chosen model.
      poll (optional, default true): block until done
      poll_timeout_seconds (optional, default 120)

    Returns: {ok, job_id, model, status, image_url, image_urls, images, output}
    """
    from app.settings import settings as _settings
    api_key = _settings.hedra_api_key
    if not api_key:
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_not_configured",
                "error_message": "HEDRA_API_KEY not set on this environment"}

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "missing_required:prompt",
                "error_message": "prompt is required"}

    model = (args.get("model") or "nano-banana-2").strip()
    aspect_ratio = (args.get("aspect_ratio") or "1:1").strip()
    try:
        n = max(1, min(4, int(args.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1
    reference_url = (args.get("reference_url") or "").strip() or None
    poll = bool(args.get("poll", True))
    poll_timeout = max(10, min(600, int(args.get("poll_timeout_seconds") or 120)))

    body: dict[str, Any] = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "n": n,
        }
    }
    if reference_url:
        body["input"]["reference_url"] = reference_url

    status, payload = await _hedra_post(api_key, f"/v3/models/{quote(model, safe='')}", body)
    if status in (401, 403):
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_auth",
                "error_message": f"auth failed (HTTP {status}); verify HEDRA_API_KEY",
                "raw": str(payload)[:200]}
    if status >= 500:
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_upstream",
                "error_message": f"hedra returned {status}",
                "raw": str(payload)[:300]}
    if status >= 400:
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_rejected",
                "error_message": f"hedra rejected request (HTTP {status})",
                "raw": str(payload)[:500]}
    if not isinstance(payload, dict):
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_parse",
                "error_message": "non-JSON response from Hedra",
                "raw": str(payload)[:500]}

    job_id = payload.get("id") or payload.get("job_id")
    if not job_id:
        return {"ok": False, "skill_id": "hedra.image",
                "error_code": "hedra_no_job_id",
                "error_message": "Hedra response missing job id",
                "raw": json.dumps(payload)[:500]}

    if not poll:
        return {
            "ok": True,
            "skill_id": "hedra.image",
            "job_id": str(job_id),
            "model": model,
            "status": payload.get("status", "queued"),
        }

    final = await _hedra_poll(api_key, model, str(job_id), poll_timeout)
    final["model"] = model
    return final


# ---- v3 video generation (text + image-to-video) ----

async def hedra_video(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Skill: hedra.video — generate a video using any Hedra-hosted video model.

    Inputs:
      prompt (required): the text prompt
      model (optional, default "kling-v3"): one of
        kling-v3, kling-2.5-turbo, veo-3.1, sora-2-pro,
        grok-imagine, hedra-avatar, omnia, seedance-2.0, seedance-1.5-pro,
        omnihuman-1.5, seedance-2.5
      start_image_url (optional): if set, image-to-video mode.
      aspect_ratio (optional, default "16:9")
      duration_ms (optional, default 5000)
      quality (optional, default "standard"): "draft", "standard", "pro" — model-dependent
      poll (optional, default true)
      poll_timeout_seconds (optional, default 180)

    Returns: {ok, job_id, model, status, video_url, video_urls, output}
    """
    from app.settings import settings as _settings
    api_key = _settings.hedra_api_key
    if not api_key:
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_not_configured",
                "error_message": "HEDRA_API_KEY not set on this environment"}

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "missing_required:prompt",
                "error_message": "prompt is required"}

    model = (args.get("model") or "kling-v3").strip()
    aspect_ratio = (args.get("aspect_ratio") or "16:9").strip()
    try:
        duration_ms = max(1000, min(30000, int(args.get("duration_ms") or 5000)))
    except (TypeError, ValueError):
        duration_ms = 5000
    quality = (args.get("quality") or "standard").strip()
    start_image_url = (args.get("start_image_url") or args.get("input_reference") or "").strip() or None
    poll = bool(args.get("poll", True))
    poll_timeout = max(10, min(600, int(args.get("poll_timeout_seconds") or 180)))

    body: dict[str, Any] = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "duration_ms": duration_ms,
            "quality": quality,
        }
    }
    if start_image_url:
        body["input"]["start_image_url"] = start_image_url

    status, payload = await _hedra_post(api_key, f"/v3/models/{quote(model, safe='')}", body)
    if status in (401, 403):
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_auth",
                "error_message": f"auth failed (HTTP {status}); verify HEDRA_API_KEY",
                "raw": str(payload)[:200]}
    if status >= 500:
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_upstream",
                "error_message": f"hedra returned {status}",
                "raw": str(payload)[:300]}
    if status >= 400:
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_rejected",
                "error_message": f"hedra rejected request (HTTP {status})",
                "raw": str(payload)[:500]}
    if not isinstance(payload, dict):
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_parse",
                "error_message": "non-JSON response from Hedra",
                "raw": str(payload)[:500]}

    job_id = payload.get("id") or payload.get("job_id")
    if not job_id:
        return {"ok": False, "skill_id": "hedra.video",
                "error_code": "hedra_no_job_id",
                "error_message": "Hedra response missing job id",
                "raw": json.dumps(payload)[:500]}

    if not poll:
        return {
            "ok": True,
            "skill_id": "hedra.video",
            "job_id": str(job_id),
            "model": model,
            "status": payload.get("status", "queued"),
        }

    final = await _hedra_poll(api_key, model, str(job_id), poll_timeout)
    final["model"] = model
    return final


# ---- low-level helpers ----

async def _hedra_post(api_key: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    """POST to Hedra. Returns (status_code, parsed_json_or_text)."""
    import asyncio
    return await asyncio.to_thread(
        _hedra_post_sync, api_key, path, body
    )


def _hedra_post_sync(api_key: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    import urllib.request
    import urllib.error
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{HEDRA_BASE}{path}",
        data=data,
        method="POST",
        headers={
            **({"Content-Type": "application/json"} if body is not None else {}),
            **_auth_header(api_key),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HEDRA_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, {"text": raw[:8000]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw) if raw else {"error": e.reason}
        except json.JSONDecodeError:
            return e.code, {"error": raw[:2000] or e.reason}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason if hasattr(e, "reason") else e)}


async def _hedra_poll(api_key: str, model: str, job_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Poll Hedra job until status in (succeeded, failed, cancelled) or timeout."""
    import asyncio
    deadline = time.time() + timeout_seconds
    last_status = "unknown"
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            status, payload = await asyncio.to_thread(
                _hedra_get_sync, api_key, f"/v3/models/{quote(model, safe='')}/jobs/{quote(job_id, safe='')}"
            )
        except Exception as exc:
            return {"ok": False, "job_id": job_id, "status": last_status,
                    "error_code": "hedra_poll_error",
                    "error_message": str(exc)[:200]}
        if status in (401, 403):
            return {"ok": False, "job_id": job_id, "status": last_status,
                    "error_code": "hedra_auth",
                    "error_message": f"auth failed (HTTP {status})"}
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False, "job_id": job_id, "status": last_status,
                    "error_code": "hedra_poll_upstream",
                    "error_message": f"poll returned {status}",
                    "raw": str(payload)[:300]}
        last_payload = payload
        last_status = payload.get("status", last_status)
        if last_status in ("succeeded", "completed", "done"):
            return {
                "ok": True,
                "job_id": job_id,
                "status": last_status,
                "output": payload.get("output"),
                "output_url": _extract_output_url(payload, "video"),
                "image_url": _extract_output_url(payload, "image"),
                "image_urls": _extract_output_urls(payload, "image"),
                "video_url": _extract_output_url(payload, "video"),
                "video_urls": _extract_output_urls(payload, "video"),
            }
        if last_status in ("failed", "error", "cancelled"):
            return {"ok": False, "job_id": job_id, "status": last_status,
                    "error_code": "hedra_job_failed",
                    "error_message": payload.get("error") or payload.get("failure_reason") or "Hedra job failed",
                    "raw": json.dumps(payload)[:500]}
        await asyncio.sleep(2.0)
    return {"ok": False, "job_id": job_id, "status": last_status,
            "error_code": "hedra_poll_timeout",
            "error_message": f"job did not complete in {timeout_seconds}s",
            "last_payload": json.dumps(last_payload)[:500]}


def _hedra_get_sync(api_key: str, path: str) -> tuple[int, Any]:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        f"{HEDRA_BASE}{path}",
        method="GET",
        headers=_auth_header(api_key),
    )
    try:
        with urllib.request.urlopen(req, timeout=HEDRA_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, {"text": raw[:8000]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw) if raw else {"error": e.reason}
        except json.JSONDecodeError:
            return e.code, {"error": raw[:2000] or e.reason}
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason if hasattr(e, "reason") else e)}


def _extract_output_url(payload: dict[str, Any], kind: str) -> str | None:
    """Pull a single output URL of the given kind (image|video) from a Hedra job payload."""
    out = payload.get("output")
    if isinstance(out, dict):
        urls = out.get("urls") or out.get(f"{kind}_urls") or []
        if isinstance(urls, list) and urls:
            return str(urls[0])
        single = out.get("url") or out.get(f"{kind}_url")
        if single:
            return str(single)
    if isinstance(out, str):
        return out
    direct = payload.get(f"{kind}_url")
    if direct:
        return str(direct)
    return None


def _extract_output_urls(payload: dict[str, Any], kind: str) -> list[str]:
    out = payload.get("output")
    if isinstance(out, dict):
        urls = out.get("urls") or out.get(f"{kind}_urls") or []
        if isinstance(urls, list):
            return [str(u) for u in urls if u]
    return []
