"""Image-generation tool for the Graphic Designer agent.

Uses the google-genai SDK's image-capable Gemini model to turn a text prompt
into a real PNG, saved under a local `generated/` dir. The agent calls this like
any other tool; the saved path is surfaced to the game so the chat panel can show
the artwork.

Graceful: if google-genai isn't installed or no key is set, load_designer_tools
returns [] and the Designer falls back to describing the design in words.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config

# Image model verified working with the project key (returns inline PNG bytes).
IMAGE_MODEL = os.getenv("COMPANY_AI_IMAGE_MODEL", "gemini-2.5-flash-image")


def output_dir() -> Path:
    """Where generated images land: the active company's assets/ folder (override
    with COMPANY_AI_IMAGE_DIR)."""
    override = os.getenv("COMPANY_AI_IMAGE_DIR", "")
    if override:
        d = Path(override)
    else:
        try:
            from . import workspace
            d = workspace.asset_dir(workspace.active_slug())
        except Exception:
            d = Path.cwd() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _api_key() -> str | None:
    for name in config.API_KEY_ENVS:
        key = os.getenv(name)
        if key:
            return key
    return None


def _slug(text: str, n: int = 32) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(filter(None, keep.split("-")))[:n] or "image"


def generate_image_file(prompt: str) -> tuple[str | None, str]:
    """Generate one image from `prompt`. Returns (saved_path_or_None, message)."""
    key = _api_key()
    if not key:
        return None, "[no API key set; cannot generate image]"
    try:
        from google import genai
    except ImportError:
        return None, "[google-genai not installed; cannot generate image]"

    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=IMAGE_MODEL, contents=prompt)
    except Exception as exc:  # network / quota / model errors
        return None, f"[image generation failed: {type(exc).__name__}: {exc}]"

    for cand in (resp.candidates or []):
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                ts = time.strftime("%Y%m%d-%H%M%S")
                path = output_dir() / f"{ts}-{_slug(prompt)}.png"
                path.write_bytes(inline.data)
                return str(path), f"Generated image saved to {path}"
    return None, "[model returned no image data]"


def load_designer_tools(author_id: str | None = None,
                        author_name: str = "") -> list:
    """Return the image-generation tool as a LangChain tool, or [] if unavailable.

    `author_id`/`author_name` (the calling agent) are stamped on the asset when
    it's registered into the shared company drive, so the artwork is catalogued
    with provenance rather than vanishing as a loose host path.
    """
    if not _api_key():
        return []
    try:
        from langchain_core.tools import tool
    except ImportError:
        return []

    @tool
    def generate_image(prompt: str) -> str:
        """Generate a real image from a detailed text prompt and save it as a PNG.

        Use this whenever the CEO asks you to design, draw, mock up, or produce any
        visual — a logo, icon, poster, UI mockup, illustration, banner, etc. Write a
        rich, specific prompt (subject, style, colors, composition, background). The
        tool returns the saved file path; tell the CEO what you created and where it
        was saved.
        """
        path, message = generate_image_file(prompt)
        if path:
            from .company_fs import register_asset
            vpath = register_asset(path, author_id, author_name)
            if vpath:
                message += f" (on the company drive at {vpath})"
        return message

    return [generate_image]
