# src/rendering/rich_helpers.py
import re
import json
from telegram import Bot, Message
from src.config import CONFIG, Style
from src.typography import lite_math
from src.http_client import get_shared_client
from src.perf import timed
from src.rendering.html_views import smart_truncate_html

def convert_to_legacy_html(rich_html: str) -> str:
    """
    Converts advanced Rich HTML tags to standard, safe legacy Telegram HTML tags.
    """
    if not rich_html:
        return ""

    text = rich_html

    # Convert headers to standard bold tags
    text = re.sub(r'</?h[1-6](?:\s+[^>]*)?>', lambda m: "<b>" if not m.group(0).startswith("</") else "</b>\n", text)

    # Convert dividers to classic plain-text separators
    text = text.replace("<hr/>", "━━━━━━━━━━━━━━━━━━━━━━━━")
    text = text.replace("<hr>", "━━━━━━━━━━━━━━━━━━━━━━━━")

    # Convert lists to standard indented bullets
    text = re.sub(r'<li>', "  • ", text)
    text = re.sub(r'</li>', "\n", text)
    text = re.sub(r'</?u[lo](?:\s+[^>]*)?>', "", text)

    # Convert line breaks, paragraph tags, and image elements to standard formats
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    text = text.replace("<p>", "").replace("</p>", "\n")
    text = re.sub(r'<img[^>]*>', "", text)

    # Convert complex visual tables to aligned key-value summaries
    def table_sub(match):
        table_content = match.group(1)
        rows = re.findall(r'<tr>(.*?)</tr>', table_content, re.DOTALL)
        formatted_rows = []
        for row in rows:
            cells = re.findall(r'<td>(.*?)</td>', row, re.DOTALL)
            clean_cells = [re.sub(r'<[^>]*>', '', c).strip() for c in cells]
            if clean_cells:
                if len(clean_cells) == 2:
                    formatted_rows.append(f"  ├─ {clean_cells[0]}: <b>{clean_cells[1]}</b>")
                else:
                    formatted_rows.append("  " + " | ".join(clean_cells))
            if formatted_rows:
                formatted_rows[-1] = formatted_rows[-1].replace("├─", "└─")
        return "\n".join(formatted_rows)

    text = re.sub(r'<table>(.*?)</table>', table_sub, text, flags=re.DOTALL)

    # Safe allowed standard formatting elements supported by Telegram Bot API.
    # Preserves <tg-math> and <tg-math-block> so they render on mobile apps natively.
    supported_legacy_tags = [
        "b", "/b", "i", "/i", "u", "/u", "s", "/s", "tg-spoiler", "/tg-spoiler",
        "code", "/code", "pre", "/pre", "a", "/a", "blockquote", "/blockquote",
        "tg-math", "/tg-math", "tg-math-block", "/tg-math-block"
    ]

    def strip_unsupported(match):
        tag_full = match.group(0)
        tag_name_match = re.match(r'</?([a-zA-Z1-6-]+)', tag_full)
        if tag_name_match:
            tag_name = tag_name_match.group(1).lower()
            if tag_name in supported_legacy_tags or tag_full.startswith("<blockquote expandable"):
                return tag_full
        return ""

    text = re.sub(r'<[^>]*>', strip_unsupported, text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


async def send_rich_message_safe(bot, chat_id, html_content, reply_markup=None, reply_to_message_id=None, media_bytes=None, file_id=None, preserve_utility=False, **kwargs) -> Message:
    if not preserve_utility:
        try:
            from src.config import LAST_UTILITY_MID, NO_ANSWER_NUDGE_MIDS
            stale_utility_mid = LAST_UTILITY_MID.pop(chat_id, None)
            if stale_utility_mid:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=stale_utility_mid)
                except Exception:
                    pass

            # "No answer on file yet" nudges only ever cleaned themselves up via TTL or a
            # re-tap on the SAME question's link. If a DIFFERENT question's answer card lands
            # in this DM first, the nudge stays stranded between two unrelated cards. Since
            # NO_ANSWER_NUDGE_MIDS is keyed by (user_id, display_id), sweep every entry for
            # this chat (== user_id in a private DM) before sending anything new here.
            stale_nudge_keys = [k for k in NO_ANSWER_NUDGE_MIDS if isinstance(k, tuple) and k[0] == chat_id]
            for k in stale_nudge_keys:
                nudge_mid = NO_ANSWER_NUDGE_MIDS.pop(k, None)
                if nudge_mid:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=nudge_mid)
                    except Exception:
                        pass
        except Exception:
            pass

    normalized_content = html_content.replace("\r\n", "\n").replace("\r", "\n")
    has_media = (media_bytes is not None) or (file_id is not None)

    print(f"\033[96m[RICH MESSENGER]\033[0m Attempting rich delivery to Chat: {chat_id} (media present: {has_media}, file_id cached: {file_id is not None})", flush=True)

    # ...rest of the function is UNCHANGED — leave every line below this exactly as it is
    # --- TIER 1: native python-telegram-bot library helper ---
    for method_name in ["send_rich_message", "sendRichMessage"]:
        if hasattr(bot, method_name):
            try:
                with timed(f"TIER1 native {method_name} -> {chat_id}"):
                    method = getattr(bot, method_name)
                    rich_html = normalized_content.replace("\n", "<br/>")
                    media_arr = []
                    if has_media:
                        media_arr.append({
                            "id": "quiz_diagram",
                            "media": {
                                "type": "photo",
                                "media": file_id if file_id else "attach://quiz_diagram"
                            }
                        })
                    return await method(
                        chat_id=chat_id,
                        rich_message={
                            "html": rich_html,
                            "media": media_arr if has_media else None
                        },
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        **kwargs
                    )
            except Exception as e:
                print(f"{Style.YELLOW}[RICH MSG] Native client call failed: {e}. Trying HTTP raw fallback...{Style.RESET}", flush=True)

    # --- TIER 2: raw HTTP POST to /sendRichMessage using the SHARED pooled client ---
    try:
        client = get_shared_client()
        url = f"https://api.telegram.org/bot{bot.token}/sendRichMessage"
        rich_html = normalized_content.replace("\n", "<br/>")

        rich_message_dict = {
            "html": rich_html
        }
        if has_media:
            rich_message_dict["media"] = [
                {
                    "id": "quiz_diagram",
                    "media": {
                        "type": "photo",
                        "media": file_id if file_id else "attach://quiz_diagram"
                    }
                }
            ]

        data_payload = {
            "chat_id": str(chat_id),
            "rich_message": json.dumps(rich_message_dict)
        }
        if reply_to_message_id:
            data_payload["reply_to_message_id"] = str(reply_to_message_id)

        if reply_markup:
            data_payload["reply_markup"] = json.dumps(reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup)

        for k, v in kwargs.items():
            data_payload[k] = json.dumps(v.to_dict() if hasattr(v, "to_dict") else v)

        files_payload = {}
        if has_media and not file_id:
            files_payload["quiz_diagram"] = ("diagram.png", media_bytes, "image/png")

        with timed(f"TIER2 sendRichMessage -> {chat_id}"):
            resp = await client.post(url, data=data_payload, files=files_payload if (has_media and not file_id) else None, timeout=30.0)

        if resp.status_code == 200:
            resp_json = resp.json()
            if resp_json.get("ok"):
                return Message.de_json(resp_json["result"], bot)
        else:
            print(f"[RICH MSG] sendRichMessage raw HTTP returned status {resp.status_code}: {resp.text[:300]}", flush=True)
    except Exception as e:
        print(f"[RICH MSG] HTTP multipart fallback connection failed: {e}", flush=True)

    # --- TIER 3: legacy HTML delivery ---
    print(f"{Style.YELLOW}[RICH MSG] Falling back to standard HTML legacy delivery.{Style.RESET}", flush=True)
    legacy_html = convert_to_legacy_html(normalized_content)

    if has_media and len(legacy_html) > 1024:
        ref_match = re.search(r'REF\s*<code>(\d+)</code>', normalized_content)
        ref_str = ref_match.group(1) if ref_match else ""
        print(f"{Style.YELLOW}[CONSOLIDATED-FIX] Caption length ({len(legacy_html)}) exceeds 1024. Safely splitting into photo + detailed follow-up text. REF: {ref_str}{Style.RESET}", flush=True)

        short_caption = f"🎨 <b>DIAGRAM CANVAS • REF {ref_str}</b>"
        photo_msg = await bot.send_photo(
            chat_id=chat_id,
            photo=file_id if file_id else media_bytes,
            caption=short_caption,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        return await bot.send_message(
            chat_id=chat_id,
            text=legacy_html,
            parse_mode="HTML",
            reply_markup=reply_markup,
            reply_to_message_id=photo_msg.message_id,
            **kwargs
        )

    with timed(f"TIER3 legacy send -> {chat_id}"):
        if has_media:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id if file_id else media_bytes,
                caption=legacy_html,
                parse_mode="HTML",
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                **kwargs
            )
        else:
            return await bot.send_message(
                chat_id=chat_id,
                text=legacy_html,
                parse_mode="HTML",
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
                **kwargs
            )


