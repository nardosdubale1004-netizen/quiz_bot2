# src/rendering/rich_helpers.py
import re
import json
import httpx
from telegram import Bot, Message
from src.config import Style
from src.typography import lite_math

def convert_to_legacy_html(rich_html: str) -> str:
    """
    Converts advanced 2026 Rich HTML tags to standard, safe legacy Telegram HTML tags
    to prevent entity parsing crashes on photo captions and fallback channels.
    """
    if not rich_html:
        return ""
    
    # 1. Convert native math blocks to monospaced pre blocks
    def math_block_sub(match):
        expr = match.group(1).strip()
        return f"<pre>{lite_math(expr)}</pre>"
    text = re.sub(r'<tg-math-block>(.*?)</tg-math-block>', math_block_sub, rich_html, flags=re.DOTALL)
    
    # 2. Convert native inline math to inline code blocks
    def math_inline_sub(match):
        expr = match.group(1).strip()
        return f"<code>{lite_math(expr)}</code>"
    text = re.sub(r'<tg-math>(.*?)</tg-math>', math_inline_sub, text, flags=re.DOTALL)
    
    # 3. Convert headers to bold
    text = re.sub(r'</?h[1-6](?:\s+[^>]*)?>', lambda m: "<b>" if not m.group(0).startswith("</") else "</b>\n", text)
    
    # 4. Convert divider to standard horizontal line
    text = text.replace("<hr/>", "━━━━━━━━━━━━━━━━━━━━━━━━")
    text = text.replace("<hr>", "━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # 5. Convert lists
    # Replace <li> with bullet point, strip <ul> and </ul>
    text = re.sub(r'<li>', "  • ", text)
    text = re.sub(r'</li>', "\n", text)
    text = re.sub(r'</?u[lo](?:\s+[^>]?>)?', "", text)
    
    # 6. Convert tables to clean plain-text key-value blocks
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
    
    # 7. Strip any remaining unsupported HTML tags (safety guard)
    supported_legacy_tags = ["b", "/b", "i", "/i", "u", "/u", "s", "/s", "tg-spoiler", "/tg-spoiler", "code", "/code", "pre", "/pre", "a", "/a", "blockquote", "/blockquote"]
    
    def strip_unsupported(match):
        tag_full = match.group(0)
        tag_name_match = re.match(r'</?([a-zA-Z1-6-]+)', tag_full)
        if tag_name_match:
            tag_name = tag_name_match.group(1).lower()
            if tag_name in supported_legacy_tags or tag_full.startswith("<blockquote expandable"):
                return tag_full
        return ""
        
    text = re.sub(r'<[^>]*>', strip_unsupported, text)
    
    # Clean up spacing
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

async def send_rich_message_safe(bot: Bot, chat_id, html_content: str, reply_markup=None, reply_to_message_id=None, media_bytes=None, **kwargs) -> Message:
    """
    Safely delivers structured rich messages using Telegram's sendRichMessage API.
    Falls back gracefully to raw HTTP requests or legacy sendMessage as a secondary layer.
    """
    # Normalize newlines before processing
    normalized_content = html_content.replace("\r\n", "\n").replace("\r", "\n")
    has_media = media_bytes is not None

    # 1. Attempt native python-telegram-bot library helper if available
    for method_name in ["send_rich_message", "sendRichMessage"]:
        if hasattr(bot, method_name):
            try:
                method = getattr(bot, method_name)
                # Convert literal newlines to XML line breaks for rich document flow
                rich_html = normalized_content.replace("\n", "<br/>")
                media_arr = []
                if has_media:
                    media_arr.append({
                        "id": "quiz_diagram",
                        "media": "attach://quiz_diagram"
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

    # 2. Raw HTTP POST Fallback to /sendRichMessage with Multipart Form-Data
    try:
        url = f"https://api.telegram.org/bot{bot.token}/sendRichMessage"
        rich_html = normalized_content.replace("\n", "<br/>")
        
        # Structure rich message json parameter
        rich_message_dict = {
            "html": rich_html
        }
        if has_media:
            rich_message_dict["media"] = [
                {
                    "id": "quiz_diagram",
                    "media": "attach://quiz_diagram"
                }
            ]

        # Construct multipart payload
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

        # Build file payload if binary media is present
        files_payload = {}
        if has_media:
            files_payload["quiz_diagram"] = ("diagram.png", media_bytes, "image/png")

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data_payload, files=files_payload if has_media else None, timeout=30.0)
            if resp.status_code == 200:
                resp_json = resp.json()
                if resp_json.get("ok"):
                    return Message.de_json(resp_json["result"], bot)
            else:
                print(f"[RICH MSG] sendRichMessage raw HTTP returned status {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"[RICH MSG] HTTP multipart fallback connection failed: {e}", flush=True)

    # 3. Ultimate Fallback to Standard sendPhoto (if media is present) or sendMessage (if text-only)
    print(f"{Style.YELLOW}[RICH MSG] Falling back to standard HTML legacy delivery.{Style.RESET}", flush=True)
    legacy_html = convert_to_legacy_html(normalized_content)
    
    if has_media:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=media_bytes,
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

async def edit_rich_message_safe(bot: Bot, chat_id, message_id, html_content: str, reply_markup=None, **kwargs) -> Message:
    """
    Safely edits structured rich messages using Telegram's editMessageText parameter overrides.
    """
    # Normalize newlines before processing
    normalized_content = html_content.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Attempt using edit_message_text with rich_message in api_kwargs
    try:
        rich_html = normalized_content.replace("\n", "<br/>")
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="",  # Standard text parameter is empty when using rich_message
            api_kwargs={"rich_message": {"html": rich_html}},
            reply_markup=reply_markup,
            **kwargs
        )
    except Exception as e:
        print(f"{Style.YELLOW}[RICH MSG] editMessageText with api_kwargs failed: {e}. Trying raw HTTP...{Style.RESET}", flush=True)

    # 2. Raw HTTP POST Fallback to /editMessageText
    try:
        url = f"https://api.telegram.org/bot{bot.token}/editMessageText"
        rich_html = normalized_content.replace("\n", "<br/>")
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": "",
            "rich_message": {
                "html": rich_html
            }
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup

        for k, v in kwargs.items():
            payload[k] = v.to_dict() if hasattr(v, "to_dict") else v

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=30.0)
            if resp.status_code == 200:
                resp_json = resp.json()
                if resp_json.get("ok"):
                    return Message.de_json(resp_json["result"], bot)
            else:
                print(f"[RICH MSG] editMessageText raw endpoint returned status {resp.status_code}: {resp.text}", flush=True)
    except Exception as e:
        print(f"[RICH MSG] HTTP edit fallback connection failed: {e}", flush=True)

    # 3. Ultimate Fallback to Standard HTML edit_message_text
    legacy_html = convert_to_legacy_html(normalized_content)
    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=legacy_html,
        parse_mode="HTML",
        reply_markup=reply_markup,
        **kwargs
    )