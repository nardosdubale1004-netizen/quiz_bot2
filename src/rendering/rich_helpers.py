# src/rendering/rich_helpers.py
import httpx
from telegram import Bot, Message
from src.config import Style

async def send_rich_message_safe(bot: Bot, chat_id, html_content: str, reply_markup=None, reply_to_message_id=None, **kwargs) -> Message:
    """
    Safely delivers structured rich messages using Telegram's sendRichMessage API.
    Falls back gracefully to raw HTTP requests or legacy sendMessage as a secondary layer.
    """
    # Normalize newlines before processing
    normalized_content = html_content.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Attempt native python-telegram-bot library helper if available
    for method_name in ["send_rich_message", "sendRichMessage"]:
        if hasattr(bot, method_name):
            try:
                method = getattr(bot, method_name)
                # Convert literal newlines to XML line breaks for rich document flow
                rich_html = normalized_content.replace("\n", "<br/>")
                return await method(
                    chat_id=chat_id,
                    rich_message={"html": rich_html},
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    **kwargs
                )
            except Exception as e:
                print(f"{Style.YELLOW}[RICH MSG] Native client call failed: {e}. Trying HTTP raw fallback...{Style.RESET}", flush=True)

    # 2. Raw HTTP POST Fallback to /sendRichMessage
    try:
        url = f"https://api.telegram.org/bot{bot.token}/sendRichMessage"
        rich_html = normalized_content.replace("\n", "<br/>")
        payload = {
            "chat_id": str(chat_id),
            "rich_message": {
                "html": rich_html
            }
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)

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
                print(f"{Style.RED}[RICH MSG] sendRichMessage endpoint returned {resp.status_code}: {resp.text}{Style.RESET}", flush=True)
    except Exception as e:
        print(f"{Style.RED}[RICH MSG] HTTP send fallback connection failed: {e}{Style.RESET}", flush=True)

    # 3. Ultimate Fallback to Standard sendMessage with HTML parse mode
    # NOTE: Standard parse_mode="HTML" does NOT support <br/>, so we retain raw newlines
    print(f"{Style.YELLOW}[RICH MSG] Falling back to standard HTML sendMessage.{Style.RESET}", flush=True)
    return await bot.send_message(
        chat_id=chat_id,
        text=normalized_content,
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
                print(f"{Style.RED}[RICH MSG] editMessageText raw endpoint returned {resp.status_code}: {resp.text}{Style.RESET}", flush=True)
    except Exception as e:
        print(f"{Style.RED}[RICH MSG] HTTP edit fallback connection failed: {e}{Style.RESET}", flush=True)

    # 3. Ultimate Fallback to Standard HTML edit_message_text
    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=normalized_content,
        parse_mode="HTML",
        reply_markup=reply_markup,
        **kwargs
    )