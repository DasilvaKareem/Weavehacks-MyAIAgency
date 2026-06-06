"""Video-generation tool for the Animator agent.

Uses Gemini's Veo model to turn a text prompt into a real short MP4, saved under
the local `generated/` dir. Veo is a LONG-RUNNING operation (typically 1-3
minutes) and a paid generation, unlike the synchronous image model. The agent
calls this like any other tool; the saved path is surfaced to the game so the
chat panel can offer to play it.

Graceful: if google-genai isn't installed, no key is set, or the key lacks Veo
access, load_animator_tools returns [] and the Animator falls back to describing
the animation in words.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config

# Veo model verified available with the project key. Override via env.
VIDEO_MODEL = os.getenv("COMPANY_AI_VIDEO_MODEL", "veo-3.1-fast-generate-preview")
# How long to wait for a render before giving up (seconds), and poll interval.
VIDEO_TIMEOUT_S = config._float("COMPANY_AI_VIDEO_TIMEOUT", 300.0)
VIDEO_POLL_S = config._float("COMPANY_AI_VIDEO_POLL", 10.0)


def output_dir() -> Path:
    """Where generated videos land: the active company's assets/ folder (override
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
    return "-".join(filter(None, keep.split("-")))[:n] or "video"


def generate_video_file(prompt: str) -> tuple[str | None, str]:
    """Generate one video from `prompt`. Returns (saved_path_or_None, message).

    Blocks while Veo renders (minutes). Safe to call from the chat worker thread.
    """
    key = _api_key()
    if not key:
        return None, "[no API key set; cannot generate video]"
    try:
        from google import genai
    except ImportError:
        return None, "[google-genai not installed; cannot generate video]"

    try:
        client = genai.Client(api_key=key)
        op = client.models.generate_videos(model=VIDEO_MODEL, prompt=prompt)
    except Exception as exc:
        return None, f"[video generation failed to start: {type(exc).__name__}: {exc}]"

    # Poll the long-running operation until it finishes or we time out.
    deadline = time.monotonic() + VIDEO_TIMEOUT_S
    try:
        while not op.done:
            if time.monotonic() > deadline:
                return None, f"[video timed out after {VIDEO_TIMEOUT_S:.0f}s; try a shorter prompt]"
            time.sleep(VIDEO_POLL_S)
            op = client.operations.get(op)
    except Exception as exc:
        return None, f"[video generation failed while rendering: {type(exc).__name__}: {exc}]"

    # Pull the rendered clip out of the finished operation and save it.
    try:
        videos = op.response.generated_videos or []
        if not videos:
            return None, "[model returned no video]"
        vid = videos[0].video
        client.files.download(file=vid)          # populates vid.video_bytes
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = output_dir() / f"{ts}-{_slug(prompt)}.mp4"
        data = getattr(vid, "video_bytes", None)
        if data:
            path.write_bytes(data)
        else:
            vid.save(str(path))                  # SDK fallback saver
        return str(path), f"Generated video saved to {path}"
    except Exception as exc:
        return None, f"[failed to save video: {type(exc).__name__}: {exc}]"


def load_animator_tools(author_id: str | None = None,
                        author_name: str = "") -> list:
    """Return the video-generation tool as a LangChain tool, or [] if unavailable.

    `author_id`/`author_name` (the calling agent) are stamped on the clip when it's
    registered into the shared company drive, so the video is catalogued with
    provenance rather than vanishing as a loose host path.
    """
    if not _api_key():
        return []
    try:
        from langchain_core.tools import tool
    except ImportError:
        return []

    @tool
    def generate_video(prompt: str) -> str:
        """Generate a real short video clip from a detailed text prompt, saved as MP4.

        Use this when the CEO asks you to animate, produce a video, motion graphic,
        ad clip, intro, or any moving visual. Write a rich, specific prompt
        (subject, action/motion, camera movement, style, mood, setting). Rendering
        takes a few minutes. The tool returns the saved file path; tell the CEO what
        you created and where it was saved.
        """
        path, message = generate_video_file(prompt)
        if path:
            from .company_fs import register_asset
            vpath = register_asset(path, author_id, author_name)
            if vpath:
                message += f" (on the company drive at {vpath})"
        return message

    return [generate_video]
