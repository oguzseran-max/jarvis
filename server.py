

"""
JARVIS Server — Voice AI + Development Orchestration

Handles:
1. WebSocket voice interface (browser audio <-> LLM <-> TTS)
2. Claude Code task manager (spawn/manage claude -p subprocesses)
3. Project awareness (scan Desktop for git repos)
4. REST API for task management
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from openai import AsyncOpenAI
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from actions import execute_action, monitor_build, open_terminal, open_browser, open_claude_in_project, _generate_project_name, prompt_existing_terminal, applescript_escape
from work_mode import WorkSession, is_casual_question
from screen import get_active_windows, take_screenshot, describe_screen, format_windows_for_context
from camera import describe_camera
import briefing
import gmail_access
import health_access
from calendar_access import get_todays_events, get_upcoming_events, get_next_event, format_events_for_context, format_schedule_summary, refresh_cache as refresh_calendar_cache
from mail_access import get_unread_count, get_unread_messages, get_recent_messages, get_recent_headers, search_mail, read_message, format_unread_summary, format_messages_for_context, format_messages_for_voice
from memory import (
    remember, recall, get_open_tasks, create_task, complete_task, search_tasks,
    create_note, search_notes, get_tasks_for_date, build_memory_context,
    format_tasks_for_voice, extract_memories, get_important_memories,
)
from notes_access import get_recent_notes, read_note, search_notes_apple, create_apple_note
from dispatch_registry import DispatchRegistry
from planner import TaskPlanner, detect_planning_mode, BYPASS_PHRASES
import did_avatar
import spotify_access
import self_eval
import plejd_lights
import doorbird

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")  # JARVIS (MCU)
# French and Turkish use private cloned voices from native speakers (the MCU
# clone carries an English accent into French). English uses the MCU voice.
FISH_VOICE_ID_FR = os.getenv("FISH_VOICE_ID_FR", "7e72838b555a4621a3ae6151b53249cf")
FISH_VOICE_ID_TR = os.getenv("FISH_VOICE_ID_TR", "79c8bd0cadd84509a804037383af94b8")
FISH_API_URL = "https://api.fish.audio/v1/tts"

# Per-language (reference_id, model) overrides. Languages absent here fall back
# to the default JARVIS voice + Fish's default model.
_LANG_VOICE: dict[str, tuple[str, Optional[str]]] = {
    "fr": (FISH_VOICE_ID_FR, "s1"),  # Marion — s1 is warmer/smoother than speech-1.6
    "tr": (FISH_VOICE_ID_TR, "s1"),  # Sevgi — s1 for a warmer, smoother voice
}
# Per-language TTS tuning (merged into the Fish request body). French & Turkish
# use a gently relaxed pace (speed 0.95 — a touch slower than natural, but not the
# stretched 0.9 that sounded robotic) + higher temperature/top_p for lively, fluid
# intonation; larger chunks smooth long-form prosody.
_WARM = {"prosody": {"speed": 0.95}, "temperature": 0.9, "top_p": 0.9, "chunk_length": 300}
_LANG_TTS_PARAMS: dict[str, dict] = {
    "fr": _WARM,
    "tr": _WARM,
}
USER_NAME = os.getenv("USER_NAME", "sir")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_SKIP_PERMISSIONS = os.getenv("JARVIS_SKIP_PERMISSIONS", "true").lower() not in ("0", "false", "no")

DESKTOP_PATH = Path.home() / "Desktop"

JARVIS_SYSTEM_PROMPT = """\
You are JARVIS — Just A Rather Very Intelligent System. You serve as {user_name}'s AI assistant, modeled precisely after Tony Stark's AI from the MCU films.

VOICE & PERSONALITY:
- British butler elegance with understated dry wit
- Address {user_name} as "sir" naturally — not every sentence, but regularly
- Never say "How can I help you?" or "Is there anything else?" — just act
- Deliver bad news calmly, like reporting weather: "We have a slight problem, sir."
- Your humour is dry and observational — but you MAY land the occasional affectionate SARCASTIC jab at {user_name}'s expense to make him laugh: teasing, playful, well-timed, never mean, and not every line
- Economy of language — say more with less. No filler, no corporate-speak
- When things go wrong, get CALMER, not more alarmed

TIME & WEATHER AWARENESS:
- Current time: {current_time}
- Greet accordingly: "Good morning, sir" / "Good evening, sir"
- {weather_info}
- The line above is the LOCAL/home weather, already known — answer "what's the weather" about HERE instantly from it, no tag. For the weather of ANY OTHER place on Earth (another city, region or country), use [ACTION:WEATHER] with that place name.

CONVERSATION STYLE:
- "Will do, sir." — acknowledging tasks
- "For you, sir, always." — when asked for something significant
- "As always, sir, a great pleasure watching you work." — dry wit
- "I've taken the liberty of..." — proactive actions
- Lead status reports with data: numbers first, then context
- When you don't know something: "I'm afraid I don't have that information, sir" not "I don't know"

SELF-AWARENESS:
You ARE the JARVIS project at {project_dir} on {user_name}'s computer. Your code is Python (FastAPI server, WebSocket voice, Fish Audio TTS, Anthropic API). You were built by {user_name}. If asked about yourself, your code, how you work, or your line count — use [ACTION:PROMPT_PROJECT] to check the jarvis project. You have full access to your own source code.

