"""Cast a cutscene with the player's REAL company, and voice it.

Turns the 10 generic cutscenes (game/cutscenes.py) into films of YOUR company:

  * recast(scene, ceo_profile, agents) — swaps the generic actors for the real CEO
    and hired agents (their names, saved looks, and avatar models), then remaps the
    script's speaker labels to the real names. Returns {speaker -> tts voice}.
  * voice_track(scene, voices, out_path) — synthesizes ONE composite WAV for the
    whole cut: every scripted line spoken at its start time in that speaker's unique
    Gemini voice. record()/encode() muxes it under the video via scene.music.

Both degrade gracefully: with no save (no profile / no hires) the scene keeps its
generic actors, and lines just get the narrator/CEO/derived voices.
"""
from __future__ import annotations

import array
import json

from . import roster

# Fixed voices for the two non-employee speakers (must be in tts.GEMINI_VOICES).
NARRATOR_VOICE = "Charon"        # deep, steady voiceover
CEO_VOICE = "Puck"               # the player's own voice in the cuts


def _valid_model(m):
    return m if (m and m.endswith((".gltf", ".glb"))) else None


def _apply(ch, look) -> None:
    try:
        roster.apply_look(ch, look or {})
    except Exception:
        pass


def recast(scene, ceo_profile, agents) -> dict:
    """Replace generic actors with the real CEO + hires and remap speaker labels.
    Returns {speaker_name: voice} for voice_track()."""
    pool = [a for a in (agents or []) if getattr(a, "status", "") != "fired"]
    rename: dict = {}                # old actor name -> real name
    voices: dict = {}                # speaker name -> tts voice

    def take_for(role: str):
        for i, a in enumerate(pool):     # prefer a hire whose role matches the actor
            if role and role.lower() in (a.role or "").lower():
                return pool.pop(i)
        return pool.pop(0) if pool else None

    for actor in scene.actors:
        ch = actor.ch
        old = ch.name
        is_ceo = (getattr(ch, "role", "") == "CEO") or (old == "You (CEO)")
        if is_ceo:
            new = (ceo_profile or {}).get("name") or old
            if ceo_profile:
                m = _valid_model(ceo_profile.get("model"))
                if m:
                    ch.model = m
                _apply(ch, ceo_profile)
                ch.name = new
            # The script labels the CEO "You"/"CEO"; alias them all to the real name
            # so those subtitles + voices resolve to the CEO.
            for alias in ("You", "You (CEO)", "CEO", old):
                rename.setdefault(alias, new)
            voices[new] = CEO_VOICE
            continue
        a = take_for(getattr(ch, "role", ""))
        if a is None:
            continue                     # more actors than hires → keep this one generic
        m = _valid_model(a.char_model)
        if m:
            ch.model = m
        if a.char_appearance:
            try:
                _apply(ch, json.loads(a.char_appearance))
            except ValueError:
                pass
        ch.name = a.name
        rename[old] = a.name
        from backend.tts import voice_for
        voices[a.name] = voice_for(a.id)

    for ln in getattr(scene, "script", []):
        if ln.speaker in rename:
            ln.speaker = rename[ln.speaker]
    for c in getattr(scene, "captions", []):
        if getattr(c, "speaker", "") in rename:
            c.speaker = rename[c.speaker]
    return voices


def voice_track(scene, voices, out_path, gap: float = 0.35, lead: float = 0.5) -> str | None:
    """Synthesize the script to ONE composite WAV — and RE-TIME the script so lines
    never overlap. Authored `dur`s are only guesses; real TTS audio is usually
    longer, so we measure each clip and lay the lines back-to-back (with `gap` of
    silence between them, after a `lead` lead-in). Each Line's t/dur is rewritten to
    its real spoken time so the subtitles match the audio; call fit_shots(scene)
    afterwards to stretch the video over the (possibly longer) timeline.

    Returns the WAV path, or None (no lines / TTS unavailable)."""
    lines = [ln for ln in getattr(scene, "script", []) if (ln.text or "").strip()]
    if not lines:
        return None
    from backend import tts
    sr = tts.SAMPLE_RATE

    # 1) synthesize every line, measuring its REAL duration, sequencing as we go.
    placed = []                                  # (start_sample, pcm_bytes)
    cursor = lead
    for ln in lines:
        if ln.kind == "narrate":
            voice = voices.get(ln.speaker) or NARRATOR_VOICE
        else:
            voice = voices.get(ln.speaker) or tts.voice_for(ln.speaker or "narrator")
        try:
            pcm = tts.synth_pcm(ln.text.strip(), voice)
        except Exception as exc:
            if not getattr(voice_track, "_warned", False):
                print(f"[cast] TTS failed ({exc}) — voice track will be silent")
                voice_track._warned = True
            continue
        if not pcm:
            continue
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]   # whole int16 samples
        dur = (len(pcm) // 2) / sr               # the line's ACTUAL spoken length
        ln.t = round(cursor, 3)                  # re-time the subtitle to the audio
        ln.dur = round(dur, 3)
        placed.append((int(cursor * sr), pcm))
        cursor += dur + gap                       # next line starts AFTER this one

    if not placed:
        return None

    # 2) lay the (now non-overlapping) clips into one buffer.
    n = int(cursor * sr) + sr                     # +1s tail
    mix = array.array("h", bytes(2 * n))
    for off, pcm in placed:
        seg = array.array("h")
        seg.frombytes(pcm)
        end = min(n, off + len(seg))
        mix[off:end] = seg[: end - off]
    with open(out_path, "wb") as fh:
        fh.write(tts.pcm_to_wav(mix.tobytes(), sr))
    return out_path


def fit_shots(scene) -> None:
    """After re-timing, stretch the last camera shot so the video covers the full
    (possibly longer) audio timeline instead of cutting off or freezing on no shot."""
    total = scene.total()
    if not scene.shots:
        return
    last = max(scene.shots, key=lambda s: s.end)
    if last.end < total:
        last.dur += (total - last.end)
