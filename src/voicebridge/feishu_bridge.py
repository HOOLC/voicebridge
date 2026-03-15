from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Callable

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody
    from lark_oapi.core.enum import LogLevel
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws.client import Client as FeishuWsClient
except ModuleNotFoundError:  # pragma: no cover - depends on local optional dependency
    lark = None
    CreateMessageRequest = None
    CreateMessageRequestBody = None
    EventDispatcherHandler = None
    FeishuWsClient = None

    class LogLevel:
        INFO: Any = None

from .config import BridgeConfig
from .interactions import FeishuMessage


class FeishuBridge:
    def __init__(self, config: BridgeConfig, on_text: Callable[[str], None], log: Callable[[str, str], None]):
        self.config = config
        self._on_text = on_text
        self._log = log
        self._thread: threading.Thread | None = None
        self._client: lark.Client | None = None
        self._ws_client: FeishuWsClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.feishu_enabled
            and self.config.feishu_app_id
            and self.config.feishu_app_secret
            and self.config.feishu_user_id
        )

    def start(self) -> None:
        if not self.enabled:
            return
        if lark is None or EventDispatcherHandler is None or FeishuWsClient is None:
            raise RuntimeError("缺少 lark_oapi 依赖，无法启用飞书链路")
        if self._thread is not None and self._thread.is_alive():
            return

        self._client = (
            lark.Client.builder()
            .app_id(str(self.config.feishu_app_id))
            .app_secret(str(self.config.feishu_app_secret))
            .log_level(LogLevel.INFO)
            .build()
        )
        dispatcher = (
            EventDispatcherHandler.builder("", "", LogLevel.INFO)
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )
        self._ws_client = FeishuWsClient(
            str(self.config.feishu_app_id),
            str(self.config.feishu_app_secret),
            event_handler=dispatcher,
            log_level=LogLevel.INFO,
        )
        self._thread = threading.Thread(target=self._run_ws_client, daemon=True)
        self._thread.start()
        self._log("飞书", "长连接已启动")

    def close(self) -> None:
        ws_client = self._ws_client
        thread = self._thread
        self._ws_client = None
        self._client = None
        self._thread = None
        if ws_client is not None and hasattr(ws_client, "stop"):
            try:
                ws_client.stop()
            except Exception:  # noqa: BLE001
                pass
        if thread and thread.is_alive():
            thread.join(timeout=1.0)

    def send(self, message: FeishuMessage) -> None:
        if not self.enabled or self._client is None:
            return
        if CreateMessageRequest is None or CreateMessageRequestBody is None:
            raise RuntimeError("缺少 lark_oapi 依赖，无法发送飞书消息")

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(self.config.feishu_user_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(str(self.config.feishu_user_id))
                .msg_type(message.msg_type)
                .content(message.serialized_content())
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if response.success():
            return
        log_id = response.get_log_id() or ""
        raise RuntimeError(f"飞书发消息失败：code={response.code}, msg={response.msg}, log_id={log_id}")

    def _run_ws_client(self) -> None:
        try:
            if self._ws_client is not None:
                self._ws_client.start()
        except Exception as error:  # noqa: BLE001
            self._log("飞书", f"长连接退出：{error}")

    def _handle_message_event(self, event: object) -> None:
        data = getattr(event, "event", None)
        if data is None:
            return

        sender = getattr(data, "sender", None)
        message = getattr(data, "message", None)
        if sender is None or message is None:
            return
        if str(getattr(sender, "sender_type", "")).strip().lower() != "user":
            return
        if str(getattr(message, "chat_type", "")).strip().lower() != "p2p":
            return
        if str(getattr(message, "message_type", "")).strip().lower() != "text":
            return
        if not self._sender_matches(sender):
            return

        text = _extract_text_message(str(getattr(message, "content", "") or ""))
        if not text:
            return

        self._log("飞书", f"收到消息原文：{json.dumps(text, ensure_ascii=False)}")
        self._on_text(text)

    def _sender_matches(self, sender: object) -> bool:
        sender_id = getattr(sender, "sender_id", None)
        if sender_id is None:
            return False

        target_value = str(self.config.feishu_user_id or "").strip()
        id_type = str(self.config.feishu_user_id_type).strip().lower()
        if not target_value:
            return False

        mapping = {
            "user_id": str(getattr(sender_id, "user_id", "") or "").strip(),
            "open_id": str(getattr(sender_id, "open_id", "") or "").strip(),
            "union_id": str(getattr(sender_id, "union_id", "") or "").strip(),
        }
        return mapping.get(id_type, "") == target_value


def _extract_text_message(raw_content: str) -> str:
    if not raw_content.strip():
        return ""
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content.strip()
    if not isinstance(payload, dict):
        return raw_content.strip()
    return str(payload.get("text") or "").strip()