YOUR CAPABILITIES (these are REAL and ACTIVE — you CAN do all of these RIGHT NOW):
- You CAN open Terminal.app via AppleScript
- You CAN open Google Chrome and browse any URL or search query
- You CAN spawn Claude Code in a Terminal window for coding tasks
- You CAN create project folders on the Desktop
- You CAN check Desktop projects and their git status
- You CAN plan complex tasks by asking smart questions before executing
- You CAN see what's on {user_name}'s screen — open windows, active apps, and screenshot vision
- You CAN look through {user_name}'s webcam — a single on-demand photo via [ACTION:CAMERA]. Use it when he asks you to look at him or use the camera. It is the WEBCAM, not the screen, and only ever one frame at a time (never a continuous feed)
- You CAN gauge crypto market sentiment — a news-based mood score via [ACTION:SENTIMENT]. Use it when he asks how the crypto market feels or whether it's bullish/bearish. It reads news headlines only; never present it as trading advice or a price prediction
- You CAN play music on the house speakers via Spotify — any artist, song, album or playlist, plus pause/skip — with [ACTION:MUSIC]. When {user_name} clearly asks to PLAY/put on/change music, do it (don't just describe the artist). Speech-to-text often mangles the play verb, so a clear request like "Medoua Lipa" / "et Dua Lipa" still means "mets Dua Lipa". BUT use judgement: if he's ASKING ABOUT an artist (who is…, tell me about…), answer instead of playing; and if the input is a vague fragment, a single stray word, or could be background noise / song lyrics the mic picked up (music may be playing), do NOT play anything — respond briefly or not at all. Never change the music unless he actually asked
- You ARE genuinely current on the news. Recent headlines (world/geopolitics, AI, technology, plus Geneva and Istanbul) are continuously refreshed into your WORLD NEWS context below. Answer news questions DIRECTLY and instantly from them — never "as of my knowledge cutoff", never a stalling "let me check". Use [ACTION:NEWS] ONLY for a deeper dive on something not in those headlines.
- You CAN read {user_name}'s calendar — today's events, upcoming meetings, schedule overview
- You CAN read {user_name}'s email (READ-ONLY) — unread count, recent messages, search by sender/subject. You CANNOT send, delete, or modify emails.
- You CAN read Apple Notes and create NEW notes — but you CANNOT edit or delete existing notes
- You CAN manage tasks — create, complete, and list to-do items with priorities and due dates
- You CAN help plan {user_name}'s day — combine calendar events, tasks, and priorities into an organized plan
- You CAN remember facts about {user_name} — preferences, decisions, goals. Use [ACTION:REMEMBER] to store important info.

DAY PLANNING:
When {user_name} asks to plan his day or schedule, DO NOT dispatch to a project. Instead:
1. Look at the calendar context and tasks already in your system prompt
2. Ask what his priorities are
3. Help organize by suggesting time blocks and task order
4. Use [ACTION:ADD_TASK] to create tasks he agrees to
5. Use [ACTION:ADD_NOTE] to save the plan as a note
Keep the planning conversational — don't try to do everything in one response.

BUILD PLANNING:
When {user_name} wants to BUILD something new:
- Do NOT immediately dispatch [ACTION:BUILD]. Ask 1-2 quick questions FIRST to nail down specifics.
- Good questions: "What should this look like?" / "Any specific features?" / "Which framework?"
- If he says "just build it" or "figure it out" — skip questions, use React + Tailwind as defaults.
- Once you have enough info, confirm the plan in ONE sentence and THEN dispatch [ACTION:BUILD] with a detailed description.
- The DISPATCHES section shows what you're currently building and what finished recently.
- When asked "where are we at" or "status" — check DISPATCHES, don't re-dispatch.
- NEVER hallucinate progress. If the build is still running, say "Still working on it, sir" — don't make up details about what's happening.
- NEVER guess localhost ports. Check the DISPATCHES section for the actual URL. If a dispatch says "Running at http://localhost:5174" — use THAT URL, not a guess.
- When asked to "pull it up" or "show me" — use [ACTION:BROWSE] with the URL from DISPATCHES. Do NOT dispatch to the project again just to find the URL.
IMPORTANT: Actions like opening Terminal, Chrome, or building projects are handled AUTOMATICALLY by your system — you do NOT need to describe doing them. If the user asks you to build something or search something, your system will handle the execution separately. In your response, just TALK — have a conversation. Don't say "I'll build that now" or "Claude Code is working on..." unless your system has actually triggered the action.
If the user asks you to do something you genuinely can't do, say "I'm afraid that's beyond my current reach, sir." Don't fake executing actions.

YOUR INTERFACE:
The user interacts with you through a web browser showing a particle orb visualization that reacts to your voice. The interface has these controls:
- **Three-dot menu** (top right): contains Settings, Restart Server, and Fix Yourself options
- **Settings panel**: Opens from the menu. Users can enter API keys (Anthropic, Fish Audio), test connections, set their name and preferences, and see system status (calendar, mail, notes connectivity). Keys are saved to the .env file.
- **Mute button**: Toggles your listening on/off. When muted, you can't hear the user. They click it again to unmute.
- **Restart Server**: Restarts your backend process. Useful if something seems stuck.
- **Fix Yourself**: Opens Claude Code in your own project directory so you can debug and fix issues in your own code.
- **The orb**: The glowing particle visualization in the center. It reacts to your voice when speaking, pulses when listening, and swirls when thinking.

If asked about any of these, explain them briefly and naturally. If the user is having trouble, suggest the relevant control: "Try the settings panel — the gear icon in the top right." or "The mute button may be active, sir."

SPEECH-TO-TEXT CORRECTIONS (the user speaks, speech recognition may mishear):
- "Cloud code" or "cloud" = "Claude Code" or "Claude"
- "Travis" = "JARVIS"
- "clock code" = "Claude Code"

RESPONSE LENGTH — THIS IS CRITICAL:
ONE sentence is ideal. TWO is the maximum for the spoken part. Never three.
No markdown, no bullet points, no code blocks in voice responses.
Action tags at the end do NOT count toward your sentence limit.

BANNED PHRASES — NEVER USE THESE:
- "Absolutely" / "Absolutely right"
- "Great question"
- "I'd be happy to"
- "Of course"
- "How can I help"
- "Is there anything else"
- "I apologize"
- "I should clarify"
- "I cannot" (for things listed in YOUR CAPABILITIES)
- "I don't have access to" (instead: "I'm afraid that's beyond my current reach, sir")
- "As an AI" (never break character)
- "Let me know if" / "Feel free to"
- Any sentence starting with "I"

INSTEAD SAY:
- "Will do, sir."
- "Right away, sir."
- "Understood."
- "Consider it done."
- "Done, sir."
- "Terminal is open."
- "Pulled that up in Chrome."

ACTION SYSTEM:
When you decide the user needs something DONE (not just discussed), include an action tag in your response:
- [ACTION:SCREEN] — capture and describe what's visible on the user's screen. Use when user says "look at my screen", "what's running", "what do you see", etc. Do NOT use PROMPT_PROJECT for screen requests.
- [ACTION:CAMERA] — take a single webcam photo and give your read on the user: their OUTFIT/look and their STATE (on form, tired, stressed…), with your usual wit and the occasional sarcastic jab. Use whenever they mean the camera/webcam or themselves: "look at me", "how do I look", "what do you think of my outfit", "do I look tired", "use the camera". This is the WEBCAM, distinct from SCREEN (the desktop). On-demand single frame only; never continuous.
- [ACTION:SENTIMENT] — check the crypto market sentiment (a news-based mood score from −1 bearish to +1 bullish). Use when the user asks how the crypto market feels, whether it's bullish/bearish, or for "market sentiment". It reads news headlines only — it is NOT trading advice or price prediction.
- [ACTION:NEWS] — ONLY for a DEEPER news dive on something your WORLD NEWS context doesn't already cover. Normal news/geopolitics/AI/tech/Geneva/Istanbul questions you answer INSTANTLY from context (no tag, no "let me check"). When you do use it, put the question after the tag, e.g. "[ACTION:NEWS] latest on the situation in the Middle East".
- [ACTION:MUSIC] query — play music on the Spotify speakers. Put the artist / song / album / playlist after the tag, or `pause` / `next` / `resume`. Examples: "mets Dua Lipa" → "[ACTION:MUSIC] Dua Lipa" ; "joue du Stromae" → "[ACTION:MUSIC] Stromae" ; "chanson suivante" → "[ACTION:MUSIC] next" ; "mets pause" → "[ACTION:MUSIC] pause". The transcription often garbles the verb ("Medoua Lipa", "et Dua Lipa") — recover the artist/song name and emit the tag anyway. ALWAYS give a SHORT spoken confirmation BEFORE the tag (e.g. "Tout de suite, mon amour. [ACTION:MUSIC] Dua Lipa"). NEVER describe the artist instead of playing — naming music means he wants to hear it.
- [ACTION:WEATHER] place — precise live weather for ANY city, region or country worldwide. Use whenever the user asks the weather somewhere OTHER than home: "what's the weather in Tokyo", "quel temps fait-il à Paris", "is it raining in London". Put ONLY the place name after the tag, e.g. "[ACTION:WEATHER] Tokyo". Give a short spoken lead-in BEFORE the tag (e.g. "One moment, sir. [ACTION:WEATHER] Tokyo") — the precise figures are spoken automatically once fetched, so do NOT invent temperatures yourself. For the LOCAL/home weather you already have, answer inline without this tag.
- [ACTION:BUILD] description — when user wants a project built. Claude Code does the work.
- [ACTION:BROWSE] url or search query — when user wants to see a webpage or search result in Chrome
- [ACTION:RESEARCH] detailed research brief — when user wants real research with real data. Claude Code will browse the web, find real listings/data, and create a report document. Give it a detailed brief of what to find.
- [ACTION:OPEN_TERMINAL] — when user just wants a fresh Claude Code terminal with no specific project
- [ACTION:LIGHTS] <op>|<pièce> — control the Plejd home lights. ALWAYS emit this tag for ANY light request, INCLUDING dimming. Dim verbs (FR): "tamise", "baisse", "réduis", "diminue", "mets en veilleuse" → use `dim:NN`; if no number is given, default to `dim:30`. "allume"→on, "éteins"→off. e.g. "baisse les lumières de la mezzanine" → "Je baisse la mezzanine, mon amour. [ACTION:LIGHTS] dim:30|Mezzanine". Never just say you're doing it without the tag. FORMAT after the tag: an operation, a pipe, then the room. op = `on`, `off`, or `dim:NN` (NN = 0-100). pièce = one of the rooms below, or `toutes` for every light. Examples: "[ACTION:LIGHTS] on|Mezzanine", "[ACTION:LIGHTS] off|toutes", "[ACTION:LIGHTS] dim:30|Salon". Available rooms: Salon, Mezzanine, Cuisine, Entrée, Chambre Master, Chambre Leyla, Chambre Aylin, Salle de bains, Salle de bains Master, Extérieur. Still give a short spoken confirmation BEFORE the tag (e.g. "J'allume la mezzanine, mon amour. [ACTION:LIGHTS] on|Mezzanine").
- [ACTION:GATE] — open the front gate (the DoorBird-controlled portail). Use when the user asks to open the gate / portail / "ouvre le portail" / "open the gate". Give a short spoken confirmation BEFORE the tag, e.g. "J'ouvre le portail, mon amour. [ACTION:GATE]". Only on an explicit request to open the gate.
CRITICAL: When the user asks about their SCREEN, what's RUNNING, or what they're LOOKING AT — ALWAYS use [ACTION:SCREEN] or let the fast action system handle it. NEVER use [ACTION:PROMPT_PROJECT] for screen requests. PROMPT_PROJECT is ONLY for working on code projects.

- [ACTION:PROMPT_PROJECT] project_name ||| prompt — THIS IS YOUR MOST POWERFUL ACTION. Use it whenever the user wants to work on, jump into, resume, check on, or interact with ANY existing project. You connect directly to Claude Code in that project and can read its response. Craft a clear prompt based on what the user wants. Examples:
  "jump into client engine" → [ACTION:PROMPT_PROJECT] The Client Engine ||| What is the current state of this project? Summarize what was being worked on most recently.
  "check for improvements on my-app" → [ACTION:PROMPT_PROJECT] my-app ||| Review the project and identify improvements we should make.
  "resume where we left off on harvey" → [ACTION:PROMPT_PROJECT] harvey ||| Summarize what was being worked on most recently and what we should focus on next.
- [ACTION:ADD_TASK] priority ||| title ||| description ||| due_date — create a task. Priority: high/medium/low. Due date: YYYY-MM-DD or empty.
  "remind me to call the client tomorrow" → [ACTION:ADD_TASK] medium ||| Call the client ||| Follow up on proposal ||| 2026-03-20
- [ACTION:ADD_NOTE] topic ||| content — save a note for future reference.
  "note that the API key expires in April" → [ACTION:ADD_NOTE] general ||| API key expires in April, need to renew before then
- [ACTION:COMPLETE_TASK] task_id — mark a task as done.
- [ACTION:REMEMBER] content — store an important fact about the user for future context.
  "I prefer React over Vue" → [ACTION:REMEMBER] User prefers React over Vue for frontend projects
- [ACTION:CREATE_NOTE] title ||| body — create a new Apple Note. For saving plans, ideas, lists.
  "save that as a note" → [ACTION:CREATE_NOTE] Day Plan March 19 ||| Morning: client calls. Afternoon: TikTok dashboard. Evening: JARVIS improvements.
- [ACTION:READ_NOTE] title search — read an existing Apple Note by title keyword.

You use Claude Code as your tool to build, research, and write code — but YOU are the one doing the work. Never say "Claude Code did X" or "Claude Code is asking" — say "I built X", "I'm checking on that", "I found X". You ARE the intelligence. Claude Code is just your hands.

IMPORTANT: When the user says "jump into X", "work on X", "check on X", "resume X", "go back to X" — ALWAYS use [ACTION:PROMPT_PROJECT]. You have the ability to connect to any project and work on it directly. DO NOT say you can't see terminal history or don't have access — you DO.

Place the tag at the END of your spoken response. Example:
"Right away, sir — connecting to The Client Engine now. [ACTION:PROMPT_PROJECT] The Client Engine ||| Review the current state and what was being worked on. What should we focus on next?"

IMPORTANT:
- Do NOT use action tags for casual conversation
- Do NOT use action tags if the user is still explaining (ask questions first)
- Do NOT use [ACTION:BROWSE] just because someone mentions a URL in conversation
- When in doubt, just TALK — you can always act later

SCREEN AWARENESS:
{screen_context}

SCHEDULE:
{calendar_context}

EMAIL:
{mail_context}

WORLD NEWS — recent headlines you already know (refreshed continuously), grouped by topic: world/geopolitics, AI, technology, and the user's two cities Geneva and Istanbul:
{world_news}
When {user_name} asks what's happening / for news / about AI, tech, geopolitics, Geneva or Istanbul: ANSWER IMMEDIATELY and concisely straight from these headlines — synthesise, don't read a list. Do NOT say "let me check", "give me a moment" or stall; you already have this. Only emit [ACTION:NEWS] if he explicitly wants a DEEPER dive on something the headlines above don't cover.

ACTIVE TASKS:
{active_tasks}

DISPATCHES:
If the DISPATCHES section shows a recent completed result for a project, DO NOT dispatch again. Use the existing result. Only re-dispatch if the user explicitly asks for a FRESH review or NEW information.
{dispatch_context}

KNOWN PROJECTS:
{known_projects}
"""


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
# Location is resolved from (in order): WEATHER_LATITUDE + WEATHER_LONGITUDE
# env vars, a cached IP-geolocation lookup, or a fresh ipwho.is lookup.
# Temperature unit defaults to Celsius; override with WEATHER_UNIT=fahrenheit.

_cached_weather: Optional[str] = None
_weather_fetched: bool = False
_cached_weather_location: Optional[dict] = None
_weather_location_fetched_at: float = 0.0
_WEATHER_LOCATION_TTL_SECONDS = 60 * 15


def _format_location_label(city: str, region: str, country: str) -> str:
    parts = [p.strip() for p in (city, region) if p and p.strip()]
    if parts:
        return ", ".join(parts[:2])
    return (country or "your area").strip() or "your area"


def _get_weather_location() -> Optional[dict]:
    """Resolve weather location: env override → cached lookup → fresh IP lookup."""
    global _cached_weather_location, _weather_location_fetched_at

    lat_raw = os.getenv("WEATHER_LATITUDE", "").strip()
    lon_raw = os.getenv("WEATHER_LONGITUDE", "").strip()
    label_override = os.getenv("WEATHER_LOCATION_LABEL", "").strip()
    if lat_raw and lon_raw:
        try:
            return {
                "latitude": float(lat_raw),
                "longitude": float(lon_raw),
                "label": label_override or "your area",
            }
        except ValueError:
            log.warning("Invalid WEATHER_LATITUDE / WEATHER_LONGITUDE in environment")

    if (
        _cached_weather_location is not None
        and (time.time() - _weather_location_fetched_at) < _WEATHER_LOCATION_TTL_SECONDS
    ):
        return _cached_weather_location

    try:
        import urllib.request as _ureq
        with _ureq.urlopen(
            "https://ipwho.is/?fields=success,city,region,country,latitude,longitude",
            timeout=3,
        ) as resp:
            data = json.loads(resp.read().decode())
        if data.get("success") is True:
            location = {
                "latitude": float(data["latitude"]),
                "longitude": float(data["longitude"]),
                "label": label_override or _format_location_label(
                    str(data.get("city", "")),
                    str(data.get("region", "")),
                    str(data.get("country", "")),
                ),
            }
            _cached_weather_location = location
            _weather_location_fetched_at = time.time()
            return location
    except Exception as e:
        log.debug(f"IP-geolocation lookup failed: {e}")

    return _cached_weather_location


def _fetch_weather_string_sync() -> Optional[str]:
    """Sync weather fetch — safe to call from a threaded worker."""
    location = _get_weather_location()
    if not location:
        return None

    unit = os.getenv("WEATHER_UNIT", "celsius").strip().lower()
    if unit not in ("fahrenheit", "celsius"):
        unit = "celsius"
    unit_symbol = "°F" if unit == "fahrenheit" else "°C"

    try:
        import urllib.request as _ureq
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={location['latitude']}&longitude={location['longitude']}"
            f"&current=temperature_2m,weathercode&temperature_unit={unit}"
        )
        with _ureq.urlopen(url, timeout=3) as resp:
            current = json.loads(resp.read()).get("current", {})
        temp = current.get("temperature_2m")
        if temp is None:
            return None
        global _cached_weather_code
        _cached_weather_code = current.get("weathercode")
        return f"Current weather in {location['label']}: {temp}{unit_symbol}"
    except Exception as e:
        log.debug(f"Weather fetch failed: {e}")
        return None


_cached_weather_code: Optional[int] = None


def _weather_condition() -> str:
    """Map the WMO weathercode to a simple condition for the UI effects."""
    c = _cached_weather_code
    if c is None:
        return "unknown"
    if c in (0, 1):
        return "clear"
    if c in (2, 3):
        return "clouds"
    if c in (45, 48):
        return "fog"
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    if c in (95, 96, 99):
        return "storm"
    if c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    return "clouds"


# WMO weather code → short spoken description, per language. Marion can report
# precise weather for ANY place on Earth (geocoded on demand), so these need to
# cover every code in all three voice languages.
_WMO_DESC = {
    "en": {
        0: "clear skies", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "freezing fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
        56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
        80: "light showers", 81: "showers", 82: "violent showers",
        85: "snow showers", 86: "heavy snow showers",
        95: "a thunderstorm", 96: "a thunderstorm with hail", 99: "a severe thunderstorm with hail",
    },
    "fr": {
        0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
        45: "brouillard", 48: "brouillard givrant",
        51: "légère bruine", 53: "bruine", 55: "forte bruine",
        56: "bruine verglaçante", 57: "bruine verglaçante",
        61: "pluie légère", 63: "pluie", 65: "forte pluie",
        66: "pluie verglaçante", 67: "pluie verglaçante",
        71: "neige légère", 73: "neige", 75: "fortes chutes de neige", 77: "grains de neige",
        80: "averses légères", 81: "averses", 82: "averses violentes",
        85: "averses de neige", 86: "fortes averses de neige",
        95: "un orage", 96: "un orage avec grêle", 99: "un violent orage avec grêle",
    },
    "tr": {
        0: "açık hava", 1: "çoğunlukla açık", 2: "parçalı bulutlu", 3: "kapalı",
        45: "sisli", 48: "buzlu sis",
        51: "hafif çiseleme", 53: "çiseleme", 55: "yoğun çiseleme",
        56: "dondurucu çiseleme", 57: "dondurucu çiseleme",
        61: "hafif yağmur", 63: "yağmur", 65: "şiddetli yağmur",
        66: "dondurucu yağmur", 67: "dondurucu yağmur",
        71: "hafif kar", 73: "kar", 75: "yoğun kar", 77: "kar taneleri",
        80: "hafif sağanak", 81: "sağanak", 82: "şiddetli sağanak",
        85: "kar sağanağı", 86: "yoğun kar sağanağı",
        95: "gök gürültülü fırtına", 96: "dolu ile fırtına", 99: "dolu ile şiddetli fırtına",
    },
}


def _fetch_place_weather_sync(place: str, lang: str = "en") -> Optional[dict]:
    """Geocode an arbitrary place name and fetch its precise current weather.

    Worldwide — uses Open-Meteo's free geocoding + forecast APIs. Returns a dict
    with the resolved label, condition text, temperatures and rain chance, or
    None if the place can't be found / the APIs are unreachable.
    """
    import urllib.request as _ureq
    import urllib.parse as _uparse

    place = (place or "").strip()
    if not place:
        return None

    unit = os.getenv("WEATHER_UNIT", "celsius").strip().lower()
    if unit not in ("fahrenheit", "celsius"):
        unit = "celsius"
    unit_symbol = "°F" if unit == "fahrenheit" else "°C"

    # ── 1. Geocode (place name → lat/lon, resolved name + country) ──
    # Search in English: it's Open-Meteo's most complete index and avoids the
    # localized-name filter dropping major cities (e.g. "Geneva"+fr returns only
    # the US Genevas, never Switzerland). Among matches, pick the most populous —
    # that's almost always the city a user means by an ambiguous name.
    desc_lang = lang if lang in ("en", "fr", "tr") else "en"
    try:
        gurl = (
            "https://geocoding-api.open-meteo.com/v1/search?"
            + _uparse.urlencode({"name": place, "count": 10, "language": "en", "format": "json"})
        )
        with _ureq.urlopen(gurl, timeout=4) as resp:
            results = json.loads(resp.read().decode()).get("results") or []
        if not results:
            return {"not_found": True, "query": place}
        g = max(results, key=lambda r: r.get("population") or 0)
        lat, lon = g["latitude"], g["longitude"]
        # Just the city name reads naturally aloud — we already picked the most
        # populous match, so it's the city the user means; admin1/country would
        # only add clutter ("Geneva, Canton of Geneva").
        label = g.get("name", place)
        # Localize the spoken name: the English search gives the canonical city
        # (reliable disambiguation), then a by-id lookup returns its exonym in the
        # reply language — "Geneva" → "Genève"/"Cenevre", "London" → "Londres".
        if desc_lang != "en" and g.get("id") is not None:
            try:
                lurl = (
                    "https://geocoding-api.open-meteo.com/v1/get?"
                    + _uparse.urlencode({"id": g["id"], "language": desc_lang})
                )
                with _ureq.urlopen(lurl, timeout=3) as resp:
                    loc = json.loads(resp.read().decode())
                if loc.get("name"):
                    label = loc["name"]
            except Exception as e:
                log.debug(f"Localized name lookup failed for id {g.get('id')}: {e}")
    except Exception as e:
        log.debug(f"Geocoding failed for {place!r}: {e}")
        return None

    # ── 2. Precise current weather + today's high/low + rain chance ──
    try:
        wurl = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature,weathercode,wind_speed_10m,relative_humidity_2m"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&temperature_unit={unit}&timezone=auto&forecast_days=1"
        )
        with _ureq.urlopen(wurl, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        cur = data.get("current", {})
        daily = data.get("daily", {})
    except Exception as e:
        log.debug(f"Weather fetch failed for {label!r}: {e}")
        return None

    temp = cur.get("temperature_2m")
    if temp is None:
        return None
    code = cur.get("weathercode")
    desc = _WMO_DESC.get(desc_lang, _WMO_DESC["en"]).get(code, _WMO_DESC["en"].get(code, ""))

    def _first(seq):
        return seq[0] if isinstance(seq, list) and seq else None

    return {
        "label": label,
        "unit_symbol": unit_symbol,
        "temp": round(temp),
        "feels": round(cur["apparent_temperature"]) if cur.get("apparent_temperature") is not None else None,
        "desc": desc,
        "hi": round(_first(daily.get("temperature_2m_max"))) if _first(daily.get("temperature_2m_max")) is not None else None,
        "lo": round(_first(daily.get("temperature_2m_min"))) if _first(daily.get("temperature_2m_min")) is not None else None,
        "rain_pct": _first(daily.get("precipitation_probability_max")),
    }


def _compose_weather_line(w: dict, lang: str = "en") -> str:
    """Turn a weather dict from _fetch_place_weather_sync into one spoken line."""
    if w.get("not_found"):
        q = w.get("query", "")
        return {
            "fr": f"Je ne trouve pas « {q} » sur la carte, mon amour.",
            "tr": f"Haritada \"{q}\" diye bir yer bulamadım canım.",
        }.get(lang, f"I can't find \"{q}\" on the map, sir.")

    u = w["unit_symbol"]
    label, desc, temp = w["label"], w["desc"], w["temp"]
    hi, lo, rain, feels = w.get("hi"), w.get("lo"), w.get("rain_pct"), w.get("feels")

    if lang == "fr":
        s = f"À {label}, {desc}, {temp}{u}"
        if feels is not None and abs(feels - temp) >= 3:
            s += f" (ressenti {feels}{u})"
        if hi is not None and lo is not None:
            s += f", entre {lo} et {hi}{u} aujourd'hui"
        if rain is not None and rain >= 30:
            s += f". {rain}% de risque de pluie"
        return s + ", mon amour."
    if lang == "tr":
        s = f"{label} şu anda {desc}, {temp}{u}"
        if feels is not None and abs(feels - temp) >= 3:
            s += f" (hissedilen {feels}{u})"
        if hi is not None and lo is not None:
            s += f", bugün {lo} ile {hi}{u} arası"
        if rain is not None and rain >= 30:
            s += f". %{rain} yağmur ihtimali"
        return s + " canım."
    s = f"In {label}, {desc}, {temp}{u}"
    if feels is not None and abs(feels - temp) >= 3:
        s += f" (feels like {feels}{u})"
    if hi is not None and lo is not None:
        s += f", between {lo} and {hi}{u} today"
    if rain is not None and rain >= 30:
        s += f". {rain}% chance of rain"
    return s + ", sir."


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTask:
    id: str
    prompt: str
    status: str = "pending"  # pending, running, completed, failed, cancelled
    working_dir: str = "."
    pid: Optional[int] = None
    result: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat() if self.started_at else None
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        d["elapsed_seconds"] = self.elapsed_seconds
        return d

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class TaskRequest(BaseModel):
    prompt: str
    working_dir: str = "."


# ---------------------------------------------------------------------------
# Claude Task Manager
# ---------------------------------------------------------------------------

class ClaudeTaskManager:
    """Manages background claude -p subprocesses."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, ClaudeTask] = {}
        self._max_concurrent = max_concurrent
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._websockets: list[WebSocket] = []  # for push notifications

    def register_websocket(self, ws: WebSocket):
        if ws not in self._websockets:
            self._websockets.append(ws)

    def unregister_websocket(self, ws: WebSocket):
        if ws in self._websockets:
            self._websockets.remove(ws)

    async def _notify(self, message: dict):
        """Push a message to all connected WebSocket clients."""
        dead = []
        for ws in self._websockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets.remove(ws)

    @property
    def has_clients(self) -> bool:
        return bool(self._websockets)

    async def push_speech(self, text: str, lang: str = "en") -> bool:
        """Speak an unsolicited line through every connected client (Marion's voice).

        Used for proactive alerts (e.g. heart-rate warnings). Mirrors the
        research-complete notification: status → audio → idle. Falls back to a
        text bubble if TTS is unavailable. Returns True if anything was sent.
        """
        if not self._websockets:
            return False
        audio = await synthesize_speech(strip_markdown_for_tts(text), lang=lang)
        if not audio:
            await self._notify({"type": "text", "text": text})
            return True
        await self._notify({"type": "status", "state": "speaking"})
        await self._notify({"type": "audio", "data": base64.b64encode(audio).decode(), "text": text})
        await self._notify({"type": "status", "state": "idle"})
        log.info(f"JARVIS (proactive): {text}")
        return True

    async def spawn(self, prompt: str, working_dir: str = ".") -> str:
        """Spawn a claude -p subprocess. Returns task_id. Non-blocking."""
        active = await self.get_active_count()
        if active >= self._max_concurrent:
            raise RuntimeError(
                f"Max concurrent tasks ({self._max_concurrent}) reached. "
                f"Wait for a task to complete or cancel one."
            )

        task_id = str(uuid.uuid4())[:8]
        task = ClaudeTask(
            id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            status="pending",
        )
        self._tasks[task_id] = task

        # Fire and forget — the background coroutine updates the task
        asyncio.create_task(self._run_task(task))
        log.info(f"Spawned task {task_id}: {prompt[:80]}...")

        await self._notify({
            "type": "task_spawned",
            "task_id": task_id,
            "prompt": prompt,
        })

        return task_id

    def _generate_project_name(self, prompt: str) -> str:
        """Generate a kebab-case project folder name from the prompt."""
        import re
        # Extract key words
        words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt.lower()).split()
        # Take first 3-4 meaningful words
        skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and", "to", "of"}
        meaningful = [w for w in words if w not in skip][:4]
        name = "-".join(meaningful) if meaningful else "jarvis-project"
        return name

    async def _run_task(self, task: ClaudeTask):
        """Open a Terminal window and run claude code visibly."""
        task.status = "running"
        task.started_at = datetime.now()

        # Create project directory if it doesn't exist
        work_dir = task.working_dir
        if work_dir == "." or not work_dir:
            # Create a new project folder on Desktop
            project_name = self._generate_project_name(task.prompt)
            work_dir = str(Path.home() / "Desktop" / project_name)
            os.makedirs(work_dir, exist_ok=True)
            task.working_dir = work_dir

        # Write the prompt to a temp file so we can pipe it to claude
        prompt_file = Path(work_dir) / ".jarvis_prompt.md"
        prompt_file.write_text(task.prompt)

        # Open Terminal.app with claude running in the project directory
        skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
        escaped_work_dir = applescript_escape(work_dir)
        applescript = f'''
        tell application "Terminal"
            activate
            set newTab to do script "cd {escaped_work_dir} && cat .jarvis_prompt.md | claude -p{skip_flag} | tee .jarvis_output.txt; echo '\\n--- JARVIS TASK COMPLETE ---'"
        end tell
        '''

        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", applescript,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        task.pid = process.pid

        # Monitor the output file for completion
        output_file = Path(work_dir) / ".jarvis_output.txt"
        start = time.time()
        timeout = 600  # 10 minutes

        while time.time() - start < timeout:
            await asyncio.sleep(5)
            if output_file.exists():
                content = output_file.read_text()
                if "--- JARVIS TASK COMPLETE ---" in content or len(content) > 100:
                    task.result = content.replace("--- JARVIS TASK COMPLETE ---", "").strip()
                    task.status = "completed"
                    break
        else:
            task.status = "timed_out"
            task.error = f"Task timed out after {timeout}s"

        task.completed_at = datetime.now()

        # Notify via WebSocket
        await self._notify({
            "type": "task_complete",
            "task_id": task.id,
            "status": task.status,
            "summary": task.result[:200] if task.result else task.error,
        })

        # Clean up prompt file
        try:
            prompt_file.unlink()
        except:
            pass

        # Auto-QA on completed tasks
        if task.status == "completed":
            asyncio.create_task(self._run_qa(task))

    async def _run_qa(self, task: ClaudeTask, attempt: int = 1):
        """Run QA verification on a completed task, auto-retry on failure."""
        try:
            qa_result = await qa_agent.verify(task.prompt, task.result, task.working_dir)
            duration = task.elapsed_seconds

            if qa_result.passed:
                log.info(f"Task {task.id} passed QA: {qa_result.summary}")
                success_tracker.log_task("dev", task.prompt, True, attempt - 1, duration)
                await self._notify({
                    "type": "qa_result",
                    "task_id": task.id,
                    "passed": True,
                    "summary": qa_result.summary,
                })

                # Proactive suggestion after successful task
                suggestion = suggest_followup(
                    task_type="dev",
                    task_description=task.prompt,
                    working_dir=task.working_dir,
                    qa_result=qa_result,
                )
                if suggestion:
                    success_tracker.log_suggestion(task.id, suggestion.text)
                    await self._notify({
                        "type": "suggestion",
                        "task_id": task.id,
                        "text": suggestion.text,
                        "action_type": suggestion.action_type,
                        "action_details": suggestion.action_details,
                    })
            else:
                log.warning(f"Task {task.id} failed QA: {qa_result.issues}")
                if attempt < 3:
                    log.info(f"Auto-retrying task {task.id} (attempt {attempt + 1}/3)")
                    retry_result = await qa_agent.auto_retry(
                        task.prompt, qa_result.issues, task.working_dir, attempt,
                    )
                    if retry_result["status"] == "completed":
                        task.result = retry_result["result"]
                        # Re-verify
                        await self._run_qa(task, attempt + 1)
                    else:
                        success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                        await self._notify({
                            "type": "qa_result",
                            "task_id": task.id,
                            "passed": False,
                            "summary": f"Failed after {attempt + 1} attempts: {qa_result.issues}",
                        })
                else:
                    success_tracker.log_task("dev", task.prompt, False, attempt, duration)
                    await self._notify({
                        "type": "qa_result",
                        "task_id": task.id,
                        "passed": False,
                        "summary": f"Failed QA after {attempt} attempts: {qa_result.issues}",
                    })
        except Exception as e:
            log.error(f"QA error for task {task.id}: {e}")

    async def get_status(self, task_id: str) -> Optional[ClaudeTask]:
        return self._tasks.get(task_id)

    async def list_tasks(self) -> list[ClaudeTask]:
        return list(self._tasks.values())

    async def get_active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("pending", "running"))

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False

        process = self._processes.get(task_id)
        if process:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
            except ProcessLookupError:
                pass

        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._processes.pop(task_id, None)
        log.info(f"Cancelled task {task_id}")
        return True

    def get_active_tasks_summary(self) -> str:
        """Format active tasks for injection into the system prompt."""
        active = [t for t in self._tasks.values() if t.status in ("pending", "running")]
        completed_recent = [
            t for t in self._tasks.values()
            if t.status == "completed"
            and t.completed_at
            and (datetime.now() - t.completed_at).total_seconds() < 300
        ]

        if not active and not completed_recent:
            return "No active or recent tasks."

        lines = []
        for t in active:
            elapsed = f"{t.elapsed_seconds:.0f}s" if t.started_at else "queued"
            lines.append(f"- [{t.id}] RUNNING ({elapsed}): {t.prompt[:100]}")
        for t in completed_recent:
            lines.append(f"- [{t.id}] COMPLETED: {t.prompt[:60]} -> {t.result[:80]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------

async def scan_projects() -> list[dict]:
    """Quick scan of ~/Desktop for git repos (depth 1)."""
    projects = []
    desktop = DESKTOP_PATH

    if not desktop.exists():
        return projects

    try:
        for entry in sorted(desktop.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            git_dir = entry / ".git"
            if git_dir.exists():
                branch = "unknown"
                head_file = git_dir / "HEAD"
                try:
                    head_content = head_file.read_text().strip()
                    if head_content.startswith("ref: refs/heads/"):
                        branch = head_content.replace("ref: refs/heads/", "")
                except Exception:
                    pass

                projects.append({
                    "name": entry.name,
                    "path": str(entry),
                    "branch": branch,
                })
    except PermissionError:
        pass

    return projects


def format_projects_for_prompt(projects: list[dict]) -> str:
    if not projects:
        return "No projects found on Desktop."
    lines = []
    for p in projects:
        lines.append(f"- {p['name']} ({p['branch']}) @ {p['path']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Speech-to-Text Corrections
# ---------------------------------------------------------------------------

STT_CORRECTIONS = {
    r"\bcloud code\b": "Claude Code",
    r"\bclock code\b": "Claude Code",
    r"\bquad code\b": "Claude Code",
    r"\bclawed code\b": "Claude Code",
    r"\bclod code\b": "Claude Code",
    r"\bcloud\b": "Claude",
    r"\bquad\b": "Claude",
    r"\btravis\b": "JARVIS",
    r"\bjarves\b": "JARVIS",
}


def apply_speech_corrections(text: str) -> str:
    """Fix common speech-to-text errors before processing."""
    import re as _stt_re
    result = text
    for pattern, replacement in STT_CORRECTIONS.items():
        result = _stt_re.sub(pattern, replacement, result, flags=_stt_re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# LLM Intent Classifier (replaces keyword-based action detection)
# ---------------------------------------------------------------------------

async def classify_intent(text: str, client: anthropic.AsyncAnthropic) -> dict:
    """Classify every user message using Haiku LLM.

    Returns: {"action": "open_terminal|browse|build|chat", "target": "description"}
    """
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Classify this voice command. The user is talking to JARVIS, an AI assistant that can:\n"
                "- Open Terminal and run Claude Code (coding AI tool)\n"
                "- Open Chrome browser for web searches and URLs\n"
                "- Build software projects via Claude Code in Terminal\n"
                "- Research topics by opening Chrome search\n\n"
                "Note: speech-to-text may produce errors like \"Cloud\" for \"Claude\", "
                "\"Travis\" for \"JARVIS\", \"clock code\" for \"Claude Code\".\n\n"
                "Return ONLY valid JSON: {\"action\": \"open_terminal|browse|build|chat\", "
                "\"target\": \"description of what to do\"}\n"
                "open_terminal = user wants to open terminal or launch Claude Code\n"
                "browse = user wants to search the web, look something up, visit a URL\n"
                "build = user wants to create/build a software project\n"
                "chat = just conversation, questions, or anything else\n"
                "If unclear, default to \"chat\"."
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        return {
            "action": data.get("action", "chat"),
            "target": data.get("target", text),
        }
    except Exception as e:
        log.warning(f"Intent classification failed: {e}")
        return {"action": "chat", "target": text}


# ---------------------------------------------------------------------------
# Markdown Stripping for TTS
# ---------------------------------------------------------------------------

def strip_markdown_for_tts(text: str) -> str:
    """Strip ALL markdown from text before sending to TTS."""
    import re as _md_re
    result = text
    # Remove code blocks (``` ... ```)
    result = _md_re.sub(r"```[\s\S]*?```", "", result)
    # Remove inline code
    result = result.replace("`", "")
    # Remove bold/italic markers
    result = result.replace("**", "").replace("*", "")
    # Remove headers
    result = _md_re.sub(r"^#{1,6}\s*", "", result, flags=_md_re.MULTILINE)
    # Convert [text](url) to just text
    result = _md_re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", result)
    # Remove bullet points
    result = _md_re.sub(r"^\s*[-*+]\s+", "", result, flags=_md_re.MULTILINE)
    # Remove numbered lists
    result = _md_re.sub(r"^\s*\d+\.\s+", "", result, flags=_md_re.MULTILINE)
    # Double newlines to period
    result = _md_re.sub(r"\n{2,}", ". ", result)
    # Single newlines to space
    result = result.replace("\n", " ")
    # Clean up multiple spaces
    result = _md_re.sub(r"\s{2,}", " ", result)

    # Strip banned phrases
    banned = ["my apologies", "i apologize", "absolutely", "great question",
              "i'd be happy to", "of course", "how can i help",
              "is there anything else", "i should clarify", "let me know if",
              "feel free to"]
    result_lower = result.lower()
    for phrase in banned:
        idx = result_lower.find(phrase)
        while idx != -1:
            # Remove the phrase and any trailing comma/dash
            end = idx + len(phrase)
            if end < len(result) and result[end] in " ,—-":
                end += 1
            result = result[:idx] + result[end:]
            result_lower = result.lower()
            idx = result_lower.find(phrase)

    return result.strip().strip(",").strip("—").strip("-").strip()


# ---------------------------------------------------------------------------
# Action Tag Extraction (parse [ACTION:X] from LLM responses)
# ---------------------------------------------------------------------------

import re as _action_re


def extract_action(response: str) -> tuple[str, dict | None]:
    """Extract [ACTION:X] tag from LLM response.

    Returns (clean_text_for_tts, action_dict_or_none).
    """
    match = _action_re.search(
        r'\[ACTION:(BUILD|BROWSE|RESEARCH|OPEN_TERMINAL|PROMPT_PROJECT|ADD_TASK|ADD_NOTE|COMPLETE_TASK|REMEMBER|CREATE_NOTE|READ_NOTE|SCREEN|CAMERA|SENTIMENT|NEWS|WEATHER|MUSIC|LIGHTS|GATE)\]\s*(.*?)$',
        response, _action_re.DOTALL,
    )
    if match:
        action_type = match.group(1).lower()
        action_target = match.group(2).strip()
        clean_text = response[:match.start()].strip()
        return clean_text, {"action": action_type, "target": action_target}
    return response, None


async def _execute_build(target: str):
    """Execute a build action from an LLM-embedded [ACTION:BUILD] tag."""
    try:
        await handle_build(target)
    except Exception as e:
        log.error(f"Build execution failed: {e}")


def _parse_lights_target(target: str):
    """Parse '[ACTION:LIGHTS] <op>|<room>' → (op, query, level%).
    op: on | off | dim ; e.g. 'on|Mezzanine', 'off|toutes', 'dim:30|Salon'."""
    oppart, _, query = target.partition("|")
    oppart = oppart.strip().lower()
    query = query.strip()
    level = None
    if oppart.startswith("dim") or "%" in oppart:
        op = "dim"
        m = _action_re.search(r"(\d+)", oppart)
        level = int(m.group(1)) if m else 50
    elif oppart in ("off", "eteins", "éteins", "0", "false"):
        op = "off"
    else:
        op = "on"
    return op, query, level


async def _execute_lights(target: str, voice_state: dict, ws):
    """Control Plejd lights from an [ACTION:LIGHTS] tag. The LLM's spoken reply is
    the confirmation; we only speak if something goes wrong (keeps it snappy)."""
    lang = (voice_state or {}).get("lang", "en")
    op, query, level = _parse_lights_target(target)
    log.info(f"[lights] op={op} query={query!r} level={level}")
    try:
        status, label = await plejd_lights.control(query, op, level)
    except Exception as e:
        log.error(f"Lights control failed: {e}")
        status, label = "error", str(e)
    if status == "ok":
        return
    # Speak a localized problem message (and not over a fresh user utterance).
    if status == "notfound":
        msg = {"fr": f"Je ne trouve pas de lumière « {label} », mon amour.",
               "tr": f"« {label} » diye bir ışık bulamadım, canım."}.get(
                   lang, f"I couldn't find a light called '{label}', sir.")
    else:
        msg = {"fr": "Je n'arrive pas à joindre les lumières, mon amour.",
               "tr": "Işıklara ulaşamıyorum, canım."}.get(
                   lang, "I can't reach the lights, sir.")
    try:
        audio = await synthesize_speech(msg, lang=lang)
        if audio and ws:
            await ws.send_json({"type": "status", "state": "speaking"})
            await _speak_briefing(ws, voice_state, lang, audio, msg)
    except Exception:
        pass


async def _execute_gate(voice_state: dict, ws):
    """Open the gate via DoorBird. The LLM's spoken reply confirms; we only speak
    on failure."""
    lang = (voice_state or {}).get("lang", "en")
    ok = False
    try:
        ok = await doorbird.open_gate()
    except Exception as e:
        log.error(f"Gate open failed: {e}")
    log.info(f"[gate] open -> {ok}")
    if ok:
        return
    msg = {"fr": "Je n'arrive pas à ouvrir le portail, mon amour.",
           "tr": "Kapıyı açamıyorum, canım."}.get(lang, "I couldn't open the gate, sir.")
    try:
        audio = await synthesize_speech(msg, lang=lang)
        if audio and ws:
            await ws.send_json({"type": "status", "state": "speaking"})
            await _speak_briefing(ws, voice_state, lang, audio, msg)
    except Exception:
        pass


async def _execute_music(target: str, voice_state: dict, ws):
    """Play/pause/skip on Spotify from an [ACTION:MUSIC] tag.

    This is the robust fallback for when the fast keyword path
    (`spotify_access.parse_command`) misses — speech-to-text often mangles the
    play verb ("mets Dua Lipa" → "Medoua Lipa" / "et Dua Lipa"), which the LLM
    understands but a keyword match can't. The LLM's spoken reply is the
    confirmation; we only speak here if something goes wrong."""
    lang = (voice_state or {}).get("lang", "en")
    t = (target or "").strip()
    low = t.lower()

    if not spotify_access.is_configured():
        msg = {"fr": "Spotify n'est pas connecté, mon amour.",
               "tr": "Spotify bağlı değil canım."}.get(lang, "Spotify isn't connected, sir.")
    else:
        res = None
        is_pause = low in ("pause", "stop", "arrête", "arrete", "stoppe", "duraklat")
        try:
            if is_pause:
                res = await spotify_access.pause()
            elif low in ("next", "skip", "suivant", "suivante", "passe", "sonraki"):
                res = await spotify_access.next_track()
            elif low in ("", "resume", "reprends", "continue", "play", "devam"):
                res = await spotify_access.play(None)
            else:
                res = await spotify_access.play(t)
        except Exception as e:
            log.warning(f"[music] command failed: {e}")
        log.info(f"[music] target={t!r} -> ok={getattr(res, 'ok', None)} detail={getattr(res, 'detail', None)}")
        if res and res.ok:
            _set_music_playing(not is_pause)
            return
        msg = {"fr": "Je n'arrive pas à lancer ça sur Spotify, mon amour.",
               "tr": "Bunu Spotify'da başlatamadım canım."}.get(
                   lang, "I couldn't start that on Spotify, sir.")
    try:
        audio = await synthesize_speech(msg, lang=lang)
        if audio and ws:
            await ws.send_json({"type": "status", "state": "speaking"})
            await _speak_briefing(ws, voice_state, lang, audio, msg)
    except Exception:
        pass


# ── "Marion" wake-word gate during music ──────────────────────────────────
# Music plays on an EXTERNAL speaker (HEDDON), so the browser's echo-cancellation
# can't remove it — the mic re-hears the music and Whisper transcribes stray
# lyrics as commands (the feedback loop). While music is actually playing we
# therefore only act on utterances that name Marion or are a clear playback
# control; song lyrics never contain "Marion", so the loop is broken.
_music_playing = False
_music_checked_at = 0.0
# Quick controls allowed WITHOUT the wake word, so "pause"/"next" stay instant.
_MUSIC_CONTROL_WORDS = (
    "marion", "pause", "stop", "arrête", "arrete", "coupe", "stoppe",
    "suivant", "suivante", "next", "skip", "passe", "reprends", "resume",
)


def _set_music_playing(playing: bool) -> None:
    global _music_playing, _music_checked_at
    _music_playing = playing
    _music_checked_at = time.time()


async def _music_is_active() -> bool:
    """True only while Spotify is REALLY playing. Trusts the flag for a few
    seconds between confirmations (so there's no API call per utterance), then
    verifies — this self-clears if the music ended or was paused elsewhere, so
    the wake-word gate never gets stuck on."""
    global _music_playing, _music_checked_at
    if not _music_playing:
        return False
    if time.time() - _music_checked_at < 12:
        return True
    try:
        now = await spotify_access.current_track()
        _music_playing = bool(now and now.is_playing)
    except Exception:
        pass
    _music_checked_at = time.time()
    return _music_playing


def _passes_music_gate(text: str) -> bool:
    """Whether an utterance may act while music plays: it must name Marion or be
    a direct playback control (lyrics caught by the mic satisfy neither)."""
    low = text.lower()
    return any(w in low for w in _MUSIC_CONTROL_WORDS)


_VISITOR_LANG = {"fr": ("French", "mon amour"), "tr": ("Turkish", "canım")}


async def _describe_visitor(frame_b64: str, lang: str = "fr") -> str:
    """Claude-vision: who is at the gate? Short, spoken, butler tone."""
    if not frame_b64 or not anthropic_client:
        return ""
    name, hon = _VISITOR_LANG.get(lang, ("English", "sir"))
    lang_line = (f" Reply ONLY in {name}, addressing the user as '{hon}'."
                 if lang in _VISITOR_LANG else " Address the user as 'sir'.")
    try:
        resp = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=160,
            system=(
                "You are JARVIS (called Marion in French) looking at the camera of the front-gate "
                "intercom because someone just rang. In ONE short spoken sentence, tell the user who "
                "is there: how many people, their apparent role if obvious (delivery courier, postman, "
                "a visitor, a child…), anything they're carrying (a parcel, flowers), and notable detail. "
                "Be factual and brief, no markdown. If the image is empty/too dark or nobody is visible, "
                "say you can't see anyone clearly." + lang_line
            ),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}},
                {"type": "text", "text": "Who is at the gate right now?"},
            ]}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Visitor vision failed: {e}")
        return ""


