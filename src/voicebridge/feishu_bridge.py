from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Callable

try:
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_module
    from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
    from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody
    from lark_oapi.core.enum import LogLevel
    from lark_oapi.core.log import logger as lark_logger
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws.client import Client as FeishuWsClient
    from lark_oapi.ws.exception import ClientException
except ModuleNotFoundError:  # pragma: no cover - depends on local optional dependency
    lark = None
    lark_ws_module = None
    CreateMessageRequest = None
    CreateMessageRequestBody = None
    EventDispatcherHandler = None
    FeishuWsClient = None
    lark_logger = None

    class LogLevel:
        INFO: Any = None

    class ClientException(Exception):
        pass

from .config import BridgeConfig
from .interactions import FeishuMessage


class _LarkReconnectNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "receive message loop exit, err: no close frame received or sent" in message:
            record.levelno = logging.INFO
            record.levelname = logging.getLevelName(logging.INFO)
        return True


def _install_lark_log_filter() -> None:
    if lark_logger is None:
        return
    for existing in lark_logger.filters:
        if isinstance(existing, _LarkReconnectNoiseFilter):
            return
    lark_logger.addFilter(_LarkReconnectNoiseFilter())


class FeishuBridge:
    _SUPERVISOR_RESTART_DELAY_SECONDS = 3.0
    _SHUTDOWN_TIMEOUT_SECONDS = 2.0

    def __init__(self, config: BridgeConfig, on_text: Callable[[str], None], log: Callable[[str, str], None]):
        self.config = config
        self._on_text = on_text
        self._log = log
        self._thread: threading.Thread | None = None
        self._client: lark.Client | None = None
        self._ws_client: FeishuWsClient | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()

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
        _install_lark_log_filter()

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._client = self._build_api_client()
            self._thread = threading.Thread(target=self._run_ws_supervisor, daemon=True, name="VoiceBridgeFeishuWs")
            self._thread.start()
        self._log("飞书", "长连接已启动")

    def close(self) -> None:
        with self._state_lock:
            thread = self._thread
            self._thread = None
            self._client = None
            self._stop_event.set()
        self._request_ws_shutdown()
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                self._log("飞书", "长连接线程未在 5 秒内退出，将等待进程结束时回收")

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

    def _build_api_client(self) -> lark.Client:
        return (
            lark.Client.builder()
            .app_id(str(self.config.feishu_app_id))
            .app_secret(str(self.config.feishu_app_secret))
            .log_level(LogLevel.INFO)
            .build()
        )

    def _build_dispatcher(self) -> EventDispatcherHandler:
        return (
            EventDispatcherHandler.builder("", "", LogLevel.INFO)
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )

    def _build_ws_client(self) -> FeishuWsClient:
        return FeishuWsClient(
            str(self.config.feishu_app_id),
            str(self.config.feishu_app_secret),
            event_handler=self._build_dispatcher(),
            log_level=LogLevel.INFO,
        )

    def _run_ws_supervisor(self) -> None:
        while not self._stop_event.is_set():
            loop = asyncio.new_event_loop()
            previous_loop = getattr(lark_ws_module, "loop", None) if lark_ws_module is not None else None
            ws_client: FeishuWsClient | None = None
            try:
                # The SDK's public start() blocks forever and exposes no stop(); run its async internals
                # on our own loop so close() can terminate cleanly and the supervisor can rebuild it.
                asyncio.set_event_loop(loop)
                if lark_ws_module is not None:
                    lark_ws_module.loop = loop
                ws_client = self._build_ws_client()
                with self._state_lock:
                    if self._stop_event.is_set():
                        return
                    self._ws_client = ws_client
                    self._ws_loop = loop
                loop.run_until_complete(self._open_ws_client(loop, ws_client))
                loop.run_forever()
                if self._stop_event.is_set():
                    return
                self._log("飞书", "长连接已停止，准备重建")
            except Exception as error:  # noqa: BLE001
                if self._stop_event.is_set():
                    return
                self._log(
                    "飞书",
                    f"长连接退出：{error}；{int(self._SUPERVISOR_RESTART_DELAY_SECONDS)}秒后重建",
                )
            finally:
                self._dispose_ws_runtime(loop, ws_client, previous_loop)
            if self._stop_event.wait(self._SUPERVISOR_RESTART_DELAY_SECONDS):
                return

    async def _open_ws_client(self, loop: asyncio.AbstractEventLoop, ws_client: FeishuWsClient) -> None:
        try:
            await ws_client._connect()
        except ClientException:
            raise
        except Exception:
            await ws_client._disconnect()
            if getattr(ws_client, "_auto_reconnect", True):
                await ws_client._reconnect()
            else:
                raise
        loop.create_task(ws_client._ping_loop())

    def _request_ws_shutdown(self) -> None:
        with self._state_lock:
            loop = self._ws_loop
            ws_client = self._ws_client
        if loop is None or loop.is_closed():
            return

        if ws_client is not None and loop.is_running():
            disconnect = getattr(ws_client, "_disconnect", None)
            if callable(disconnect):
                try:
                    future = asyncio.run_coroutine_threadsafe(disconnect(), loop)
                    future.result(timeout=self._SHUTDOWN_TIMEOUT_SECONDS)
                except Exception:  # noqa: BLE001
                    pass
        try:
            loop.call_soon_threadsafe(self._cancel_loop_tasks, loop)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass

    def _dispose_ws_runtime(
        self,
        loop: asyncio.AbstractEventLoop,
        ws_client: FeishuWsClient | None,
        previous_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        with self._state_lock:
            if self._ws_client is ws_client:
                self._ws_client = None
            if self._ws_loop is loop:
                self._ws_loop = None

        try:
            if not loop.is_closed():
                if ws_client is not None:
                    disconnect = getattr(ws_client, "_disconnect", None)
                    if callable(disconnect):
                        try:
                            loop.run_until_complete(disconnect())
                        except Exception:  # noqa: BLE001
                            pass
                self._drain_loop(loop)
        finally:
            if lark_ws_module is not None and getattr(lark_ws_module, "loop", None) is loop and previous_loop is not None:
                lark_ws_module.loop = previous_loop
            asyncio.set_event_loop(None)
            loop.close()

    @staticmethod
    def _cancel_loop_tasks(loop: asyncio.AbstractEventLoop) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    @staticmethod
    def _drain_loop(loop: asyncio.AbstractEventLoop) -> None:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())

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
