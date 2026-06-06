"""Image bridge for the Blogger agent — gets a generated picture into the live site.

The Blogger publishes a real website from a Daytona cloud sandbox (it builds the
files there and `serve_site` hands back a public URL — see daytona_tools.py). It
also generates artwork with the Designer's image tool (designer_tools.py). But
those two live in different places: generate_image saves a PNG on the HOST, while
the site is served from the REMOTE sandbox, so a host path is invisible to the
published page.

This module is the one piece that closes that gap: `add_blog_image` generates the
image on the host, then uploads the bytes into the sandbox's filesystem (reusing
the *same* cached sandbox the blogger is building in), so the page can reference
it with an ordinary relative <img src>. The blogger still gets the raw Daytona
and Designer tools separately; this just lets them work together.

Only meaningful when BOTH Daytona and image generation are configured; otherwise
load_blogger_tools() returns [] and the blogger falls back to a text-only or
image-less site.
"""
from __future__ import annotations

from . import daytona_tools
from .designer_tools import _api_key, _slug, generate_image_file

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def load_blogger_tools(author_id: str | None = None,
                       author_name: str = "") -> list:
    """Return the host→sandbox image bridge, or [] if it can't work here.

    The bridge needs a sandbox to upload into (Daytona) and a way to make the
    image (an API key); without either there's nothing to bridge. `author_id`/
    `author_name` (the calling agent) are stamped on the image when it's also
    catalogued into the shared company drive.
    """
    if not (daytona_tools.is_configured() and _api_key()):
        return []

    from langchain_core.tools import tool

    @tool
    def add_blog_image(prompt: str, filename: str = "") -> str:
        """Generate an image and place it in your Daytona site so the PUBLISHED page
        can display it.

        Use this for blog hero images, post illustrations, and banners — not the
        plain generate_image tool, whose file stays on the host and never reaches
        the live site. Give a rich visual prompt (subject, style, colors, mood).
        `filename` is optional (e.g. 'hero.png'); it defaults to one derived from
        the prompt. The image is uploaded into the same working directory your
        site is served from, so embed it with a plain relative path —
        <img src="hero.png" alt="...">. Returns the filename to reference.
        """
        # 1) make the PNG on the host via Gemini (same path generate_image uses)
        path, message = generate_image_file(prompt)
        if not path:
            return message  # generation failed — surfaces the actual reason

        # Catalogue the host image on the shared company drive too, so the visual
        # is browsable alongside the rest of the company's work (the live <img>
        # still resolves from the sandbox copy uploaded below).
        from .company_fs import register_asset
        register_asset(path, author_id, author_name)

        # 2) pick a sandbox-relative destination filename
        name = filename.strip() or f"{_slug(prompt)}.png"
        if not name.lower().endswith(_IMG_EXTS):
            name += ".png"

        # 3) upload the bytes into the (shared, cached) sandbox the blogger builds
        #    in, so a relative <img src> resolves against the served directory.
        try:
            with open(path, "rb") as f:
                data = f.read()
            sandbox = daytona_tools._get_sandbox()
            sandbox.fs.upload_file(data, name)
        except Exception as exc:
            return (f"[generated {path} on the host, but uploading it into the "
                    f"sandbox failed: {exc}]")

        return (f"Added image to the site as '{name}' ({len(data)} bytes). "
                f'Embed it in your HTML with <img src="{name}" alt="...">.')

    return [add_blog_image]