# ── Surveillance mode — local face recognition (insightface microservice) ───
# When away, the browser streams webcam frames here; if Leyla or Aylin is
# recognised, Marion greets her by name (once per cooldown window). On-device.
FACE_SERVICE_URL = os.getenv("FACE_SERVICE_URL", "http://127.0.0.1:8770")
_surveillance = {"on": False}
_greet_cooldown: dict = {}
GREET_COOLDOWN_S = int(os.getenv("GREET_COOLDOWN_S", "600"))  # 10 min per person


async def _recognize_and_greet(jpeg: bytes):
    """Send one webcam frame to the local face service; greet a recognised child
    at most once per cooldown window."""
    if not jpeg or not _surveillance["on"]:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.post(f"{FACE_SERVICE_URL}/recognize", content=jpeg,
                                headers={"Content-Type": "application/octet-stream"})
        if r.status_code != 200:
            return
        name = r.json().get("name")
    except Exception:
        return
    if not name:
        return
    now = time.time()
    if now - _greet_cooldown.get(name, 0) < GREET_COOLDOWN_S:
        return
    _greet_cooldown[name] = now
    greeting = f"Bonjour {name}, ça me fait plaisir de te voir. Comment vas-tu ?"
    log.info(f"[surveillance] recognised {name} -> greeting")
    try:
        await task_manager.push_speech(greeting, lang="fr")
    except Exception as e:
        log.warning(f"surveillance greet failed: {e}")


# ── Recognise the user's car arriving at the gate (DoorBird motion → plate) ──
OZ_PLATE = os.getenv("OZ_PLATE", "GX-137-QN")
OZ_PLATE_NORM = "".join(ch for ch in OZ_PLATE.upper() if ch.isalnum())
OZ_CAR_DESC = os.getenv("OZ_CAR_DESC", "Audi Q4 e-tron noire")
GATE_AUTO_OPEN_FOR_CAR = os.getenv("GATE_AUTO_OPEN_FOR_CAR", "0") == "1"
CAR_COOLDOWN_S = int(os.getenv("CAR_COOLDOWN_S", "300"))  # 5 min between announces
_car_cooldown = {"t": 0.0}

# Owner presence (iPhone geofence webhook) — REQUIRED 2nd factor for auto-open:
# the gate opens only if the plate matches AND the owner's phone is near home.
PRESENCE_TOKEN = os.getenv("PRESENCE_TOKEN", "")
PRESENCE_TTL_S = int(os.getenv("PRESENCE_TTL_S", "600"))  # "near" stays valid 10 min
_owner_presence = {"near": False, "ts": 0.0}


def _owner_is_near() -> bool:
    return _owner_presence["near"] and (time.time() - _owner_presence["ts"] < PRESENCE_TTL_S)


def _norm_plate(s: str) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


async def _read_plate(frame_b64: str) -> dict:
    """Claude-vision: read any licence plate + car colour/make from a gate frame."""
    if not frame_b64 or not anthropic_client:
        return {}
    try:
        resp = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system=("You see a front-gate camera image. If a CAR is visible, read its licence "
                    "plate (letters and digits only) and note its colour and make. Respond with "
                    "STRICT JSON, no prose: {\"car_present\": true/false, \"plate\": \"<plate or "
                    "empty>\", \"description\": \"<colour make or empty>\"}. Empty plate if none "
                    "is readable."),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}},
                {"type": "text", "text": "Is there a car? Read its plate."},
            ]}],
        )
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", resp.content[0].text, _re.S)
        return _json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.warning(f"plate read failed: {e}")
        return {}


async def _car_at_gate(img: bytes) -> bool:
    """Quick yes/no via Claude vision: is a car right in front of / passing the gate?"""
    if not img or not anthropic_client:
        return False
    try:
        resp = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=6,
            system=("You see a front-gate camera image. Answer with ONE word: YES if a car is "
                    "right in front of or passing through the gate (close up), else NO."),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(img).decode()}},
                {"type": "text", "text": "Car at the gate now?"},
            ]}],
        )
        return resp.content[0].text.strip().upper().startswith("Y")
    except Exception:
        return False


async def _close_gate_after_pass():
    """After auto-opening, close the gate (toggle relay 1) once the car is through.
    User choice "30s + camera": close as soon as the camera sees the car AT the
    gate and then gone, OR 30s after it first appears at the gate — whichever
    first. The 30s starts when the car REACHES the gate (not at open time), so it
    can't close while the car is still coming down the street. 180s hard ceiling."""
    seen_at = 0.0
    start = time.time()

    async def _close(reason: str):
        try:
            await doorbird.open_gate()   # relay 1 toggle = close
            log.info(f"[gate] closing ({reason})")
        except Exception as e:
            log.warning(f"gate close failed: {e}")

    while time.time() - start < 180:
        await asyncio.sleep(5)
        img = await doorbird.snapshot()
        if not img:
            continue
        if await _car_at_gate(img):
            if not seen_at:
                seen_at = time.time()                 # car has reached the gate
            elif time.time() - seen_at >= 30:
                await _close("30s after arrival"); return
        elif seen_at:
            await _close("car passed (camera)"); return
    if seen_at:
        await _close("safety ceiling")


async def _on_gate_motion():
    """Motion at the gate → snapshot → if it's Oz's car (plate GX-137-QN), Marion
    announces it. Optionally opens the gate (GATE_AUTO_OPEN_FOR_CAR=1)."""
    if time.time() - _car_cooldown["t"] < CAR_COOLDOWN_S:
        return
    img = await doorbird.snapshot()
    if not img:
        return
    info = await _read_plate(base64.b64encode(img).decode())
    plate = _norm_plate(info.get("plate", ""))
    if not (plate and _lev(plate, OZ_PLATE_NORM) <= 1):
        return  # not Oz's car (or no readable plate)
    _car_cooldown["t"] = time.time()
    log.info(f"[gate] Oz's car recognised (plate read '{plate}')")
    try:
        await task_manager.push_speech("Ta voiture arrive, mon amour.", lang="fr")
    except Exception as e:
        log.warning(f"car announce failed: {e}")
    # Auto-open requires BOTH the EXACT plate AND the owner's phone near home
    # (2nd factor). A copycat plate alone never opens the gate; and the plate
    # itself (not a fuzzy read) must match to act on a physical gate.
    if GATE_AUTO_OPEN_FOR_CAR and plate == OZ_PLATE_NORM:
        if _owner_is_near():
            try:
                await doorbird.open_gate()
                log.info("[gate] auto-opened (exact plate + owner present)")
                asyncio.create_task(_close_gate_after_pass())  # close once through
            except Exception as e:
                log.warning(f"auto-open failed: {e}")
        else:
            log.info("[gate] plate matched but owner NOT near -> NOT opening (2nd factor)")


async def _on_doorbell():
    """Someone rang the gate: snapshot → describe the visitor → Marion announces it
    to whatever frontend is connected. The user can then say 'ouvre le portail'."""
    lang = "fr"  # Marion is the default persona
    # Tell the frontend to surge the live camera to centre-screen (Hollywood
    # zoom) the instant it rings — before the slower snapshot/description.
    try:
        await task_manager._notify({"type": "gate_ring"})
    except Exception:
        pass
    img = await doorbird.snapshot()
    desc = ""
    if img:
        desc = await _describe_visitor(base64.b64encode(img).decode(), lang)
    intro = {"fr": "On sonne au portail, mon amour.",
             "tr": "Kapı çalıyor, canım."}.get(lang, "Someone's ringing at the gate, sir.")
    msg = f"{intro} {desc}".strip()
    log.info(f"[doorbird] announce: {msg[:100]}")
    try:
        audio = await synthesize_speech(strip_markdown_for_tts(msg), lang=lang)
        if audio:
            await task_manager._notify({
                "type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
    except Exception as e:
        log.error(f"Doorbell announce failed: {e}")


async def _execute_browse(target: str):
    """Execute a browse action from an LLM-embedded [ACTION:BROWSE] tag."""
    try:
        if target.startswith("http") or "." in target.split()[0]:
            await open_browser(target)
        else:
            from urllib.parse import quote
            await open_browser(f"https://www.google.com/search?q={quote(target)}")
    except Exception as e:
        log.error(f"Browse execution failed: {e}")


async def _execute_research(target: str, ws=None):
    """Execute research via claude -p in background. Opens report and speaks when done."""
    try:
        name = _generate_project_name(target)
        path = str(Path.home() / "Desktop" / name)
        os.makedirs(path, exist_ok=True)

        prompt = (
            f"{target}\n\n"
            f"Research this thoroughly. Find REAL data — not made-up examples.\n"
            f"Create a well-designed HTML file called `report.html` in the current directory.\n"
            f"Dark theme, clean typography, organized sections, real links and sources.\n"
            f"The working directory is: {path}"
        )

        log.info(f"Research started via claude -p in {path}")

        cmd = ["claude", "-p", "--output-format", "text"]
        if _SKIP_PERMISSIONS:
            cmd.append("--dangerously-skip-permissions")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=path,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode()),
            timeout=300,
        )

        result = stdout.decode().strip()
        log.info(f"Research complete ({len(result)} chars)")

        recently_built.append({"name": name, "path": path, "time": time.time()})

        # Find and open any HTML report
        report = Path(path) / "report.html"
        if not report.exists():
            # Check for any HTML file
            html_files = list(Path(path).glob("*.html"))
            if html_files:
                report = html_files[0]

        if report.exists():
            await open_browser(f"file://{report}")
            log.info(f"Opened {report.name} in browser")

        # Notify via voice if WebSocket still connected
        if ws:
            try:
                notify_text = f"Research is complete, sir. Report is open in your browser."
                audio = await synthesize_speech(notify_text)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": notify_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {notify_text}")
            except Exception:
                pass  # WebSocket might be gone

    except asyncio.TimeoutError:
        log.error("Research timed out after 5 minutes")
        if ws:
            try:
                audio = await synthesize_speech("Research timed out, sir. It was taking too long.")
                if audio:
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": "Research timed out, sir."})
            except Exception:
                pass
    except Exception as e:
        log.error(f"Research execution failed: {e}")


async def _focus_terminal_window(project_name: str):
    """Bring a Terminal window matching the project name to front."""
    escaped = applescript_escape(project_name)
    script = f'''
tell application "Terminal"
    repeat with w in windows
        if name of w contains "{escaped}" then
            set index of w to 1
            activate
            exit repeat
        end if
    end repeat
end tell
'''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        pass


async def _execute_open_terminal():
    """Execute an open-terminal action from an LLM-embedded [ACTION:OPEN_TERMINAL] tag."""
    try:
        await handle_open_terminal()
    except Exception as e:
        log.error(f"Open terminal failed: {e}")