async def edit_rich_message_safe(bot: Bot, chat_id, message_id, html_content: str, reply_markup=None, media_bytes=None, file_id=None, **kwargs) -> Message:
    normalized_content = html_content.replace("\r\n", "\n").replace("\r", "\n")
    rich_html = normalized_content.replace("\n", "<br/>")
    has_media = (media_bytes is not None) or (file_id is not None)

    print(f"\033[96m[RICH MESSENGER]\033[0m Editing active rich message state for Msg ID: {message_id} (media present: {has_media})", flush=True)

    def _is_not_modified(text: str) -> bool:
        return "message is not modified" in (text or "").lower()

    # --- TIER 1: raw HTTP POST with serialized JSON strings ---
    try:
        client = get_shared_client()

        endpoint = "editMessageCaption" if has_media else "editMessageText"
        url = f"https://api.telegram.org/bot{bot.token}/{endpoint}"

        rich_message_dict = {"html": rich_html}
        if has_media:
            rich_message_dict["media"] = [
                {"id": "quiz_diagram", "media": {"type": "photo", "media": file_id if file_id else "attach://quiz_diagram"}}
            ]

        data_payload = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "rich_message": json.dumps(rich_message_dict)
        }
        if reply_markup:
            data_payload["reply_markup"] = json.dumps(reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup)

        for k, v in kwargs.items():
            data_payload[k] = json.dumps(v.to_dict() if hasattr(v, "to_dict") else v)

        files_payload = {}
        if has_media and not file_id:
            files_payload["quiz_diagram"] = ("diagram.png", media_bytes, "image/png")

        print(f"[DEBUG-FIX-EDIT-API] Dispatching TIER 1 API request using endpoint: '{endpoint}' to modify message_id: {message_id}", flush=True)
        resp = await client.post(url, data=data_payload, files=files_payload if (has_media and not file_id) else None, timeout=30.0)

        if resp.status_code == 200:
            resp_json = resp.json()
            if resp_json.get("ok"):
                print(f"[DEBUG-FIX-SUCCESS] TIER 1 edit resolved successfully on primary endpoint '{endpoint}'.", flush=True)
                return Message.de_json(resp_json["result"], bot)
        elif _is_not_modified(resp.text):
            print(f"[RICH MSG] Edit skipped — message {message_id} content is already identical. No changes applied.", flush=True)
            return None

        fallback_endpoint = "editMessageText" if has_media else "editMessageCaption"
        print(f"[DEBUG-FIX-EDIT-FALLBACK] Primary endpoint '{endpoint}' failed (HTTP {resp.status_code}). Retrying with: '{fallback_endpoint}'", flush=True)
        fallback_url = f"https://api.telegram.org/bot{bot.token}/{fallback_endpoint}"
        resp = await client.post(fallback_url, data=data_payload, files=files_payload if (has_media and not file_id) else None, timeout=30.0)

        if resp.status_code == 200:
            resp_json = resp.json()
            if resp_json.get("ok"):
                print(f"[DEBUG-FIX-SUCCESS] TIER 1 edit resolved successfully on fallback endpoint '{fallback_endpoint}'.", flush=True)
                return Message.de_json(resp_json["result"], bot)
        elif _is_not_modified(resp.text):
            print(f"[RICH MSG] Edit skipped (fallback) — message {message_id} content is already identical.", flush=True)
            return None
        else:
            print(f"[RICH MSG] editMessage HTTP raw request returned status {resp.status_code}: {resp.text[:300]}", flush=True)
    except Exception as e:
        print(f"[RICH MSG] HTTP edit fallback connection failed: {e}", flush=True)

    # --- TIER 2: legacy HTML editing fallback ---
    legacy_html = convert_to_legacy_html(normalized_content)
    if has_media and len(legacy_html) > 1024:
        print(f"{Style.YELLOW}[CONSOLIDATED-FIX] Edit caption exceeds 1024 limit. Safely truncating to 1000 characters to prevent API overflow. Msg ID: {message_id}{Style.RESET}", flush=True)
        legacy_html = smart_truncate_html(legacy_html, 1000)

    print(f"[DEBUG-FIX-EDIT-FALLBACK] Falling back to TIER 2 legacy editing for message_id: {message_id}", flush=True)

    try:
        with timed(f"TIER2 legacy edit_message_text msg={message_id}"):
            return await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=legacy_html,
                parse_mode="HTML", reply_markup=reply_markup, **kwargs
            )
    except Exception as text_err:
        if _is_not_modified(str(text_err)):
            print(f"[RICH MSG] Legacy edit skipped — message {message_id} content is already identical.", flush=True)
            return None
        print(f"[DEBUG-FIX-EDIT-WARNING] edit_message_text failed: {text_err}. Retrying with edit_message_caption...", flush=True)
        try:
            with timed(f"TIER2 legacy edit_message_caption msg={message_id}"):
                return await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=legacy_html,
                    parse_mode="HTML", reply_markup=reply_markup, **kwargs
                )
        except Exception as cap_err:
            if _is_not_modified(str(cap_err)):
                print(f"[RICH MSG] Legacy caption edit skipped — message {message_id} content is already identical.", flush=True)
                return None
            print(f"[DEBUG-FIX-ERROR] Both TIER 2 legacy edit methods failed: {cap_err}", flush=True)
            raise text_err

async def open_utility_view(bot, last_utility_mid: dict, locks: dict, user_id, chat_id, html_content, reply_markup=None):
    """Ensures only ONE management/utility message (profile, settings, leaderboard,
    invite, help, feedback, admin panels, etc.) is ever visible in a chat at a time.
    Deletes whatever utility message preceded it, sends the new one, and tracks it.
    A per-user asyncio.Lock prevents two near-simultaneous taps from both reading the
    same stale tracked id and leaving an orphaned message behind."""
    import asyncio
    lock = locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        prev_mid = last_utility_mid.get(user_id)
        if prev_mid:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=prev_mid)
            except Exception:
                pass
        m = await send_rich_message_safe(bot, chat_id=chat_id, html_content=html_content, reply_markup=reply_markup, preserve_utility=True)
        if m:
            last_utility_mid[user_id] = m.message_id
        return m