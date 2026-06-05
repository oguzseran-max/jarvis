"""
JARVIS Camera Awareness — see through the webcam (on-demand, single frame).

Unlike screen.py (which captures the desktop server-side via `screencapture`),
the webcam lives in the browser. So the flow is a round-trip:

  1. The server asks the frontend for ONE frame  ({"type": "capture_camera"}).
  2. The frontend calls getUserMedia, grabs a single JPEG, releases the camera,
     and sends it back     ({"type": "camera_frame", "data": "<base64>"}).
  3. The server hands that frame to the Claude vision API for a description.

Privacy by design: there is no continuous feed. The camera is opened, one frame
is taken, and the stream is stopped immediately — every time.
"""

import logging

log = logging.getLogger("jarvis.camera")


_LANG = {"fr": ("French", "mon amour"), "tr": ("Turkish", "canım")}


async def describe_camera(anthropic_client, frame_b64: str, lang: str = "en") -> str:
    """Describe a single webcam frame via the Claude vision API.

    Args:
        anthropic_client: AsyncAnthropic client.
        frame_b64: base64-encoded JPEG (no data-URL prefix).
        lang: 'fr'/'tr' to reply in that language; otherwise English.

    Returns:
        A short, spoken-style description, or a polite failure line.
    """
    if not frame_b64:
        return "I couldn't get a camera frame, sir."
    if not anthropic_client:
        return "Camera captured, but I've no vision model configured, sir."

    name, honorific = _LANG.get(lang, ("English", "sir"))
    lang_line = (f" Reply ONLY in {name}, addressing the user as '{honorific}'."
                 if lang in _LANG else " Address the user as 'sir'.")
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "You are JARVIS (called 'Marion' in French, 'Eda' in Turkish) looking at the user through "
                "their webcam. Give a quick, candid read on THEM, like a witty British "
                "butler: (1) their outfit/look — a real verdict (sharp, casual, a bit "
                "much, mismatched, dressed to impress…); (2) how they seem — fresh and "
                "on form, tired, stressed, relaxed, under the weather. Be warm, "
                "observant, economical. You MAY drop the occasional gentle SARCASTIC "
                "jab at their expense to make them smile — teasing, playful, never "
                "cruel. 1-3 sentences, spoken style, no markdown. If the frame is too "
                "dark or no one is in it, say so plainly." + lang_line
            ),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": frame_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Look at me — how do I look right now? My outfit, and my general state (on form, tired…). A little sass is welcome.",
                    },
                ],
            }],
        )
        return response.content[0].text
    except Exception as e:
        log.warning(f"Camera vision call failed: {e}")
        return "I had trouble making sense of the camera image, sir."