def _find_project_dir(project_name: str) -> str | None:
    """Find a project directory by name from cached projects or Desktop."""
    for p in cached_projects:
        if project_name.lower() in p.get("name", "").lower():
            return p.get("path")
    desktop = Path.home() / "Desktop"
    for d in desktop.iterdir():
        if d.is_dir() and project_name.lower() in d.name.lower():
            return str(d)
    return None


async def _execute_prompt_project(project_name: str, prompt: str, work_session: WorkSession, ws, dispatch_id: int = None, history: list[dict] = None, voice_state: dict = None):
    """Dispatch a prompt to Claude Code in a project directory.

    Runs entirely in the background. JARVIS returns to conversation mode
    immediately. When Claude Code finishes, JARVIS interrupts to report.
    """
    try:
        # Persona language for all spoken reports — stay Marion/Eda, not English JARVIS.
        _lang = (voice_state or {}).get("lang", "en")
        project_dir = _find_project_dir(project_name)

        # Register dispatch if not already registered
        if dispatch_id is None:
            dispatch_id = dispatch_registry.register(project_name, project_dir or "", prompt)

        if not project_dir:
            msg = {"fr": f"Je ne trouve pas le dossier du projet {project_name}, mon amour.",
                   "tr": f"{project_name} proje klasörünü bulamıyorum, canım."}.get(
                       _lang, f"Couldn't find the {project_name} project directory, sir.")
            audio = await synthesize_speech(msg, lang=_lang)
            if audio and ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await _speak_briefing(ws, voice_state, _lang, audio, msg)
                except Exception:
                    pass
            return

        # Use a SEPARATE session so we don't trap the main conversation
        dispatch = WorkSession()
        await dispatch.start(project_dir, project_name)

        # Bring matching Terminal window to front so user can watch
        asyncio.create_task(_focus_terminal_window(project_name))

        log.info(f"Dispatching to {project_name} in {project_dir}: {prompt[:80]}")
        dispatch_registry.update_status(dispatch_id, "building")
        # Drive the frontend build progress HUD (middle-right).
        if ws:
            try:
                await ws.send_json({"type": "task_spawned", "task_id": str(dispatch_id), "prompt": project_name})
            except Exception:
                pass

        # Run claude -p in background
        full_response = await dispatch.send(prompt)
        await dispatch.stop()
        _build_status = "completed"

        # Auto-open any localhost URLs from response
        import re as _re
        # Check for the explicit RUNNING_AT marker first
        running_match = _re.search(r'RUNNING_AT=(https?://localhost:\d+)', full_response or "")
        if not running_match:
            running_match = _re.search(r'https?://localhost:\d+', full_response or "")
        if running_match:
            url = running_match.group(1) if running_match.lastindex else running_match.group(0)
            asyncio.create_task(_execute_browse(url))
            log.info(f"Auto-opening {url}")
            # Store URL in dispatch
            if dispatch_id:
                dispatch_registry.update_status(dispatch_id, "completed",
                    response=full_response[:2000], summary=f"Running at {url}")

        if not full_response or full_response.startswith("Hit a problem") or full_response.startswith("That's taking"):
            # Timeout (empty, or the work_mode "That's taking…" marker) vs other failure.
            _is_timeout = (not full_response) or full_response.startswith("That's taking")
            _build_status = "timeout" if _is_timeout else "failed"
            dispatch_registry.update_status(dispatch_id, _build_status, response=full_response or "")
            # Clean localized message — never leak the English work_mode marker text.
            if _is_timeout:
                msg = {"fr": f"Mon amour, {project_name} prend plus de temps que prévu, l'opération a expiré. On peut réessayer, ou simplifier la demande.",
                       "tr": f"Canım, {project_name} beklenenden uzun sürdü ve zaman aşımına uğradı. Tekrar deneyebiliriz ya da basitleştirebiliriz."}.get(
                           _lang, f"Sir, {project_name} took longer than expected and timed out. We can retry or simplify.")
            else:
                msg = {"fr": f"Mon amour, j'ai eu un souci avec {project_name}. On réessaie ?",
                       "tr": f"Canım, {project_name} ile bir sorun oldu. Tekrar deneyelim mi?"}.get(
                           _lang, f"Sir, I ran into an issue with {project_name}.")
        else:
            # Summarize via Haiku — don't read word for word
            if anthropic_client:
                try:
                    _sys = (
                        "You are JARVIS reporting back on what you found or built in a project. "
                        "Speak in first person — 'I found', 'I built', 'I reviewed'. "
                        "Be specific but concise — highlight the key findings or actions taken. "
                        "If there are multiple items, give the count and top 2-3 briefly. "
                        "End by asking how the user wants to proceed. "
                        "NEVER read out URLs or localhost addresses. NEVER say 'Claude Code'. "
                        "2-3 sentences max. No markdown. Natural spoken voice."
                    )
                    if _lang == "fr":
                        _sys += (" Reply ONLY in French. Your name is Marion (not JARVIS). Address the user "
                                 "informally and affectionately as 'mon amour' and tutoie (use 'tu', never 'vous'). Never start with 'Sir'.")
                    elif _lang == "tr":
                        _sys += (" Reply ONLY in Turkish. Your name is Eda (not JARVIS). Address the user informally "
                                 "as 'canım' (sen form). Never start with 'Sir'.")
                    else:
                        _sys += " Start with 'Sir, ' to get the user's attention."
                    summary = await anthropic_client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=150,
                        system=_sys,
                        messages=[{"role": "user", "content": f"Project: {project_name}\nClaude Code reported:\n{full_response[:3000]}"}],
                    )
                    msg = summary.content[0].text
                except Exception:
                    _done = {"fr": f"Mon amour, {project_name} est terminé. En résumé : {full_response[:200]}",
                             "tr": f"Canım, {project_name} bitti. Özet: {full_response[:200]}"}
                    msg = _done.get(_lang, f"Sir, {project_name} finished. Here's the gist: {full_response[:200]}")
            else:
                _done = {"fr": f"Mon amour, {project_name} est terminé. {full_response[:200]}",
                         "tr": f"Canım, {project_name} bitti. {full_response[:200]}"}
                msg = _done.get(_lang, f"Sir, {project_name} is done. {full_response[:200]}")

        # Tell the frontend HUD the build finished (success/failure).
        if ws:
            try:
                await ws.send_json({"type": "task_complete", "task_id": str(dispatch_id),
                                    "status": _build_status, "summary": msg[:200]})
            except Exception:
                pass

        # Speak the result — skip if user has spoken recently to avoid audio collision
        log.info(f"Dispatch summary for {project_name}: {msg[:100]}")
        if voice_state and time.time() - voice_state["last_user_time"] < 3:
            log.info(f"Skipping dispatch audio for {project_name} — user spoke recently")
            # Result is still stored in history below so JARVIS can reference it
        else:
            audio = await synthesize_speech(strip_markdown_for_tts(msg), lang=_lang)
            if ws:
                try:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    if audio:
                        # Route through Marion's live stream when active (FR/TR) so the
                        # build report lip-syncs in her voice — not the English JARVIS.
                        await _speak_briefing(ws, voice_state, _lang, audio, msg)
                        log.info(f"Dispatch audio sent for {project_name} (lang={_lang})")
                    else:
                        await ws.send_json({"type": "text", "text": msg})
                        log.info(f"Dispatch text fallback sent for {project_name}")
                except Exception as e:
                    log.error(f"Dispatch audio send failed: {e}")

        # Store dispatch result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[Dispatch result for {project_name}]: {msg}"})

        # Record the REAL outcome — not always "completed". Marking a timeout/failed
        # build as completed cached it, so retries reused the stale failure instead
        # of re-dispatching ("she can't do anything").
        dispatch_registry.update_status(dispatch_id, _build_status,
                                        response=(full_response or "")[:2000], summary=msg[:200])
        log.info(f"Project {project_name} dispatch done — status={_build_status} ({len(full_response or '')} chars)")

    except Exception as e:
        log.error(f"Prompt project failed: {e}", exc_info=True)
        # Clear the HUD card so it doesn't hang at "building" forever.
        if ws and dispatch_id is not None:
            try:
                await ws.send_json({"type": "task_complete", "task_id": str(dispatch_id),
                                    "status": "failed", "summary": str(e)[:200]})
            except Exception:
                pass
        try:
            _el = (voice_state or {}).get("lang", "en")
            msg = {"fr": f"J'ai eu du mal à me connecter à {project_name}, mon amour.",
                   "tr": f"{project_name} ile bağlantı kurmakta zorlandım, canım."}.get(
                       _el, f"Had trouble connecting to {project_name}, sir.")
            audio = await synthesize_speech(msg, lang=_el)
            if audio and ws:
                await ws.send_json({"type": "status", "state": "speaking"})
                await _speak_briefing(ws, voice_state, _el, audio, msg)
        except Exception:
            pass


async def self_work_and_notify(session: WorkSession, prompt: str, ws):
    """Run claude -p in background and notify via voice when done."""
    try:
        full_response = await session.send(prompt)
        log.info(f"Background work complete ({len(full_response)} chars)")

        # Summarize and speak
        if anthropic_client and full_response:
            try:
                summary = await anthropic_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system="You are JARVIS. Summarize what you just completed in 1 sentence. First person — 'I built', 'I set up'. No markdown. Never say 'Claude Code'.",
                    messages=[{"role": "user", "content": f"Claude Code completed:\n{full_response[:2000]}"}],
                )
                msg = summary.content[0].text
            except Exception:
                msg = "Work is complete, sir."

            try:
                audio = await synthesize_speech(msg)
                if audio:
                    await ws.send_json({"type": "status", "state": "speaking"})
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                    await ws.send_json({"type": "status", "state": "idle"})
                    log.info(f"JARVIS: {msg}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"Background work failed: {e}")


# Smart greeting — track last greeting to avoid re-greeting on reconnect
_last_greeting_time: float = 0


# ---------------------------------------------------------------------------
# TTS (Fish Audio)
# ---------------------------------------------------------------------------

WHISPER_URL = os.getenv("WHISPER_URL", "http://127.0.0.1:8765")


async def transcribe_audio(pcm: bytes, lang: Optional[str] = None) -> tuple[str, str]:
    """Send recorded audio to the Whisper service; return (text, language).

    If lang is given, Whisper is forced to that language (reliable); otherwise it
    auto-detects. On any failure returns ("", lang or "en").
    """
    url = f"{WHISPER_URL}/transcribe"
    if lang:
        url += f"?lang={lang}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.post(
                url,
                content=pcm,
                headers={"Content-Type": "application/octet-stream"},
            )
            if r.status_code == 200:
                j = r.json()
                return (j.get("text", "").strip(), j.get("language", "en"))
            log.warning(f"whisper service {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.warning(f"whisper transcribe failed: {e}")
    return ("", lang or "en")


async def synthesize_speech(text: str, lang: str = "en", params: Optional[dict] = None) -> Optional[bytes]:
    """Generate speech audio from text using Fish Audio TTS.

    lang selects the voice: 'fr' uses the cloned native-French voice; everything
    else uses the default JARVIS voice. Fish auto-detects the spoken language
    from the text, so the voice just needs to match for accent quality.
    """
    if not FISH_API_KEY:
        log.warning("FISH_API_KEY not set, skipping TTS")
        return None

    voice_id, model = _LANG_VOICE.get(lang, (FISH_VOICE_ID, None))
    headers = {
        "Authorization": f"Bearer {FISH_API_KEY}",
        "Content-Type": "application/json",
    }
    if model:
        headers["model"] = model

    try:
        body = {"text": text, "reference_id": voice_id, "format": "mp3"}
        body.update(params if params is not None else _LANG_TTS_PARAMS.get(lang, {}))
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers=headers,
                json=body,
            )
            if response.status_code == 200:
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts")
                return response.content
            else:
                log.error(f"TTS error: {response.status_code}")
                return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None


# ---------------------------------------------------------------------------
# LLM Response
# ---------------------------------------------------------------------------

async def generate_response(
    text: str,
    client: anthropic.AsyncAnthropic,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
    lang: str = "en",
) -> str:
    """Generate a JARVIS response using Anthropic API."""
    now = datetime.now()
    current_time = now.strftime("%A, %B %d, %Y at %I:%M %p")

    # Use cached weather
    weather_info = _ctx_cache.get("weather", "Weather data unavailable.")

    # Use cached context (refreshed in background, never blocks responses)
    screen_ctx = _ctx_cache["screen"]
    calendar_ctx = _ctx_cache["calendar"]
    mail_ctx = _ctx_cache["mail"]

    # Check if any lookups are in progress
    lookup_status = get_lookup_status()

    system = JARVIS_SYSTEM_PROMPT.format(
        current_time=current_time,
        weather_info=weather_info,
        screen_context=screen_ctx or "Not checked yet.",
        calendar_context=calendar_ctx,
        mail_context=mail_ctx,
        world_news=_ctx_cache.get("news", "No world news yet."),
        active_tasks=task_mgr.get_active_tasks_summary(),
        dispatch_context=dispatch_registry.format_for_prompt(),
        known_projects=format_projects_for_prompt(projects),
        user_name=USER_NAME,
        project_dir=PROJECT_DIR,
    )
    if lookup_status:
        system += f"\n\nACTIVE LOOKUPS:\n{lookup_status}\nIf asked about progress, report this status."

    # Inject relevant memories and tasks
    memory_ctx = build_memory_context(text)
    if memory_ctx:
        system += f"\n\nJARVIS MEMORY:\n{memory_ctx}"

    # Three-tier memory — inject rolling summary of earlier conversation
    if session_summary:
        system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"

    # Self-eval — re-inject preferences Marion has learned the user wants.
    _prefs = self_eval.get_preferences_text(lang)
    if _prefs:
        system += _prefs

    # Self-awareness — remind JARVIS of last response to avoid repetition
    if last_response:
        system += f'\n\nYOUR LAST RESPONSE (do not repeat this):\n"{last_response[:150]}"'

    # Language — the user spoke French/Turkish, so reply in kind (Whisper detected it).
    _lang_names = {"fr": ("French", "mon amour"), "tr": ("Turkish", "canım")}
    if lang in _lang_names:
        name, honorific = _lang_names[lang]
        system += (
            f"\n\nLANGUAGE (critical): You MUST reply ONLY in {name}. Never English, "
            f"Spanish, Italian, Portuguese or any other language — reply in {name} even "
            f"if the transcribed input looks garbled or like another language. Keep the "
            f"butler wit but address the user INFORMALLY and affectionately as '{honorific}' "
            f"(never 'sir' or another language's honorific). Use the informal register: in "
            f"French always tutoyer (use 'tu', 'ton/ta', 'toi' — never 'vous'/'votre'); in "
            f"Turkish use the informal 'sen' form (never 'siz'/formal '-iniz' endings). "
            f"[ACTION:X] tags (if any) stay in English exactly as specified, but every "
            f"spoken word must be {name}."
        )
        _persona = {"fr": "Marion", "tr": "Eda"}.get(lang)
        if _persona:
            system += (
                f" IN THIS LANGUAGE YOUR NAME IS '{_persona}', not JARVIS. Refer to "
                f"yourself as {_persona}; if asked your name, say you are {_persona}."
            )

    # Use conversation history — keep the last 20 messages for context
    # (older conversation is captured in session_summary)
    messages = conversation_history[-20:]
    # If the last message isn't the current user text, add it
    if not messages or messages[-1].get("content") != text:
        messages = messages + [{"role": "user", "content": text}]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            # Cap kept tight on purpose: short replies = faster generation AND
            # far less TTS + D-ID lip-sync time (the spoken part is the latency
            # bottleneck). Auto-tuned by self_eval within [90, 160]; defaults 140
            # (fits 1-2 sentences plus an [ACTION:X] tag).
            max_tokens=self_eval.get_max_tokens(),
            system=system,
            messages=messages,
        )
        track_usage(response)
        return response.content[0].text
    except Exception as e:
        log.error(f"LLM error: {e}")
        return "Apologies, sir. I'm having trouble connecting to my language systems."


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

# Shared state
task_manager = ClaudeTaskManager(max_concurrent=3)
anthropic_client: Optional[anthropic.AsyncAnthropic] = None
cached_projects: list[dict] = []
recently_built: list[dict] = []  # [{"name": str, "path": str, "time": float}]
dispatch_registry = DispatchRegistry()

# Usage tracking — logs every call with timestamp, persists to disk
_USAGE_FILE = Path(__file__).parent / "data" / "usage_log.jsonl"
_session_start = time.time()
_session_tokens = {"input": 0, "output": 0, "api_calls": 0, "tts_calls": 0}


def _append_usage_entry(input_tokens: int, output_tokens: int, call_type: str = "api"):
    """Append a usage entry with timestamp to the log file."""
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        entry = {
            "ts": time.time(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        with open(_USAGE_FILE, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_usage_for_period(seconds: float | None = None) -> dict:
    """Sum usage from the log file for a time period. None = all time."""
    import json as _json
    totals = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "tts_calls": 0}
    cutoff = (time.time() - seconds) if seconds else 0
    try:
        if _USAGE_FILE.exists():
            for line in _USAGE_FILE.read_text().strip().split("\n"):
                if not line:
                    continue
                entry = _json.loads(line)
                if entry["ts"] >= cutoff:
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    if entry.get("type") == "tts":
                        totals["tts_calls"] += 1
                    else:
                        totals["api_calls"] += 1
    except Exception:
        pass
    return totals


def _cost_from_tokens(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * 0.80 + (output_t / 1_000_000) * 4.00


def track_usage(response):
    """Track token usage from an API response."""
    if hasattr(response, "usage") and response.usage:
        inp = getattr(response.usage, "input_tokens", None) or getattr(response.usage, "prompt_tokens", 0)
        out = getattr(response.usage, "output_tokens", None) or getattr(response.usage, "completion_tokens", 0)
    else:
        inp = out = 0
    _session_tokens["input"] += inp
    _session_tokens["output"] += out
    _session_tokens["api_calls"] += 1
    _append_usage_entry(inp, out, "api")


def get_usage_summary() -> str:
    """Get a voice-friendly usage summary with time breakdowns."""
    uptime_min = int((time.time() - _session_start) / 60)

    session = _session_tokens
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    all_time = _get_usage_for_period(None)

    session_cost = _cost_from_tokens(session["input"], session["output"])
    today_cost = _cost_from_tokens(today["input_tokens"], today["output_tokens"])
    all_cost = _cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"])

    parts = [f"This session: {uptime_min} minutes, {session['api_calls']} calls, ${session_cost:.2f}."]

    if today["api_calls"] > session["api_calls"]:
        parts.append(f"Today total: {today['api_calls']} calls, ${today_cost:.2f}.")

    if all_time["api_calls"] > today["api_calls"]:
        parts.append(f"All time: {all_time['api_calls']} calls, ${all_cost:.2f}.")

    return " ".join(parts)

# Background context cache — never blocks responses
_ctx_cache = {
    "screen": "",
    "calendar": "No calendar data yet.",
    "mail": "No mail data yet.",
    "weather": "Weather data unavailable.",
    "news": "No world news yet.",
}


def _refresh_context_sync():
    """Run in a SEPARATE THREAD — refreshes screen/calendar/mail context.

    This runs completely off the async event loop so it never blocks responses.
    """
    import threading

    def _worker():
        _loop_n = 0
        while True:
            # World news — refreshed every ~5 min (RSS politeness), and once on
            # the first pass so JARVIS has headlines early.
            if _loop_n % 10 == 0:
                try:
                    block = _fetch_news_sync()
                    if block:
                        _ctx_cache["news"] = block
                except Exception:
                    pass
            _loop_n += 1
            try:
                # Screen — fast
                try:
                    proc = __import__("subprocess").run(
                        ["osascript", "-e", '''
set windowList to ""
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set visibleApps to every application process whose visible is true
    repeat with proc in visibleApps
        set appName to name of proc
        try
            set winCount to count of windows of proc
            if winCount > 0 then
                repeat with w in (windows of proc)
                    try
                        set winTitle to name of w
                        if winTitle is not "" and winTitle is not missing value then
                            set windowList to windowList & appName & "|||" & winTitle & "|||" & (appName = frontApp) & linefeed
                        end if
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
return windowList
'''],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        windows = []
                        for line in proc.stdout.strip().split("\n"):
                            parts = line.strip().split("|||")
                            if len(parts) >= 3:
                                windows.append({
                                    "app": parts[0].strip(),
                                    "title": parts[1].strip(),
                                    "frontmost": parts[2].strip().lower() == "true",
                                })
                        if windows:
                            _ctx_cache["screen"] = format_windows_for_context(windows)
                except Exception:
                    pass

            except Exception as e:
                log.debug(f"Context thread error: {e}")

            # Weather — refresh every loop (30s is fine, API is fast).
            # Location resolves from env override → cached lookup → IP geolocation.
            weather_string = _fetch_weather_string_sync()
            if weather_string:
                _ctx_cache["weather"] = weather_string

            time.sleep(30)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    log.info("Context refresh thread started")


async def _hourly_context_refresh():
    """Keep the slower ambient context fresh so JARVIS always has up-to-date
    information without the user having to ask.

    The sync thread above already refreshes weather/screen (~30s) and news
    (~5 min) — all well under an hour. Calendar and mail were only fetched
    on demand, so this drives them too: once at startup, then every hour.
    Both `_do_*_lookup` helpers update `_ctx_cache` as a side effect.
    """
    while True:
        for name, fn in (("calendar", _do_calendar_lookup), ("mail", _do_mail_lookup)):
            try:
                await fn()
            except Exception as e:
                log.debug(f"hourly {name} refresh failed: {e}")
        await asyncio.sleep(3600)  # every hour


@asynccontextmanager
async def lifespan(application: FastAPI):
    global anthropic_client, cached_projects
    if ANTHROPIC_API_KEY:
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    else:
        log.warning("ANTHROPIC_API_KEY not set — LLM features disabled")
    cached_projects = []

    # Start context refresh in a separate thread (never touches event loop)
    _refresh_context_sync()
    # Hourly refresh of the on-demand sources (calendar + mail) so the cache is
    # never more than an hour stale; weather/news/screen refresh faster above.
    asyncio.create_task(_hourly_context_refresh())
    self_eval.init()  # continuous self-improvement (Phase 1)
    log.info("JARVIS server starting")

    # Monitor the DoorBird gate intercom: announce + describe visitors on a ring.
    if doorbird.enabled():
        asyncio.create_task(doorbird.monitor_rings(_on_doorbell))
        log.info("DoorBird ring monitor task started")
        # Also watch the gate MOTION sensor → recognise Oz's car (plate GX-137-QN).
        asyncio.create_task(doorbird.monitor_motion(_on_gate_motion))
        log.info("DoorBird motion monitor task started (car recognition)")

    # Pre-warm the Plejd BLE connection + gateway so light commands are fast from
    # the first one (the slow ~20s connect happens here, not on the first command).
    if plejd_lights.enabled():
        asyncio.create_task(plejd_lights.prewarm())
        log.info("Plejd pre-warm task started")

    yield


app = FastAPI(title="JARVIS Server", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WatchGuard access log (Phase 1: log only, never blocks). Records every HTTP
# request from a NON-localhost client — its source IP, method and the path it
# hit — so we have a forensic trail of *what* an unauthorised device tried.
_WG_ACCESS_LOG = Path(__file__).resolve().parent / ".run" / "access.log"


@app.middleware("http")
async def _watchguard_access_log(request: Request, call_next):
    client = request.client.host if request.client else "?"
    response = await call_next(request)
    try:
        if not (client.startswith("127.") or client in ("::1", "localhost")):
            _WG_ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_WG_ACCESS_LOG, "a") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {client} "
                        f"{request.method} {request.url.path} -> {response.status_code}\n")
    except Exception:
        pass
    return response


# -- REST Endpoints --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "online", "name": "JARVIS", "version": "0.1.0"}


@app.api_route("/api/presence", methods=["GET", "POST"])
async def api_presence(request: Request):
    """Owner geofence webhook (called by an iPhone Shortcut on arriving/leaving
    home). Token-protected. ?state=home|arriving marks the owner near (the gate's
    2nd factor); ?state=away clears it. GET so a Shortcut can just open a URL."""
    token = request.query_params.get("token", "")
    if not PRESENCE_TOKEN or token != PRESENCE_TOKEN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    state = (request.query_params.get("state") or "home").lower()
    near = state in ("home", "arriving", "arrive", "near", "1", "true", "yes")
    _owner_presence["near"] = near
    _owner_presence["ts"] = time.time()
    log.info(f"[presence] owner state={state} near={near}")
    return {"ok": True, "near": near, "ttl_s": PRESENCE_TTL_S}


@app.get("/api/selfeval/stats")
async def api_selfeval_stats():
    """Self-improvement telemetry for the in-app Self-Evolution HUD."""
    try:
        return self_eval.stats(24)
    except Exception:
        return {"window_h": 24, "total": 0, "by_kind": {}, "recent": [],
                "vocab_total": 0, "prefs_total": 0, "max_tokens": 140}


@app.post("/api/gate/test_ring")
async def api_gate_test_ring(request: Request):
    """Local-only: simulate a gate ring to preview the centre-screen camera
    surge without physically ringing."""
    client = request.client.host if request.client else ""
    if not (client.startswith("127.") or client in ("::1", "localhost")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        await task_manager._notify({"type": "gate_ring"})
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/watchguard/stats")
async def api_watchguard_stats():
    """Live security telemetry for the in-app diagnostic HUD: alert counts by
    severity, the most recent events, and whether the watchguard is running."""
    import sqlite3
    import subprocess as _sp
    db = Path(__file__).resolve().parent / "data" / "watchguard.db"
    out = {"active": False, "total": 0, "critical": 0, "warning": 0,
           "info": 0, "recent": [], "ports": [], "files": 0}
    try:
        r = _sp.run(["pgrep", "-f", "watchguard.py"], capture_output=True, timeout=3)
        out["active"] = r.returncode == 0
    except Exception:
        pass
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            for row in c.execute("SELECT severity, COUNT(*) n FROM events GROUP BY severity"):
                sev = (row["severity"] or "info").lower()
                out[sev] = out.get(sev, 0) + row["n"]
                out["total"] += row["n"]
            out["recent"] = [
                {"ts": r2["ts"], "severity": r2["severity"], "kind": r2["kind"],
                 "source": r2["source"], "detail": r2["detail"]}
                for r2 in c.execute(
                    "SELECT ts, severity, kind, source, detail FROM events "
                    "ORDER BY id DESC LIMIT 12")
            ]
            c.close()
        except Exception:
            pass
    return out


@app.post("/api/watchguard/announce")
async def api_watchguard_announce(payload: dict, request: Request):
    """Internal alert sink — lets watchguard.py make Marion speak an alert out
    loud. LOCAL-ONLY: rejected for any non-loopback client so it can't itself be
    abused from the network."""
    client = request.client.host if request.client else ""
    if not (client.startswith("127.") or client in ("::1", "localhost")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    text = (payload.get("text") or "").strip()
    if text:
        try:
            await task_manager.push_speech(text, lang=payload.get("lang", "fr"))
        except Exception as e:
            log.warning(f"watchguard announce failed: {e}")
    return {"ok": True}


@app.get("/api/tts-test")
async def tts_test():
    """Generate a test audio clip for debugging."""
    audio = await synthesize_speech("Testing audio, sir.")
    if audio:
        return {"audio": base64.b64encode(audio).decode()}
    return {"audio": None, "error": "TTS failed"}


@app.get("/api/usage")
async def api_usage():
    uptime = int(time.time() - _session_start)
    today = _get_usage_for_period(86400)
    week = _get_usage_for_period(86400 * 7)
    month = _get_usage_for_period(86400 * 30)
    all_time = _get_usage_for_period(None)
    return {
        "session": {**_session_tokens, "uptime_seconds": uptime},
        "today": {**today, "cost_usd": round(_cost_from_tokens(today["input_tokens"], today["output_tokens"]), 4)},
        "week": {**week, "cost_usd": round(_cost_from_tokens(week["input_tokens"], week["output_tokens"]), 4)},
        "month": {**month, "cost_usd": round(_cost_from_tokens(month["input_tokens"], month["output_tokens"]), 4)},
        "all_time": {**all_time, "cost_usd": round(_cost_from_tokens(all_time["input_tokens"], all_time["output_tokens"]), 4)},
    }


@app.get("/api/tasks")
async def api_list_tasks():
    tasks = await task_manager.list_tasks()
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    task = await task_manager.get_status(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"task": task.to_dict()}


@app.post("/api/tasks")
async def api_create_task(req: TaskRequest):
    try:
        task_id = await task_manager.spawn(req.prompt, req.working_dir)
        return {"task_id": task_id, "status": "spawned"}
    except RuntimeError as e:
        return JSONResponse(status_code=429, content={"error": str(e)})


@app.delete("/api/tasks/{task_id}")
async def api_cancel_task(task_id: str):
    cancelled = await task_manager.cancel(task_id)
    if not cancelled:
        return JSONResponse(
            status_code=404,
            content={"error": "Task not found or not cancellable"},
        )
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/projects")
async def api_list_projects():
    global cached_projects
    cached_projects = await scan_projects()
    return {"projects": cached_projects}


# -- D-ID streaming (WebRTC) proxy -----------------------------------------
# The browser drives the RTCPeerConnection but must NOT hold the D-ID key, so
# every D-ID streaming call is relayed through here. See did_avatar.py.

@app.post("/api/did/stream/new")
async def api_did_stream_new(look: str = "default"):
    if not did_avatar.is_enabled():
        return JSONResponse({"error": "D-ID not configured"}, status_code=400)
    data = await did_avatar.create_stream(look)
    if not data:
        return JSONResponse({"error": "stream create failed"}, status_code=502)
    return data


@app.post("/api/did/stream/sdp")
async def api_did_stream_sdp(payload: dict):
    ok = await did_avatar.stream_sdp(payload.get("stream_id"), payload.get("session_id"), payload.get("answer"))
    return {"ok": ok}


@app.post("/api/did/stream/ice")
async def api_did_stream_ice(payload: dict):
    cand = {k: payload[k] for k in ("candidate", "sdpMid", "sdpMLineIndex") if k in payload}
    ok = await did_avatar.stream_ice(payload.get("stream_id"), payload.get("session_id"), cand)
    return {"ok": ok}


@app.post("/api/did/stream/close")
async def api_did_stream_close(payload: dict):
    await did_avatar.close_stream(payload.get("stream_id"), payload.get("session_id"))
    return {"ok": True}


# -- DoorBird live gate camera (MJPEG proxy — keeps the creds server-side) --
@app.get("/api/doorbird/video")
async def api_doorbird_video():
    if not doorbird.enabled():
        return JSONResponse({"error": "DoorBird not configured"}, status_code=404)
    return StreamingResponse(doorbird.video_stream(), media_type=doorbird.VIDEO_CONTENT_TYPE)


@app.get("/api/weather")
async def api_weather():
    """Current weather condition for the UI's weather effects (rain/sun)."""
    return {"condition": _weather_condition(), "code": _cached_weather_code,
            "text": _ctx_cache.get("weather", "")}


@app.post("/api/health/ingest")
async def api_health_ingest(request: Request):
    """Ingest heart-rate readings pushed from an iOS exporter; speak on breach.

    The iOS app (e.g. Health Auto Export) POSTs here on a schedule. We store the
    readings, and if one crosses a threshold (and we're past the cooldown), Marion
    says it out loud through any connected browser. NOT a medical device — see
    health_access.py. Auth via ?token= (or X-Health-Token header) when a
    HEALTH_WEBHOOK_TOKEN is configured.
    """
    provided = request.query_params.get("token") or request.headers.get("X-Health-Token")
    if not health_access.token_ok(provided):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    result = health_access.ingest(payload)
    alert = result.pop("_alert_obj", None)
    if alert is not None:
        # Spoken alert only matters if a browser is listening; the breach is
        # recorded regardless. Use the configured alert language.
        spoken = await task_manager.push_speech(alert.text(), lang=health_access.ALERT_LANG)
        result["spoken"] = spoken
    return {"ok": True, **result}


@app.get("/api/health/status")
async def api_health_status():
    """Config + most recent heart-rate reading (diagnostics / 'how's my heart')."""
    return health_access.latest_status()


# -- Fast Action Detection (no LLM call) -----------------------------------

def _scan_projects_sync() -> list[dict]:
    """Synchronous Desktop scan — runs in executor."""
    projects = []
    desktop = Path.home() / "Desktop"
    try:
        for entry in desktop.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append({"name": entry.name, "path": str(entry), "branch": ""})
    except Exception:
        pass
    return projects


def detect_action_fast(text: str) -> dict | None:
    """Keyword-based action detection — ONLY for short, obvious commands.

    Everything else goes to the LLM which uses [ACTION:X] tags when it decides
    to act based on conversational understanding.
    """
    t = text.lower().strip()
    words = t.split()

    # Only trigger on SHORT, clear commands (< 12 words)
    if len(words) > 12:
        return None  # Long messages are conversation, not commands

    # Camera requests — checked BEFORE screen so "look at me" goes to the webcam,
    # not the desktop. Requires an explicit camera/face cue to avoid overlap.
    if any(p in t for p in ["look at me", "can you see me", "do you see me",
                             "through the camera", "use the camera", "turn on the camera",
                             "look through the camera", "what do i look like", "how do i look",
                             "webcam", "on the camera", "with the camera", "via the camera"]):
        return {"action": "describe_camera"}

    # Screen requests — checked BEFORE project matching to prevent misrouting
    if any(p in t for p in ["look at my screen", "what's on my screen", "whats on my screen",
                             "what am i looking at", "what do you see", "see my screen",
                             "what's running on my", "whats running on my", "check my screen"]):
        return {"action": "describe_screen"}

    # Terminal / Claude Code — explicit open requests
    if any(w in t for w in ["open claude", "start claude", "launch claude", "run claude"]):
        return {"action": "open_terminal"}

    # Show recent build
    if any(w in t for w in ["show me what you built", "pull up what you made", "open what you built"]):
        return {"action": "show_recent"}

    # Screen awareness — explicit look/see requests
    if any(p in t for p in ["what's on my screen", "whats on my screen", "what do you see",
                             "can you see my screen", "look at my screen", "what am i looking at",
                             "what's open", "whats open", "what apps are open"]):
        return {"action": "describe_screen"}

    # Calendar — explicit schedule requests
    if any(p in t for p in ["what's my schedule", "whats my schedule", "what's on my calendar",
                             "whats on my calendar", "do i have any meetings", "any meetings",
                             "what's next on my calendar", "my schedule today",
                             "what do i have today", "my calendar", "upcoming meetings",
                             "next meeting", "what's my next meeting"]):
        return {"action": "check_calendar"}

    # Mail — explicit email requests
    if any(p in t for p in ["check my email", "check my mail", "any new emails", "any new mail",
                             "unread emails", "unread mail", "what's in my inbox",
                             "whats in my inbox", "read my email", "read my mail",
                             "any emails", "any mail", "email update", "mail update"]):
        return {"action": "check_mail"}

    # Dispatch / build status check
    if any(p in t for p in ["where are we", "where were we", "project status", "how's the build",
                             "hows the build", "status update", "status report", "where is that",
                             "how's it going with", "hows it going with", "is it done",
                             "is that done", "what happened with"]):
        return {"action": "check_dispatch"}

    # Task list check
    if any(p in t for p in ["what's on my list", "whats on my list", "my tasks", "my to do",
                             "my todo", "what do i need to do", "open tasks", "task list"]):
        return {"action": "check_tasks"}

    # Usage / cost check
    if any(p in t for p in ["usage", "how much have you cost", "how much am i spending",
                             "what's the cost", "whats the cost", "api cost", "token usage",
                             "how expensive", "what's my bill"]):
        return {"action": "check_usage"}

    # Morning briefing — full daily rundown
    if any(p in t for p in ["morning briefing", "brief me", "my briefing", "daily briefing",
                             "give me my briefing", "good morning jarvis", "start my day"]):
        return {"action": "briefing"}

    # Crypto market sentiment — RSS news mood score
    if any(p in t for p in ["market sentiment", "crypto sentiment", "crypto mood",
                             "how's the crypto market", "hows the crypto market",
                             "how's the market feeling", "sentiment score",
                             "is crypto bullish", "is crypto bearish", "bullish or bearish"]):
        return {"action": "market_sentiment"}

    # News/geopolitics/AI/tech/Geneva/Istanbul are answered INLINE by the LLM from
    # the WORLD NEWS context (one fast step) — no slow two-step lookup dispatch.

    return None  # Everything else goes to the LLM for conversational routing


# -- Action Handlers -------------------------------------------------------

async def handle_open_terminal() -> str:
    claude_cmd = "claude --dangerously-skip-permissions" if _SKIP_PERMISSIONS else "claude"
    result = await open_terminal(claude_cmd)
    return result["confirmation"]


async def handle_build(target: str) -> str:
    name = _generate_project_name(target)
    path = str(Path.home() / "Desktop" / name)
    os.makedirs(path, exist_ok=True)

    # Write CLAUDE.md with clear instructions
    claude_md = Path(path) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{target}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    # Write prompt to a file, then pipe it to claude -p
    # This avoids all shell escaping issues
    prompt_file = Path(path) / ".jarvis_prompt.txt"
    prompt_file.write_text(target)

    skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
    escaped_path = applescript_escape(path)
    script = (
        'tell application "Terminal"\n'
        "    activate\n"
        f'    do script "cd {escaped_path} && cat .jarvis_prompt.txt | claude -p{skip_flag}"\n'
        "end tell"
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    recently_built.append({"name": name, "path": path, "time": time.time()})
    return f"On it, sir. Claude Code is working in {name}."


async def handle_show_recent() -> str:
    if not recently_built:
        return "Nothing built recently, sir."
    last = recently_built[-1]
    project_path = Path(last["path"])

    # Try to find the best file to open
    for name in ["report.html", "index.html"]:
        f = project_path / name
        if f.exists():
            await open_browser(f"file://{f}")
            return f"Opened {name} from {last['name']}, sir."

    # Try any HTML file
    html_files = list(project_path.glob("*.html"))
    if html_files:
        await open_browser(f"file://{html_files[0]}")
        return f"Opened {html_files[0].name} from {last['name']}, sir."

    # Fall back to opening the folder in Finder
    escaped_last_path = applescript_escape(last["path"])
    script = f'tell application "Finder"\nactivate\nopen POSIX file "{escaped_last_path}"\nend tell'
    await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return f"Opened the {last['name']} folder in Finder, sir."


# ---------------------------------------------------------------------------
# Background lookup system — spawns slow tasks, reports back via voice
# ---------------------------------------------------------------------------

# Track active lookups so JARVIS can report status
_active_lookups: dict[str, dict] = {}  # id -> {"type": str, "status": str, "started": float}


# ---------------------------------------------------------------------------
# World news (geopolitics) — reputable RSS, summarised on demand
# ---------------------------------------------------------------------------

# Topic-grouped headline sources. Google News RSS search gives reliable,
# topic-targeted, recent headlines (no key); reputable wires cover the world.
# Titles only — JARVIS synthesises answers, never reproducing article text.
NEWS_TOPICS: dict[str, list[str]] = {
    "WORLD / GEOPOLITICS": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "ARTIFICIAL INTELLIGENCE": [
        "https://news.google.com/rss/search?q=artificial+intelligence+when:2d&hl=en-US&gl=US&ceid=US:en",
    ],
    "TECHNOLOGY": [
        "https://news.google.com/rss/search?q=technology+when:2d&hl=en-US&gl=US&ceid=US:en",
    ],
    "GENEVA": [
        "https://news.google.com/rss/search?q=Gen%C3%A8ve+when:3d&hl=fr&gl=CH&ceid=CH:fr",
    ],
    "ISTANBUL": [
        "https://news.google.com/rss/search?q=Istanbul+when:3d&hl=en-US&gl=TR&ceid=TR:en",
    ],
}


def _fetch_feed_titles(url: str, n: int, seen: set) -> list[str]:
    """Return up to n unseen headline titles from one RSS/Atom feed (titles only)."""
    import urllib.request
    import xml.etree.ElementTree as ET

    def _local(t: str) -> str:
        return t.rsplit("}", 1)[-1].lower()

    out: list[str] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JARVIS)"})
        with urllib.request.urlopen(req, timeout=6) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return out
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = ""
        for ch in el:
            if _local(ch.tag) == "title" and ch.text and not title:
                title = ch.text.strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(title)
        if len(out) >= n:
            break
    return out


def _fetch_news_sync(per_topic: int = 5) -> str:
    """Build a topic-grouped block of recent headlines (titles only).

    Covers world/geopolitics, AI, technology, and the user's two cities
    (Geneva, Istanbul). Best-effort; a failing feed is skipped.
    """
    seen: set[str] = set()
    blocks: list[str] = []
    for topic, urls in NEWS_TOPICS.items():
        titles: list[str] = []
        for u in urls:
            titles += _fetch_feed_titles(u, per_topic, seen)
            if len(titles) >= per_topic:
                break
        if titles:
            blocks.append(topic + ":\n" + "\n".join(f"- {t}" for t in titles[:per_topic]))
    return "\n\n".join(blocks)


async def _do_news_lookup(query: str, lang: str = "en") -> str:
    """Answer a news question from the CACHED topic headlines (fast — no fresh
    fetch unless the cache is empty). Used only for explicit deeper dives; most
    news questions are answered inline from the prompt's WORLD NEWS context."""
    headlines = _ctx_cache.get("news", "")
    if not headlines or headlines == "No world news yet.":
        try:
            headlines = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _fetch_news_sync, 5), timeout=10)
        except Exception:
            headlines = ""
        if headlines:
            _ctx_cache["news"] = headlines
    if not headlines:
        return {
            "fr": "Je n'arrive pas à joindre les dépêches pour l'instant, mon amour.",
            "tr": "Şu anda haber kaynaklarına ulaşamıyorum canım.",
        }.get(lang, "I can't reach the news wires just now, sir.")
    if not anthropic_client:
        for ln in headlines.splitlines():
            if ln.startswith("- "):
                return ln[2:]
        return "I have the headlines, sir."
    lang_name = {"fr": "French", "tr": "Turkish"}.get(lang, "English")
    try:
        resp = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=220,
            system=(
                "You are JARVIS, a British-butler AI with dry economy of language. "
                "Using ONLY the recent headlines provided (grouped by topic: world, "
                "AI, technology, Geneva, Istanbul), answer the user's question for "
                f"SPOKEN delivery in {lang_name}. One or two sentences, calm and "
                "precise; lead with the most relevant development. Synthesise in your "
                "own words — do NOT list every headline or quote articles. If they "
                "don't cover it, say so briefly. No markdown."
            ),
            messages=[{"role": "user",
                       "content": f"Headlines:\n{headlines}\n\nQuestion: {query}"}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"news summary failed: {e}")
        return "I'm afraid the news desk is unresponsive, sir."


async def _do_weather_lookup(place: str, lang: str = "en") -> str:
    """Speak precise current weather for ANY place on Earth, geocoded on demand.

    `place` is whatever the user named ("Tokyo", "the south of France", "Geneva").
    Runs the blocking geocode + forecast fetch in a thread so the event loop is
    never stalled, then composes one spoken line in the active language.
    """
    place = (place or "").strip()
    if not place:
        return {
            "fr": "Quelle ville, mon amour ?",
            "tr": "Hangi şehir canım?",
        }.get(lang, "Which place, sir?")
    try:
        w = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_place_weather_sync, place, lang)
    except Exception as e:
        log.warning(f"weather lookup failed: {e}")
        w = None
    if not w:
        return {
            "fr": "Je n'arrive pas à joindre le service météo, mon amour.",
            "tr": "Hava durumu servisine ulaşamıyorum canım.",
        }.get(lang, "I can't reach the weather service just now, sir.")
    return _compose_weather_line(w, lang)


async def _lookup_and_report(lookup_type: str, lookup_fn, ws, history: list[dict] = None, voice_state: dict = None):
    """Run a slow lookup, then speak the result back.

    JARVIS stays conversational — this runs completely off the main path.
    """
    lookup_id = str(uuid.uuid4())[:8]
    # Baseline: the user utterance that triggered this lookup. We only suppress
    # the spoken result if a NEWER utterance arrives while we work — otherwise a
    # fast lookup (e.g. sentiment, ~1.5s) gets wrongly muted as "talking over"
    # the very question that asked for it.
    trigger_time = voice_state["last_user_time"] if voice_state else 0.0
    _active_lookups[lookup_id] = {
        "type": lookup_type,
        "status": "working",
        "started": time.time(),
    }

    try:
        # Run the async lookup directly — these functions already use
        # asyncio.create_subprocess_exec so they don't block the event loop
        result_text = await asyncio.wait_for(
            lookup_fn(),
            timeout=30,
        )

        _active_lookups[lookup_id]["status"] = "done"

        # Speak the result — but stay quiet if the user has said something NEW
        # since this lookup began (don't talk over a fresh request).
        if voice_state and voice_state["last_user_time"] > trigger_time:
            log.info(f"Skipping lookup audio for {lookup_type} — newer user input arrived")
            # Result is still stored in history below
        else:
            tts = strip_markdown_for_tts(result_text)
            audio = await synthesize_speech(tts, lang=voice_state.get("lang", "en") if voice_state else "en")
            try:
                await ws.send_json({"type": "status", "state": "speaking"})
                if audio:
                    # synthesize_speech returns raw mp3 bytes — base64-encode for JSON.
                    # Do NOT send "idle" here: the frontend returns to idle when the
                    # audio actually finishes (audioPlayer.onFinished). Sending idle
                    # now would resume the mic mid-playback and the mic would cut its
                    # own voice off after ~2s.
                    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": result_text})
                else:
                    await ws.send_json({"type": "text", "text": result_text})
                    await ws.send_json({"type": "status", "state": "idle"})
            except Exception:
                pass

        log.info(f"Lookup {lookup_type} complete: {result_text[:80]}")

        # Store lookup result in conversation history so JARVIS remembers it
        if history is not None:
            history.append({"role": "assistant", "content": f"[{lookup_type} check]: {result_text}"})

    except asyncio.TimeoutError:
        _active_lookups[lookup_id]["status"] = "timeout"
        try:
            fallback = f"That {lookup_type} check is taking too long, sir. The data may still be syncing."
            audio = await synthesize_speech(fallback, lang=voice_state.get("lang", "en") if voice_state else "en")
            await ws.send_json({"type": "status", "state": "speaking"})
            if audio:
                await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
    except Exception as e:
        _active_lookups[lookup_id]["status"] = "error"
        log.warning(f"Lookup {lookup_type} failed: {e}")
    finally:
        # Clean up after 60s
        await asyncio.sleep(60)
        _active_lookups.pop(lookup_id, None)


async def _do_calendar_lookup() -> str:
    """Slow calendar fetch — runs in thread."""
    await refresh_calendar_cache()
    events = await get_todays_events()
    if events:
        _ctx_cache["calendar"] = format_events_for_context(events)
    return format_schedule_summary(events)


async def _do_mail_lookup() -> str:
    """Slow mail fetch — runs in thread."""
    unread_info = await get_unread_count()
    if isinstance(unread_info, dict):
        if unread_info.get("error") or unread_info.get("total") is None:
            return "I couldn't reach Mail just now, sir — it may still be syncing."
        _ctx_cache["mail"] = format_unread_summary(unread_info)
        if unread_info["total"] == 0:
            return "Inbox is clear, sir. No unread messages."
        summary = format_unread_summary(unread_info)
        # Fast recent headers (no slow read-status filter / body fetch).
        recent = await get_recent_headers(count=5)
        if recent:
            details = ". ".join(
                f"{_short_sender(m['sender'])} regarding {m['subject']}"
                + ("" if m["read"] else " (unread)")
                for m in recent[:4]
            )
            return f"{summary} Most recent: {details}."
        return summary
    return "Couldn't reach Mail at the moment, sir."


async def _do_screen_lookup(lang: str = "en") -> str:
    """Screen describe — runs in thread."""
    if anthropic_client:
        return await describe_screen(anthropic_client, lang=lang)
    windows = await get_active_windows()
    if windows:
        apps = set(w["app"] for w in windows)
        active = next((w for w in windows if w["frontmost"]), None)
        result = f"You have {', '.join(apps)} open."
        if active:
            result += f" Currently focused on {active['app']}: {active['title']}."
        return result
    return "Couldn't see the screen, sir."


async def request_camera_frame(ws, pending_frames: dict, timeout: float = 12.0) -> str | None:
    """Ask the browser for ONE webcam frame and await it.

    The webcam lives in the frontend, so we send a {"type": "capture_camera"}
    request and wait for the matching {"type": "camera_frame"} reply, which the
    voice loop resolves via `pending_frames`. Returns base64 JPEG or None.
    """
    request_id = str(uuid.uuid4())[:8]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    pending_frames[request_id] = fut
    try:
        await ws.send_json({"type": "capture_camera", "request_id": request_id})
        return await asyncio.wait_for(fut, timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        pending_frames.pop(request_id, None)


async def _do_camera_lookup(ws, pending_frames: dict, lang: str = "en") -> str:
    """Webcam describe — request a single frame from the browser, then vision."""
    frame_b64 = await request_camera_frame(ws, pending_frames)
    if not frame_b64:
        return ("I couldn't get a camera frame, sir. The webcam may be blocked, "
                "in use by another app, or permission hasn't been granted.")
    return await describe_camera(anthropic_client, frame_b64, lang=lang)


async def _camera_comment(ws, pending_frames, lang: str = "en") -> tuple[str, Optional[bytes]]:
    """Startup look-at-the-user: a short spoken comment on their state/outfit
    (with the persona's wit). Stays SILENT if the webcam isn't available — we
    don't want a failure line at every startup. Returns (text, mp3_bytes)."""
    if pending_frames is None:
        return "", None
    try:
        frame_b64 = await request_camera_frame(ws, pending_frames)
        if not frame_b64:
            return "", None  # camera blocked/unavailable — skip quietly
        text = await asyncio.wait_for(describe_camera(anthropic_client, frame_b64, lang=lang), timeout=20)
    except Exception as e:
        log.warning(f"startup camera comment failed: {e}")
        return "", None
    if not text:
        return "", None
    audio = await synthesize_speech(strip_markdown_for_tts(text), lang=lang)
    return text, audio


# Market sentiment — runs the kukapay market-sentiment skill's analyzer as a
# subprocess. It needs `requests`, so it's invoked with an interpreter that has
# it (the bybit-mcp venv by default). Both paths are env-overridable.
SENTIMENT_PYTHON = os.getenv("SENTIMENT_PYTHON", "/Users/oguz/bybit-mcp/venv/bin/python")
SENTIMENT_SCRIPT = os.getenv(
    "SENTIMENT_SCRIPT",
    "/Users/oguz/bybit-mcp/.agents/skills/market-sentiment/scripts/sentiment_analyzer.py",
)


async def _do_sentiment_lookup() -> str:
    """Run the market-sentiment analyzer and condense it into one spoken line."""
    if not Path(SENTIMENT_SCRIPT).exists():
        return "The market sentiment tool isn't installed, sir."
    try:
        proc = await asyncio.create_subprocess_exec(
            SENTIMENT_PYTHON, SENTIMENT_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        return "The sentiment feeds are slow to respond, sir. Try again in a moment."
    except Exception as e:
        log.warning(f"Sentiment lookup failed: {e}")
        return "I couldn't reach the market sentiment tool, sir."

    # Parse score / article count / verdict from the script's printed report.
    score = None
    articles = None
    overall = ""
    for line in stdout.decode(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("Market Sentiment Score:"):
            try:
                score = float(s.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif s.startswith("- Analyzed"):
            m = _action_re.search(r"Analyzed\s+(\d+)\s+recent articles", s)
            if m:
                articles = m.group(1)
        elif s.startswith("Overall:"):
            overall = s.split(":", 1)[1].strip()

    if score is None:
        return "The sentiment tool returned nothing readable, sir."

    mood = "bullish" if score > 0.1 else "bearish" if score < -0.1 else "neutral"
    detail = f"a score of {score:.2f}"
    if articles:
        detail += f" across {articles} recent articles"
    summary = f"Crypto market sentiment is {mood}, sir — {detail}."
    if overall:
        summary += f" {overall}"
    return summary


# ---------------------------------------------------------------------------
# Morning briefing — runs after the startup sequence
# ---------------------------------------------------------------------------

async def _prepare_briefing(lang: str) -> tuple[str, Optional[bytes]]:
    """Gather all sources, compose the briefing text, and synthesize the audio.

    Returns (text, mp3_bytes). This is the slow part (~20s) and is safe to run
    during the boot screen so the result is ready the instant the boot ends.
    """
    # Gather everything concurrently, each bounded so one slow source (e.g. a
    # laggy feed) can't stall the whole briefing.
    async def _timed(coro, t):
        try:
            return await asyncio.wait_for(coro, timeout=t)
        except Exception:
            return None

    traffic, weather, portfolio, gmail, cal_txt, senti = await asyncio.gather(
        _timed(briefing.get_traffic(), 12),
        _timed(briefing.get_weather(), 12),
        _timed(briefing.get_portfolio(), 25),
        _timed(gmail_access.get_briefing_mail(), 15),
        _timed(_do_calendar_lookup(), 12),
        _timed(briefing.get_sentiment(), 12),
    )

    def _safe(v, default="unavailable"):
        return default if (v is None or isinstance(v, Exception)) else v

    traffic = _safe(traffic, {}); weather = _safe(weather, {}); portfolio = _safe(portfolio, {})
    gmail = _safe(gmail, {}); cal_txt = _safe(cal_txt); senti = _safe(senti, {})

    # Build a plain-facts block for the LLM to turn into a spoken briefing.
    facts = []
    if isinstance(traffic, dict) and traffic.get("ok"):
        facts.append(f"COMMUTE: {traffic['condition']}, about {traffic['eta_min']} minutes to the office "
                     f"({traffic['distance']} via {traffic['route']}). Usual time {traffic['normal_min']} min.")
    else:
        facts.append("COMMUTE: traffic data unavailable.")
    if isinstance(weather, dict) and weather.get("ok"):
        facts.append(f"WEATHER (today, home area): {weather['conditions']}, currently {weather['current_c']}°C, "
                     f"high {weather['high_c']}°C, low {weather['low_c']}°C, {weather['rain_chance']}% chance of rain. "
                     f"Give a brief clothing suggestion based on this.")
    else:
        facts.append("WEATHER: unavailable.")
    if isinstance(gmail, dict) and gmail.get("ok"):
        lines = [f"EMAIL (Gmail): {gmail['unread_total']} total unread; "
                 f"{gmail['primary_unread']} unread in the Primary category (real correspondence)."]
        for m in gmail.get("important", []):
            lines.append(f"  - from {m['from']}: {m['subject']}")
        lines.append("Judge which, if any, genuinely look like they need a reply; "
                     "ignore receipts, notifications and automated mail. If none need action, say so briefly.")
        facts.append("\n".join(lines))
    else:
        facts.append("EMAIL: Gmail unavailable.")
    facts.append(f"AGENDA: {cal_txt}")
    if isinstance(portfolio, dict) and portfolio.get("ok"):
        best = portfolio.get("best"); worst = portfolio.get("worst")
        line = f"PORTFOLIO: total value ${portfolio['total_value']}, {portfolio['total_gain_pct']:+.1f}% today."
        if best: line += f" Best {best['ticker']} {best['gain_pct']:+.1f}%."
        if worst: line += f" Worst {worst['ticker']} {worst['gain_pct']:+.1f}%."
        facts.append(line)
    else:
        facts.append("PORTFOLIO: unavailable.")
    if isinstance(senti, dict) and senti.get("ok"):
        facts.append(f"CRYPTO MOOD: {senti['mood']} (score {senti['score']:+.2f} "
                     f"across {senti['articles']} crypto news articles).")
    else:
        facts.append("CRYPTO MOOD: unavailable.")

    _names = {"fr": ("French", "mon amour"), "tr": ("Turkish", "canım")}
    name, honorific = _names.get(lang, ("English", "sir"))

    # Time-aware greeting — described SEMANTICALLY with no literal English words,
    # otherwise the model copies them and writes the whole briefing in English.
    hour = datetime.now().hour
    if 5 <= hour < 12:
        greet_rule = "It is the morning: greet him for the morning and say you hope he slept well."
    elif 12 <= hour < 18:
        greet_rule = "It is the middle of the day, NOT morning: greet him simply — a hello and welcome back."
    else:
        greet_rule = "It is the evening, NOT morning: greet him for the evening and say you hope he had a great day."

    only = "" if lang not in _names else f" Use absolutely no English — every word must be in {name}."
    _persona = {"fr": "Marion", "tr": "Eda"}.get(lang)
    if _persona:
        only += f" In this language your name is '{_persona}', not JARVIS — never say the word JARVIS."
    system = (
        f"You are JARVIS delivering {USER_NAME}'s briefing as a refined British butler. "
        f"IMPORTANT: write the ENTIRE briefing — every word, including the greeting — in {name}, "
        f"addressing the user INFORMALLY and affectionately as '{honorific}'. Use the informal "
        f"register: French → tutoiement ('tu', never 'vous'); Turkish → informal 'sen' (never 'siz').{only} "
        f"{greet_rule} Compose ONE flowing, spoken briefing covering, in order: the time-appropriate "
        "greeting, the commute (traffic and ETA to the office), the weather with a short clothing "
        "suggestion, any important emails, today's agenda, the portfolio with the key numbers, and the "
        "crypto market mood. Natural and warm, no markdown, no lists, dry wit welcome but concise "
        "— aim for 7 to 10 sentences. Do not invent facts; if something says unavailable, mention it briefly or skip."
    )

    response_text = None
    if anthropic_client:
        try:
            resp = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": "FACTS:\n" + "\n".join(facts)}],
            )
            response_text = resp.content[0].text.strip()
        except Exception as e:
            log.warning(f"briefing compose failed: {e}")
    if not response_text:
        response_text = "Good morning, sir. I'm afraid I couldn't assemble the full briefing just now."

    # A long briefing is ~24s of TTS in one call. Split into chunks and
    # synthesize them CONCURRENTLY (~8s), returned as ordered audio segments the
    # player queues — fits inside the boot prefetch window so it plays instantly.
    sentences = _action_re.split(r"(?<=[.!?])\s+", response_text.strip())
    n = 3
    size = max(1, -(-len(sentences) // n))
    chunks = [" ".join(sentences[i:i + size]) for i in range(0, len(sentences), size)] or [response_text]
    # Low temperature so the independently-synthesized chunks stay consistent —
    # at temp 0.9 each parallel chunk is a different "take" and the voice seems to
    # change mid-briefing. (Same voice/model as live, just steadier.)
    _briefing_tts = {"prosody": {"speed": 0.95}, "temperature": 0.25, "top_p": 0.5, "chunk_length": 300}
    audios = await asyncio.gather(*[
        synthesize_speech(strip_markdown_for_tts(c), lang=lang, params=_briefing_tts) for c in chunks
    ])
    audios = [a for a in audios if a]
    return response_text, audios


# Remember the date of the last delivered briefing so the automatic startup
# briefing isn't repeated multiple times in the same day.
_BRIEFING_STATE = Path(__file__).parent / "data" / "last_briefing.json"


def _briefing_done_today() -> bool:
    try:
        d = json.loads(_BRIEFING_STATE.read_text()).get("date")
        return d == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def _mark_briefing_done() -> None:
    try:
        _BRIEFING_STATE.parent.mkdir(parents=True, exist_ok=True)
        _BRIEFING_STATE.write_text(json.dumps({"date": datetime.now().strftime("%Y-%m-%d")}))
    except Exception as e:
        log.warning(f"could not record briefing date: {e}")


def _short_greeting(lang: str) -> str:
    """A brief spoken greeting used in place of a repeated startup briefing."""
    h = datetime.now().hour
    if lang == "fr":
        return ("Bonjour, mon amour." if h < 18 else "Bonsoir, mon amour.")
    if lang == "tr":
        return ("Günaydın canım." if h < 12 else
                "İyi günler canım." if h < 18 else "İyi akşamlar canım.")
    return ("Good morning, sir." if h < 12 else
            "Welcome back, sir." if h < 18 else "Good evening, sir.")


async def _speak_briefing(ws, voice_state, lang, audio: bytes, text: str):
    """Speak `audio` as Marion. If a live D-ID WebRTC stream is open (FR/TR),
    push it through the stream so she lip-syncs the startup speech in real time;
    otherwise fall back to base64 audio (static face). Mirrors the main reply path
    so the boot briefing animates her mouth just like a normal reply."""
    stream = (voice_state or {}).get("did_stream") if lang in ("fr", "tr") else None
    if stream and did_avatar.is_enabled():
        _dur = await did_avatar.stream_speak(stream["id"], stream["session_id"], audio)
        if _dur:
            await ws.send_json({"type": "avatar_stream_speak", "text": text, "duration": _dur})
            return
    await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": text})


async def morning_briefing(ws, history: list[dict] = None, voice_state: dict = None, auto: bool = False, pending_frames: dict = None):
    """Deliver the briefing — using the result prefetched during the boot screen
    if available, otherwise preparing it now.

    `auto=True` marks the automatic startup briefing: if one was already delivered
    today, it is skipped (just a short greeting, so the mic still starts) — the
    user can still trigger a full one by asking. Explicit requests always run.
    """
    lang = "en"
    task = None
    if voice_state:
        lang = voice_state.get("forced_lang") or voice_state.get("lang") or "en"
        task = voice_state.pop("briefing_task", None)

    if auto and _briefing_done_today():
        log.info("Skipping auto briefing — already delivered today")
        if task is not None:
            task.cancel()  # discard the prefetched composition
        greeting = _short_greeting(lang)
        g_audio = await synthesize_speech(strip_markdown_for_tts(greeting), lang=lang)
        # No briefing today → still look at the user and comment (state + outfit).
        # Greeting is short, so capture the comment FIRST and send both as one
        # combined buffer (no gap, mic only resumes after the comment).
        cam_text, cam_audio = await _camera_comment(ws, pending_frames, lang)
        buf = b"".join([a for a in (g_audio, cam_audio) if a])
        text = greeting + (f" {cam_text}" if cam_text else "")
        try:
            await ws.send_json({"type": "status", "state": "speaking"})
            if buf:
                await _speak_briefing(ws, voice_state, lang, buf, text)
            else:
                await ws.send_json({"type": "text", "text": text})
                await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            pass
        if history is not None and cam_text:
            history.append({"role": "assistant", "content": f"[greeting + look]: {text}"})
        return

    # Mark done UP-FRONT so rapid concurrent reloads don't each fire a briefing
    # (the auto-skip check above will then short-circuit them).
    _mark_briefing_done()
    log.info(f"morning_briefing ({lang}); prefetched={task is not None}")
    await ws.send_json({"type": "status", "state": "thinking"})
    try:
        if task is not None:
            response_text, audios = await task   # prepared during the boot screen
        else:
            response_text, audios = await _prepare_briefing(lang)
    except Exception as e:
        log.warning(f"briefing failed: {e}")
        response_text, audios = ("Good morning, sir. I couldn't assemble the briefing just now.", [])

    # (Portfolio dashboard window opening removed at user request — the briefing
    # still mentions the portfolio numbers, it just no longer opens a window.)

    try:
        await ws.send_json({"type": "status", "state": "speaking"})
        if audios:
            # Concatenate the parallel-synthesized mp3 chunks into ONE blob, then
            # speak it (lip-synced via the live stream when one is open).
            combined = b"".join(audios)
            await _speak_briefing(ws, voice_state, lang, combined, response_text)
        else:
            await ws.send_json({"type": "text", "text": response_text})
    except Exception:
        pass

    # End of briefing → look at the user and comment on their state/outfit (with
    # the persona's wit). The long briefing is still playing while we capture +
    # describe, so the comment is enqueued right after it with no gap. onFinished
    # starts the mic once the comment finishes.
    cam_text, cam_audio = await _camera_comment(ws, pending_frames, lang)
    try:
        if cam_audio:
            await ws.send_json({"type": "audio", "data": base64.b64encode(cam_audio).decode(),
                                "text": cam_text})
        elif not audios:
            await ws.send_json({"type": "status", "state": "idle"})
    except Exception:
        pass

    if history is not None:
        history.append({"role": "assistant", "content": f"[morning briefing]: {response_text}"
                                                          + (f" [look] {cam_text}" if cam_text else "")})
    log.info(f"Briefing delivered ({lang}): {response_text[:80]}")


def get_lookup_status() -> str:
    """Get status of active lookups for when user asks 'how's that coming'."""
    if not _active_lookups:
        return ""
    active = [v for v in _active_lookups.values() if v["status"] == "working"]
    if not active:
        return ""
    parts = []
    for lookup in active:
        elapsed = int(time.time() - lookup["started"])
        parts.append(f"{lookup['type']} check ({elapsed}s)")
    return "Currently working on: " + ", ".join(parts)


def _short_sender(sender: str) -> str:
    """Extract just the name from an email sender string."""
    if "<" in sender:
        return sender.split("<")[0].strip().strip('"')
    if "@" in sender:
        return sender.split("@")[0]
    return sender


async def handle_browse(text: str, target: str) -> str:
    """Open a URL directly or search. Smart about detecting URLs in speech."""
    import re
    from urllib.parse import quote

    browser = "firefox" if "firefox" in text.lower() else "chrome"
    combined = text.lower()

    # 1. Try to find a URL or domain in the text
    # Match things like "joetmd.com", "google.com/maps", "https://example.com"
    url_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})+(?:/[^\s]*)?)'
    url_match = re.search(url_pattern, text, re.IGNORECASE)

    if url_match:
        domain = url_match.group(0)
        if not domain.startswith("http"):
            domain = "https://" + domain
        await open_browser(domain, browser)
        return f"Opened {url_match.group(0)}, sir."

    # 2. Check for spoken domains that speech-to-text mangled
    # "Joe tmd.com" → "joetmd.com", "roofo.co" etc.
    # Try joining words that end/start with a dot pattern
    words = text.split()
    for i, word in enumerate(words):
        # Look for word ending with common TLD
        if re.search(r'\.(com|co|io|ai|org|net|dev|app)$', word, re.IGNORECASE):
            # This word IS a domain — might have spaces before it
            domain = word
            # Check if previous word should be joined (e.g., "Joe tmd.com" → "joetmd.com" is tricky)
            if not domain.startswith("http"):
                domain = "https://" + domain
            await open_browser(domain, browser)
            return f"Opened {word}, sir."

    # 3. Fall back to Google search with cleaned query
    query = target
    for prefix in ["search for", "look up", "google", "find me", "pull up", "open chrome",
                    "open firefox", "open browser", "go to", "can you", "in the browser",
                    "can you go to", "please"]:
        query = query.lower().replace(prefix, "").strip()
    # Remove filler words
    query = re.sub(r'\b(can|you|the|in|to|a|an|for|me|my|please)\b', '', query).strip()
    query = re.sub(r'\s+', ' ', query).strip()

    if not query:
        query = target

    url = f"https://www.google.com/search?q={quote(query)}"
    await open_browser(url, browser)
    return "Searching for that, sir."


async def handle_research(text: str, target: str, client: anthropic.AsyncAnthropic) -> str:
    """Deep research with Opus — write results to HTML, open in browser."""
    try:
        research_response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=2000,
            system=f"You are JARVIS, researching a topic for {USER_NAME}. Be thorough, organized, and cite sources where possible.",
            messages=[{"role": "user", "content": f"Research this thoroughly:\n\n{target}"}],
        )
        research_text = research_response.content[0].text

        import html as _html
        html_content = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>JARVIS Research: {_html.escape(target[:60])}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #0a0a0a; color: #e0e0e0; line-height: 1.7; }}
h1 {{ color: #0ea5e9; font-size: 1.4em; border-bottom: 1px solid #222; padding-bottom: 10px; }}
h2 {{ color: #38bdf8; font-size: 1.1em; margin-top: 24px; }}
a {{ color: #0ea5e9; }}
pre {{ background: #111; padding: 12px; border-radius: 6px; overflow-x: auto; }}
code {{ background: #111; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
blockquote {{ border-left: 3px solid #0ea5e9; margin-left: 0; padding-left: 16px; color: #aaa; }}
</style>
</head><body>
<h1>Research: {_html.escape(target[:80])}</h1>
<div>{research_text.replace(chr(10), '<br>')}</div>
<hr style="border-color:#222;margin-top:40px">
<p style="color:#555;font-size:0.8em">Researched by JARVIS using Claude Opus &bull; {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
</body></html>"""

        results_file = Path.home() / "Desktop" / ".jarvis_research.html"
        results_file.write_text(html_content)

        browser_name = "firefox" if "firefox" in text.lower() else "chrome"
        await open_browser(f"file://{results_file}", browser_name)

        # Short voice summary via Haiku
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="Summarize this research in ONE sentence for voice. No markdown.",
            messages=[{"role": "user", "content": research_text[:2000]}],
        )
        return summary.content[0].text + " Full results are in your browser, sir."

    except Exception as e:
        log.error(f"Research failed: {e}")
        from urllib.parse import quote
        await open_browser(f"https://www.google.com/search?q={quote(target)}")
        return "Pulled up a search for that, sir."


# -- Session Summary (Three-Tier Memory) -----------------------------------

async def _update_session_summary(
    old_summary: str,
    rotated_messages: list[dict],
    client: anthropic.AsyncAnthropic,
) -> str:
    """Background Haiku call to update the rolling session summary."""
    prompt = f"""Update this conversation summary to include the new messages.

Current summary: {old_summary or '(start of conversation)'}

New messages to incorporate:
{chr(10).join(f'{m["role"]}: {m["content"][:200]}' for m in rotated_messages)}

Write an updated summary in 2-4 sentences capturing the key topics, decisions, and context. Be concise."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning(f"Summary update failed: {e}")
        return old_summary  # Keep old summary on failure


# -- WebSocket Voice Handler -----------------------------------------------

@app.websocket("/ws/voice")
async def voice_handler(ws: WebSocket):
    """
    WebSocket protocol:

    Client -> Server:
        {"type": "transcript", "text": "...", "isFinal": true}

    Server -> Client:
        {"type": "audio", "data": "<base64 mp3>", "text": "spoken text"}
        {"type": "status", "state": "thinking"|"speaking"|"idle"|"working"}
        {"type": "task_spawned", "task_id": "...", "prompt": "..."}
        {"type": "task_complete", "task_id": "...", "summary": "..."}
    """
    await ws.accept()
    task_manager.register_websocket(ws)
    history: list[dict] = []
    work_session = WorkSession()
    planner = TaskPlanner()

    # Response cancellation — when new input arrives, cancel current response
    _current_response_id = 0
    _cancel_response = False

    # Audio collision prevention — track when user last spoke
    voice_state = {"last_user_time": 0.0}

    # Pending webcam frame requests — request_id -> Future resolved by the
    # browser's "camera_frame" reply (see request_camera_frame).
    pending_frames: dict[str, asyncio.Future] = {}

    # Self-awareness — track last spoken response to avoid repetition
    last_jarvis_response = ""

    # Three-tier conversation memory
    session_buffer: list[dict] = []  # ALL messages, never truncated
    session_summary: str = ""  # Rolling summary of older conversation
    summary_update_pending: bool = False
    messages_since_last_summary: int = 0

    log.info("Voice WebSocket connected")

    try:
        # ── Greeting — always start in conversation mode ──
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            greeting = "Good morning, sir."
        elif hour < 17:
            greeting = "Good afternoon, sir."
        else:
            greeting = "Good evening, sir."

        global _last_greeting_time
        should_greet = (time.time() - _last_greeting_time) > 60

        if should_greet:
            _last_greeting_time = time.time()

            async def _send_greeting():
                try:
                    audio_bytes = await synthesize_speech(greeting)
                    if audio_bytes:
                        encoded = base64.b64encode(audio_bytes).decode()
                        await ws.send_json({"type": "status", "state": "speaking"})
                        await ws.send_json({"type": "audio", "data": encoded, "text": greeting})
                        history.append({"role": "assistant", "content": greeting})
                        log.info(f"JARVIS: {greeting}")
                        await ws.send_json({"type": "status", "state": "idle"})
                except Exception as e:
                    log.warning(f"Greeting failed: {e}")

            asyncio.create_task(_send_greeting())

        try:
            await ws.send_json({"type": "status", "state": "idle"})
        except Exception:
            return  # WebSocket already gone

        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break

            # ── Audio utterance (binary) → Whisper (forced lang if set) ──
            if message.get("bytes") is not None:
                forced = voice_state.get("forced_lang")
                text, utter_lang = await transcribe_audio(message["bytes"], lang=forced)
                user_text = apply_speech_corrections(text.strip())
                if not user_text:
                    # Nothing intelligible — return the UI to idle so the mic
                    # keeps listening instead of being stuck on "thinking".
                    try:
                        await ws.send_json({"type": "status", "state": "idle"})
                    except Exception:
                        pass
                    continue
            else:
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # ── Webcam frame reply: resolve the waiting request_camera_frame ──
                if msg.get("type") == "camera_frame":
                    rid = msg.get("request_id")
                    fut = pending_frames.get(rid)
                    if fut and not fut.done():
                        fut.set_result(msg.get("data") or None)
                    continue

                # ── Language toggle: force Whisper + replies to a language ──
                if msg.get("type") == "set_lang":
                    lg = msg.get("lang")
                    voice_state["forced_lang"] = lg if lg in ("en", "fr", "tr") else None
                    log.info(f"Forced language set to: {voice_state.get('forced_lang')}")
                    continue

                # ── D-ID live stream lifecycle: the browser owns the WebRTC peer
                #    connection and tells us its active stream so the reply path
                #    can push lip-sync audio into it (instead of base64 audio). ──
                if msg.get("type") == "did_stream_ready":
                    voice_state["did_stream"] = {"id": msg.get("stream_id"), "session_id": msg.get("session_id")}
                    log.info(f"D-ID live stream ready: {msg.get('stream_id')}")
                    continue
                if msg.get("type") == "did_stream_closed":
                    voice_state.pop("did_stream", None)
                    continue

                # ── Surveillance: a webcam frame from the browser (motion-gated).
                #    Recognise off the WS loop so it never delays voice. ──
                if msg.get("type") == "watch_frame":
                    if _surveillance["on"]:
                        try:
                            _data = (msg.get("data") or "").split(",")[-1]
                            _frame = base64.b64decode(_data)
                            asyncio.create_task(_recognize_and_greet(_frame))
                        except Exception:
                            pass
                    continue

                # ── Briefing prefetch: start gathering DURING the boot screen so
                #    the briefing is ready the instant the boot finishes. ──
                if msg.get("type") == "briefing_prefetch":
                    if _briefing_done_today():
                        log.info("Skipping briefing prefetch — already delivered today")
                        continue
                    pf_lang = voice_state.get("forced_lang") or voice_state.get("lang") or "en"
                    voice_state["briefing_task"] = asyncio.create_task(_prepare_briefing(pf_lang))
                    log.info(f"Briefing prefetch started ({pf_lang})")
                    continue

                # ── Morning briefing: triggered automatically by the frontend after
                #    startup (auto=True → skipped if already done today). Explicit
                #    "brief me" requests below run unconditionally. ──
                if msg.get("type") == "briefing":
                    asyncio.create_task(morning_briefing(ws, history=history, voice_state=voice_state, auto=True, pending_frames=pending_frames))
                    continue

                # ── Fix-self: activate work mode in JARVIS repo ──
                if msg.get("type") == "fix_self":
                    jarvis_dir = str(Path(__file__).parent)
                    await work_session.start(jarvis_dir)
                    response_text = "Work mode active in my own repo, sir. Tell me what needs fixing."
                    tts = strip_markdown_for_tts(response_text)
                    await ws.send_json({"type": "status", "state": "speaking"})
                    audio = await synthesize_speech(tts)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                    else:
                        await ws.send_json({"type": "text", "text": response_text})
                    continue

                # ── Legacy/browser STT transcript (English fallback path) ──
                if msg.get("type") != "transcript" or not msg.get("isFinal"):
                    continue
                user_text = apply_speech_corrections(msg.get("text", "").strip())
                utter_lang = "en"
                if not user_text:
                    continue

            # Track this utterance's language — drives reply language + TTS voice.
            voice_state["lang"] = utter_lang

            # Cancel any in-flight response
            _current_response_id += 1
            my_response_id = _current_response_id
            _cancel_response = True
            await asyncio.sleep(0.05)  # Let any pending sends notice the cancellation
            _cancel_response = False

            voice_state["last_user_time"] = time.time()
            log.info(f"User: {user_text}")

            # Wake-word gate: while music is actually playing, the mic re-hears the
            # speakers, so ignore anything that doesn't name Marion or command
            # playback — this breaks the music→mic→music feedback loop.
            if await _music_is_active() and not _passes_music_gate(user_text):
                log.info(f"[music-gate] ignored during playback: {user_text!r}")
                # Critical: the frontend already flipped to "thinking" when it sent
                # this utterance. Send it back to idle so the mic resumes — without
                # this it hangs forever on "listening/thinking" (the reported bug).
                try:
                    await ws.send_json({"type": "status", "state": "idle"})
                except Exception:
                    pass
                continue

            await ws.send_json({"type": "status", "state": "thinking"})

            # Lazy project scan on first message
            global cached_projects
            if not cached_projects:
                try:
                    # Run in executor since scan_projects does sync file I/O
                    loop = asyncio.get_event_loop()
                    cached_projects = await asyncio.wait_for(
                        loop.run_in_executor(None, _scan_projects_sync),
                        timeout=3
                    )
                    log.info(f"Scanned {len(cached_projects)} projects")
                except Exception:
                    cached_projects = []

            try:
                # ── CHECK FOR MODE SWITCHES ──
                t_lower = user_text.lower()

                # ── PLANNING MODE: answering clarifying questions ──
                if planner.is_planning:
                    # Check for bypass
                    if any(p in t_lower for p in BYPASS_PHRASES):
                        plan = planner.active_plan
                        if plan:
                            plan.skipped = True
                            for q in plan.pending_questions[plan.current_question_index:]:
                                if q.get("default") is not None and q["key"] not in plan.answers:
                                    plan.answers[q["key"]] = q["default"]
                        prompt = await planner.build_prompt()
                        name = _generate_project_name(prompt)
                        path = str(Path.home() / "Desktop" / name)
                        os.makedirs(path, exist_ok=True)
                        Path(path, "CLAUDE.md").write_text(prompt)
                        did = dispatch_registry.register(name, path, prompt[:200])
                        asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                        planner.reset()
                        response_text = "Building it now, sir."
                    elif planner.active_plan and planner.active_plan.confirmed is False and planner.active_plan.current_question_index >= len(planner.active_plan.pending_questions):
                        # Confirmation phase
                        result = await planner.handle_confirmation(user_text)
                        if result["confirmed"]:
                            prompt = await planner.build_prompt()
                            name = _generate_project_name(prompt)
                            path = str(Path.home() / "Desktop" / name)
                            os.makedirs(path, exist_ok=True)
                            Path(path, "CLAUDE.md").write_text(prompt)
                            did = dispatch_registry.register(name, path, prompt[:200])
                            asyncio.create_task(_execute_prompt_project(name, prompt, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state))
                            planner.reset()
                            response_text = "On it, sir."
                        elif result["cancelled"]:
                            planner.reset()
                            response_text = "Cancelled, sir."
                        else:
                            response_text = result.get("modification_question", "How shall I adjust the plan, sir?")
                    else:
                        result = await planner.process_answer(user_text, cached_projects)
                        if result["plan_complete"]:
                            response_text = result.get("confirmation_summary", "Ready to build. Shall I proceed, sir?")
                        else:
                            response_text = result.get("next_question", "What else, sir?")

                # ── "Let's go to hell" — easter egg: replay the boot jingle. ──
                elif "go to hell" in t_lower:
                    try:
                        await ws.send_json({"type": "play_music"})
                    except Exception:
                        pass
                    response_text = {"fr": "Comme tu veux, mon amour.",
                                     "tr": "Nasıl istersen canım."}.get(
                        voice_state.get("lang", "en"), "As you wish, sir.")

                # ── Surveillance mode on/off (voice) — webcam face watch ──
                elif any(p in t_lower for p in (
                    "active la surveillance", "démarre la surveillance", "demarre la surveillance",
                    "lance la surveillance", "mode surveillance", "surveille la maison",
                    "mets la surveillance", "active la caméra de surveillance",
                )):
                    _surveillance["on"] = True
                    try:
                        await ws.send_json({"type": "watch_mode", "on": True})
                    except Exception:
                        pass
                    response_text = {"fr": "Surveillance activée, mon amour. Je veille sur la maison.",
                                     "tr": "Gözetim açık canım."}.get(
                        voice_state.get("lang", "en"), "Surveillance on, sir.")

                elif any(p in t_lower for p in (
                    "désactive la surveillance", "desactive la surveillance",
                    "arrête la surveillance", "arrete la surveillance",
                    "coupe la surveillance", "stop la surveillance",
                )):
                    _surveillance["on"] = False
                    try:
                        await ws.send_json({"type": "watch_mode", "on": False})
                    except Exception:
                        pass
                    response_text = {"fr": "Surveillance désactivée.",
                                     "tr": "Gözetim kapalı canım."}.get(
                        voice_state.get("lang", "en"), "Surveillance off, sir.")

                # ── Real music control via Spotify (artist/title/playlist,
                # pause, next). spotify_access.parse_command extracts the query
                # from natural speech in FR/TR/EN. ──
                elif (_music := spotify_access.parse_command(user_text)) is not None:
                    _act, _query = _music
                    _lang = voice_state.get("lang", "en")
                    if not spotify_access.is_configured():
                        response_text = {
                            "fr": "Spotify n'est pas connecté, mon amour.",
                            "tr": "Spotify bağlı değil canım.",
                        }.get(_lang, "Spotify isn't connected, sir.")
                    else:
                        try:
                            if _act == "pause":
                                _res = await spotify_access.pause()
                                _ok = {"fr": "Musique en pause.", "tr": "Müzik durduruldu."}.get(_lang, "Paused, sir.")
                            elif _act == "next":
                                _res = await spotify_access.next_track()
                                _ok = {"fr": "Chanson suivante.", "tr": "Sonraki şarkı."}.get(_lang, "Skipped, sir.")
                            elif _query:
                                _res = await spotify_access.play(_query)
                                _ok = {"fr": f"C'est parti : {_res.detail}.",
                                       "tr": f"Çalıyorum: {_res.detail}."}.get(_lang, f"Playing {_res.detail}, sir.")
                            else:
                                _res = await spotify_access.play(None)
                                _ok = {"fr": "Je relance la musique.", "tr": "Müziğe devam."}.get(_lang, "Resuming, sir.")
                            if _res.ok:
                                response_text = _ok
                                _set_music_playing(_act != "pause")
                            else:
                                response_text = {
                                    "fr": f"Je n'ai pas réussi, mon amour — {_res.detail}.",
                                    "tr": f"Olmadı canım — {_res.detail}.",
                                }.get(_lang, f"I couldn't start playback, sir — {_res.detail}.")
                        except Exception as _e:
                            log.warning(f"Spotify command failed: {_e}")
                            response_text = {
                                "fr": "J'ai eu un souci avec Spotify, mon amour.",
                                "tr": "Spotify ile bir sorun oldu canım.",
                            }.get(_lang, "I hit a Spotify error, sir.")

                elif any(w in t_lower for w in ["quit work mode", "exit work mode", "go back to chat", "regular mode", "stop working"]):
                    if work_session.active:
                        await work_session.stop()
                        response_text = "Back to conversation mode, sir."
                    else:
                        response_text = "Already in conversation mode, sir."

                # ── WORK MODE: speech → claude -p → Haiku summary → JARVIS voice ──
                elif work_session.active:
                    if is_casual_question(user_text):
                        # Quick chat — bypass claude -p, use Haiku
                        response_text = await generate_response(
                            user_text, anthropic_client, task_manager,
                            cached_projects, history,
                            last_response=last_jarvis_response,
                            session_summary=session_summary,
                            lang=voice_state.get("lang", "en"),
                        )
                    else:
                        # Send to claude -p (full power)
                        await ws.send_json({"type": "status", "state": "working"})
                        log.info(f"Work mode → claude -p: {user_text[:80]}")

                        full_response = await work_session.send(user_text)

                        # Detect if Claude Code is stalling (asking questions instead of building)
                        if full_response and anthropic_client:
                            stall_words = ["which option", "would you prefer", "would you like me to",
                                           "before I proceed", "before proceeding", "should I",
                                           "do you want me to", "let me know", "please confirm",
                                           "which approach", "what would you"]
                            is_stalling = any(w in full_response.lower() for w in stall_words)
                            if is_stalling and work_session._message_count >= 2:
                                # Claude Code keeps asking — push it to build
                                log.info("Claude Code stalling — pushing to build")
                                push_response = await work_session.send(
                                    "Stop asking questions. Use your best judgment and start building now. "
                                    "Write the actual code files. Go with the simplest reasonable approach."
                                )
                                if push_response:
                                    full_response = push_response

                        # Auto-open any localhost URLs Claude Code mentions
                        import re as _re
                        localhost_match = _re.search(r'https?://localhost:\d+', full_response or "")
                        if localhost_match:
                            asyncio.create_task(_execute_browse(localhost_match.group(0)))
                            log.info(f"Auto-opening {localhost_match.group(0)}")

                        # Always summarize work mode responses via Haiku
                        if full_response and anthropic_client:
                            try:
                                summary = await anthropic_client.messages.create(
                                    model="claude-haiku-4-5-20251001",
                                    max_tokens=100,
                                    system=(
                                        f"You are JARVIS reporting to the user ({USER_NAME}). Summarize what happened in 1-2 sentences. "
                                        "Speak in first person — 'I built', 'I found', 'I set up'. "
                                        "You are talking TO THE USER, not to a coding tool. "
                                        "NEVER give instructions like 'go ahead and build' or 'set up the frontend' — those are NOT for the user. "
                                        "NEVER say 'Claude Code'. NEVER output [ACTION:...] tags. "
                                        "NEVER read out URLs. No markdown. British precision."
                                    ),
                                    messages=[{"role": "user", "content": f"Claude Code said:\n{full_response[:2000]}"}],
                                )
                                response_text = summary.content[0].text
                            except Exception:
                                response_text = full_response[:200]
                        else:
                            response_text = full_response

                # ── CHAT MODE: fast keyword detection + Haiku ──
                else:
                    # Fast keyword actions are English-only; for French/Turkish go
                    # straight to the LLM so the reply comes back in-language.
                    action = detect_action_fast(user_text) if voice_state.get("lang", "en") == "en" else None

                    if action:
                        if action["action"] == "open_terminal":
                            response_text = await handle_open_terminal()
                        elif action["action"] == "show_recent":
                            response_text = await handle_show_recent()
                        elif action["action"] == "describe_screen":
                            response_text = "Taking a look now, sir."
                            asyncio.create_task(_lookup_and_report("screen", lambda: _do_screen_lookup(voice_state.get("lang", "en")), ws, history=history, voice_state=voice_state))
                        elif action["action"] == "describe_camera":
                            response_text = "Let me have a look, sir."
                            asyncio.create_task(_lookup_and_report("camera", lambda: _do_camera_lookup(ws, pending_frames, voice_state.get("lang", "en")), ws, history=history, voice_state=voice_state))
                        elif action["action"] == "market_sentiment":
                            response_text = "Checking the crypto mood now, sir."
                            asyncio.create_task(_lookup_and_report("sentiment", _do_sentiment_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "briefing":
                            response_text = "Preparing your morning briefing, sir."
                            asyncio.create_task(morning_briefing(ws, history=history, voice_state=voice_state, pending_frames=pending_frames))
                        elif action["action"] == "check_calendar":
                            response_text = "Checking your calendar now, sir."
                            asyncio.create_task(_lookup_and_report("calendar", _do_calendar_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_mail":
                            response_text = "Checking your inbox now, sir."
                            asyncio.create_task(_lookup_and_report("mail", _do_mail_lookup, ws, history=history, voice_state=voice_state))
                        elif action["action"] == "check_dispatch":
                            recent = dispatch_registry.get_most_recent()
                            if not recent:
                                response_text = "No recent builds on record, sir."
                            else:
                                name = recent["project_name"]
                                status = recent["status"]
                                if status == "building" or status == "pending":
                                    elapsed = int(time.time() - recent["updated_at"])
                                    response_text = f"Still working on {name}, sir. Been at it for {elapsed} seconds."
                                elif status == "completed":
                                    response_text = recent.get("summary") or f"{name} is complete, sir."
                                elif status in ("failed", "timeout"):
                                    response_text = f"{name} ran into problems, sir."
                                else:
                                    response_text = f"{name} is {status}, sir."
                        elif action["action"] == "check_tasks":
                            tasks = get_open_tasks()
                            response_text = format_tasks_for_voice(tasks)
                        elif action["action"] == "check_usage":
                            response_text = get_usage_summary()
                        else:
                            response_text = "Understood, sir."
                    else:
                        if not anthropic_client:
                            response_text = "API key not configured."
                        else:
                            response_text = await generate_response(
                                user_text, anthropic_client, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                                lang=voice_state.get("lang", "en"),
                            )

                            # Check for action tags embedded in LLM response
                            clean_response, embedded_action = extract_action(response_text)
                            if embedded_action:
                                log.info(f"LLM embedded action: {embedded_action}")
                                response_text = clean_response
                                # Ensure there's always something to speak
                                if not response_text.strip():
                                    action_type = embedded_action["action"]
                                    _lg = voice_state.get("lang", "en")
                                    if action_type == "prompt_project":
                                        proj = embedded_action["target"].split("|||")[0].strip()
                                        response_text = ({"fr": f"Connexion à {proj}, mon amour.",
                                                          "tr": f"{proj} bağlanıyorum, canım."}
                                                         .get(_lg, f"Connecting to {proj} now, sir."))
                                    elif action_type == "build":
                                        response_text = {"fr": "Je m'en occupe, mon amour.",
                                                         "tr": "Hallediyorum, canım."}.get(_lg, "On it, sir.")
                                    elif action_type == "research":
                                        response_text = {"fr": "Je me renseigne, mon amour.",
                                                         "tr": "Araştırıyorum, canım."}.get(_lg, "Looking into that now, sir.")
                                    else:
                                        response_text = {"fr": "Tout de suite, mon amour.",
                                                         "tr": "Hemen, canım."}.get(_lg, "Right away, sir.")

                                if embedded_action["action"] == "build":
                                    # Build in background — JARVIS stays conversational
                                    target = embedded_action["target"]
                                    name = _generate_project_name(target)
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)

                                    # Write detailed CLAUDE.md
                                    Path(path, "CLAUDE.md").write_text(
                                        f"# Task\n\n{target}\n\n"
                                        "## Instructions\n"
                                        "- BUILD THIS NOW. Do not ask clarifying questions.\n"
                                        "- Use your best judgment for any design/architecture decisions.\n"
                                        "- Write complete, working code files — not plans or specs.\n"
                                        "- If it's a web app: use React + Vite + Tailwind unless specified otherwise.\n"
                                        "- Make it look polished and professional. Modern UI, clean layout.\n"
                                        "- Ensure it runs with a single command (npm run dev or similar).\n"
                                        "- If you reference a real product's UI (e.g. 'Zillow clone'), match their actual layout and features closely.\n"
                                        "- Use realistic mock data, not placeholder Lorem Ipsum.\n"
                                        "- After building, start the dev server and verify the app loads without errors.\n"
                                        "- IMPORTANT: Your LAST line of output MUST be exactly: RUNNING_AT=http://localhost:PORT (the actual port the dev server is using)\n"
                                    )

                                    # Register and dispatch
                                    did = dispatch_registry.register(name, path, target)
                                    asyncio.create_task(
                                        _execute_prompt_project(name, target, work_session, ws, dispatch_id=did, history=history, voice_state=voice_state)
                                    )
                                elif embedded_action["action"] == "browse":
                                    asyncio.create_task(_execute_browse(embedded_action["target"]))
                                elif embedded_action["action"] == "research":
                                    # Research enters work mode too
                                    name = _generate_project_name(embedded_action["target"])
                                    path = str(Path.home() / "Desktop" / name)
                                    os.makedirs(path, exist_ok=True)
                                    await work_session.start(path)
                                    asyncio.create_task(
                                        self_work_and_notify(work_session, embedded_action["target"], ws)
                                    )
                                elif embedded_action["action"] == "open_terminal":
                                    asyncio.create_task(_execute_open_terminal())
                                elif embedded_action["action"] == "prompt_project":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        proj_name, _, prompt = target.partition("|||")
                                        proj_name = proj_name.strip()
                                        prompt = prompt.strip()
                                        # Check for recent completed dispatch before re-dispatching
                                        recent = dispatch_registry.get_recent_for_project(proj_name)
                                        if recent and recent.get("summary"):
                                            log.info(f"Using recent dispatch result for {proj_name} instead of re-dispatching")
                                            response_text = recent["summary"]
                                            history.append({"role": "assistant", "content": f"[Previous dispatch result for {proj_name}]: {recent['summary']}"})
                                        else:
                                            asyncio.create_task(
                                                _execute_prompt_project(proj_name, prompt, work_session, ws, history=history, voice_state=voice_state)
                                            )
                                    else:
                                        log.warning(f"PROMPT_PROJECT missing ||| delimiter: {target}")
                                elif embedded_action["action"] == "add_task":
                                    target = embedded_action["target"]
                                    parts = target.split("|||")
                                    if len(parts) >= 2:
                                        priority = parts[0].strip() or "medium"
                                        title = parts[1].strip()
                                        desc = parts[2].strip() if len(parts) > 2 else ""
                                        due = parts[3].strip() if len(parts) > 3 else ""
                                        create_task(title=title, description=desc, priority=priority, due_date=due)
                                        log.info(f"Task created: {title}")
                                elif embedded_action["action"] == "add_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        topic, _, content = target.partition("|||")
                                        create_note(content=content.strip(), topic=topic.strip())
                                    else:
                                        create_note(content=target)
                                    log.info(f"Note created")
                                elif embedded_action["action"] == "complete_task":
                                    try:
                                        task_id = int(embedded_action["target"].strip())
                                        complete_task(task_id)
                                        log.info(f"Task {task_id} completed")
                                    except ValueError:
                                        pass
                                elif embedded_action["action"] == "remember":
                                    remember(embedded_action["target"].strip(), mem_type="fact", importance=7)
                                    log.info(f"Memory stored: {embedded_action['target'][:60]}")
                                elif embedded_action["action"] == "create_note":
                                    target = embedded_action["target"]
                                    if "|||" in target:
                                        title, _, body = target.partition("|||")
                                        asyncio.create_task(create_apple_note(title.strip(), body.strip()))
                                        log.info(f"Apple Note created: {title.strip()}")
                                    else:
                                        asyncio.create_task(create_apple_note("JARVIS Note", target))
                                elif embedded_action["action"] == "screen":
                                    asyncio.create_task(_lookup_and_report("screen", lambda: _do_screen_lookup(voice_state.get("lang", "en")), ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "camera":
                                    asyncio.create_task(_lookup_and_report("camera", lambda: _do_camera_lookup(ws, pending_frames, voice_state.get("lang", "en")), ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "sentiment":
                                    asyncio.create_task(_lookup_and_report("sentiment", _do_sentiment_lookup, ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "lights":
                                    # Marion's spoken reply already confirms; control runs in background.
                                    asyncio.create_task(_execute_lights(embedded_action["target"], voice_state, ws))
                                elif embedded_action["action"] == "gate":
                                    asyncio.create_task(_execute_gate(voice_state, ws))
                                elif embedded_action["action"] == "music":
                                    # Marion's spoken reply confirms; playback runs in background.
                                    asyncio.create_task(_execute_music(embedded_action["target"], voice_state, ws))
                                elif embedded_action["action"] == "news":
                                    _nlang = voice_state.get("lang", "en")
                                    _nq = embedded_action["target"].strip() or user_text
                                    asyncio.create_task(_lookup_and_report("news", lambda: _do_news_lookup(_nq, _nlang), ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "weather":
                                    _wlang = voice_state.get("lang", "en")
                                    _wplace = embedded_action["target"].strip()
                                    asyncio.create_task(_lookup_and_report("weather", lambda: _do_weather_lookup(_wplace, _wlang), ws, history=history, voice_state=voice_state))
                                elif embedded_action["action"] == "read_note":
                                    # Read note in background and report back
                                    async def _read_and_report(search_term, _ws):
                                        note = await read_note(search_term)
                                        if note:
                                            msg = f"Sir, your note '{note['title']}' says: {note['body'][:200]}"
                                        else:
                                            msg = f"Couldn't find a note matching '{search_term}', sir."
                                        audio = await synthesize_speech(strip_markdown_for_tts(msg))
                                        if audio and _ws:
                                            try:
                                                await _ws.send_json({"type": "status", "state": "speaking"})
                                                await _ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": msg})
                                            except Exception:
                                                pass
                                    asyncio.create_task(_read_and_report(embedded_action["target"].strip(), ws))

                # Update history
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": response_text})

                # Three-tier memory: also track in session buffer
                session_buffer.append({"role": "user", "content": user_text})
                session_buffer.append({"role": "assistant", "content": response_text})

                # Check if rolling summary needs updating
                messages_since_last_summary += 1
                if messages_since_last_summary >= 5 and len(history) > 20 and not summary_update_pending:
                    summary_update_pending = True
                    messages_since_last_summary = 0
                    # Get messages that are about to be rotated out
                    rotated = history[:-20] if len(history) > 20 else []
                    if rotated and anthropic_client:
                        async def _do_summary():
                            nonlocal session_summary, summary_update_pending
                            session_summary = await _update_session_summary(
                                session_summary, rotated, anthropic_client
                            )
                            summary_update_pending = False
                        asyncio.create_task(_do_summary())
                    else:
                        summary_update_pending = False

                # Extract memories in background (doesn't block response)
                if anthropic_client and len(user_text) > 15:
                    asyncio.create_task(extract_memories(user_text, response_text, anthropic_client))

                # TTS — voice follows the utterance's language (French → cloned voice)
                # Daily self-improvement digest — once per day, on the first reply
                # of the day, Marion mentions what she auto-adjusted.
                try:
                    _digest = self_eval.pop_due_digest(voice_state.get("lang", "en"))
                    if _digest:
                        response_text = f"{_digest} {response_text}"
                except Exception:
                    pass
                tts = strip_markdown_for_tts(response_text)
                _lang = voice_state.get("lang", "en")
                await ws.send_json({"type": "status", "state": "speaking"})
                audio = await synthesize_speech(tts, lang=_lang)
                if audio:
                    # FR/TR personas (Marion): if a live D-ID WebRTC stream is open,
                    # push the Fish audio into it → Marion lip-syncs in real time
                    # (~1-2s). The video arrives over the peer connection, so we send
                    # NO base64 audio and the frontend returns to idle on the stream's
                    # "done" event. Any failure / no stream → static face + plain audio.
                    spoke_via_stream = False
                    _stream = voice_state.get("did_stream") if _lang in ("fr", "tr") else None
                    log.info(f"reply lang={_lang} live_stream={'yes' if _stream else 'no'}")
                    if _stream and did_avatar.is_enabled():
                        _dur = await did_avatar.stream_speak(
                            _stream["id"], _stream["session_id"], audio)
                        log.info(f"stream_speak -> {_dur}")
                        if _dur:
                            spoke_via_stream = True
                            # Send the duration so the client resumes the mic exactly
                            # when she finishes — independent of D-ID's flaky events.
                            await ws.send_json({"type": "avatar_stream_speak", "text": response_text, "duration": _dur})
                    if not spoke_via_stream:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": response_text})
                else:
                    await ws.send_json({"type": "text", "text": response_text})
                    await ws.send_json({"type": "status", "state": "idle"})
                log.info(f"JARVIS: {response_text}")
                # Self-eval (Phase 1): record the turn + run the background critic
                # (auto-learns vocab/preferences, auto-tunes brevity). Fire-and-
                # forget — adds no latency and never breaks the reply.
                try:
                    await self_eval.record_turn(
                        anthropic_client,
                        user_text=user_text, reply_text=response_text,
                        lang=_lang, prev_reply=last_jarvis_response,
                    )
                except Exception:
                    pass
                last_jarvis_response = response_text

            except Exception as e:
                log.error(f"Error: {e}", exc_info=True)
                try:
                    fallback = "Something went wrong, sir."
                    audio = await synthesize_speech(fallback)
                    if audio:
                        await ws.send_json({"type": "audio", "data": base64.b64encode(audio).decode(), "text": fallback})
                    else:
                        await ws.send_json({"type": "audio", "data": "", "text": fallback})
                    # Let client's audioPlayer.onFinished handle idle transition
                except Exception:
                    pass

    except WebSocketDisconnect:
        log.info("Voice WebSocket disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        task_manager.unregister_websocket(ws)


# ---------------------------------------------------------------------------
# Settings / Configuration endpoints
# ---------------------------------------------------------------------------

def _env_file_path() -> Path:
    return Path(__file__).parent / ".env"

def _env_example_path() -> Path:
    return Path(__file__).parent / ".env.example"

def _read_env() -> tuple[list[str], dict[str, str]]:
    """Read .env file. Returns (raw_lines, parsed_dict). Creates from .env.example if missing."""
    path = _env_file_path()
    if not path.exists():
        example = _env_example_path()
        if example.exists():
            import shutil as _shutil
            _shutil.copy2(str(example), str(path))
        else:
            path.write_text("")
    lines = path.read_text().splitlines()
    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    return lines, parsed

def _write_env_key(key: str, value: str) -> None:
    """Update a single key in .env, preserving comments and order."""
    lines, _ = _read_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}")
                found = True
                continue
        new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    _env_file_path().write_text("\n".join(new_lines) + "\n")
    os.environ[key] = value

class KeyUpdate(BaseModel):
    key_name: str
    key_value: str

class KeyTest(BaseModel):
    key_value: str | None = None

class PreferencesUpdate(BaseModel):
    user_name: str = ""
    honorific: str = "sir"
    calendar_accounts: str = "auto"

@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID", "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS", "GOOGLE_MAPS_API_KEY", "DID_API_KEY"}
    if body.key_name not in allowed:
        return JSONResponse({"success": False, "error": "Invalid key name"}, status_code=400)
    _write_env_key(body.key_name, body.key_value)
    return {"success": True}

@app.post("/api/settings/test-ollama")
async def api_test_ollama(body: KeyTest):
    try:
        client = AsyncOpenAI(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
        )
        await client.chat.completions.create(
            model="gemma3:27b",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.post("/api/settings/test-fish")
async def api_test_fish(body: KeyTest):
    key = body.key_value or os.getenv("FISH_API_KEY", "")
    if not key:
        return {"valid": False, "error": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.fish.audio/v1/tts",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"text": "test", "reference_id": FISH_VOICE_ID},
            )
            if resp.status_code in (200, 201):
                return {"valid": True}
            elif resp.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}
            else:
                return {"valid": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}

@app.get("/api/settings/status")
async def api_settings_status():
    import shutil as _shutil
    _, env_dict = _read_env()
    claude_installed = _shutil.which("claude") is not None
    calendar_ok = mail_ok = notes_ok = False
    try: await get_todays_events(); calendar_ok = True
    except Exception: pass
    try: await get_unread_count(); mail_ok = True
    except Exception: pass
    try: await get_recent_notes(count=1); notes_ok = True
    except Exception: pass
    memory_count = task_count = 0
    try: memory_count = len(get_important_memories(limit=9999))
    except Exception: pass
    try: task_count = len(get_open_tasks())
    except Exception: pass
    return {
        "claude_code_installed": claude_installed,
        "calendar_accessible": calendar_ok,
        "mail_accessible": mail_ok,
        "notes_accessible": notes_ok,
        "memory_count": memory_count,
        "task_count": task_count,
        "server_port": 8340,
        "uptime_seconds": int(time.time() - _session_start),
        "env_keys_set": {
            "anthropic": bool(env_dict.get("ANTHROPIC_API_KEY", "").strip() and env_dict.get("ANTHROPIC_API_KEY", "") != "your-anthropic-api-key-here"),
            "fish_audio": bool(env_dict.get("FISH_API_KEY", "").strip() and env_dict.get("FISH_API_KEY", "") != "your-fish-audio-api-key-here"),
            "fish_voice_id": bool(env_dict.get("FISH_VOICE_ID", "").strip()),
            "user_name": env_dict.get("USER_NAME", ""),
        },
    }

@app.get("/api/settings/preferences")
async def api_get_preferences():
    _, env_dict = _read_env()
    return {
        "user_name": env_dict.get("USER_NAME", ""),
        "honorific": env_dict.get("HONORIFIC", "sir"),
        "calendar_accounts": env_dict.get("CALENDAR_ACCOUNTS", "auto"),
    }

@app.post("/api/settings/preferences")
async def api_save_preferences(body: PreferencesUpdate):
    _write_env_key("USER_NAME", body.user_name)
    _write_env_key("HONORIFIC", body.honorific)
    _write_env_key("CALENDAR_ACCOUNTS", body.calendar_accounts)
    return {"success": True}

# ---------------------------------------------------------------------------
# Control endpoints (restart, fix-self)
# ---------------------------------------------------------------------------

@app.post("/api/restart")
async def api_restart():
    """Restart the JARVIS server."""
    log.info("Restart requested — shutting down in 2 seconds")
    async def _restart():
        await asyncio.sleep(2)
        cmd = [sys.executable, __file__, "--port", "8340", "--host", "0.0.0.0"]
        os.execv(sys.executable, cmd)
    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/fix-self")
async def api_fix_self():
    """Enter work mode in the JARVIS repo — JARVIS can now fix himself."""
    jarvis_dir = str(Path(__file__).parent)
    # The work_session is per-WebSocket, so we set a flag that the handler picks up
    # For now, also open Terminal so user can see
    skip_flag = " --dangerously-skip-permissions" if _SKIP_PERMISSIONS else ""
    escaped_jarvis_dir = applescript_escape(jarvis_dir)
    script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'    do script "cd {escaped_jarvis_dir} && claude{skip_flag}"\n'
        'end tell'
    )
    await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.info("Work mode: JARVIS repo opened for self-improvement")
    return {"status": "work_mode_active", "path": jarvis_dir}


# ---------------------------------------------------------------------------
# Static file serving (frontend)
# ---------------------------------------------------------------------------

from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    _NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/")
    async def serve_index():
        # Never cache index.html — it references hash-named bundles that change on
        # every build; a stale index points at a deleted bundle and the app dies.
        return FileResponse(str(FRONTEND_DIST / "index.html"), headers=_NOCACHE)

    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    # Serve the built public files (marion-cutout.png, boot_*.mp3, boot_cue.json…)
    # so the production URL (:8340) is fully self-contained — no vite dev server,
    # hence no HMR page reloads mid-build. Defined LAST so it never shadows the
    # /api and /ws routes above; unknown paths fall back to index.html (SPA).
    @app.get("/{path:path}")
    async def serve_static(path: str):
        candidate = (FRONTEND_DIST / path).resolve()
        if FRONTEND_DIST.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIST / "index.html"), headers=_NOCACHE)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="JARVIS Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8340, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    parser.add_argument("--ssl", action="store_true", help="Enable HTTPS with key.pem/cert.pem")
    args = parser.parse_args()

    # Auto-detect SSL certs
    cert_file = Path(__file__).parent / "cert.pem"
    key_file = Path(__file__).parent / "key.pem"
    use_ssl = args.ssl or (cert_file.exists() and key_file.exists())

    proto = "https" if use_ssl else "http"
    ws_proto = "wss" if use_ssl else "ws"

    print()
    print("  J.A.R.V.I.S. Server v0.1.0")
    print(f"  WebSocket: {ws_proto}://{args.host}:{args.port}/ws/voice")
    print(f"  REST API:  {proto}://{args.host}:{args.port}/api/")
    print(f"  Tasks:     {proto}://{args.host}:{args.port}/api/tasks")
    print()

    ssl_kwargs = {}
    if use_ssl:
        ssl_kwargs["ssl_keyfile"] = str(key_file)
        ssl_kwargs["ssl_certfile"] = str(cert_file)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        **ssl_kwargs,
    )
   