"""Windows 系统通知

用 ``windows-toasts``（WinRT 的 Python 封装）弹通知，并把 ``launch`` 设成
协议激活地址——**点击通知会直接用默认浏览器打开那个地址**，后端不用守着
等回调。

非 Windows 平台（或没装 ``windows-toasts``）上本模块会安静地降级：
返回 ``False``，调度器按「推送失败」处理并退避。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.review.constants import (
    CONTEXT_MESSAGE_MAX_LENGTH,
    DEFAULT_TOAST_AUMID,
    EXAMPLE_MAX_LENGTH,
    NOTIFY_FALLBACK_BODY,
    TOAST_ATTRIBUTION,
)

log = logging.getLogger("app.review.notifier")

#: 通知里显示的 AUMID。默认借用系统自带的 Windows PowerShell，保证任何
#: Windows 机器上都能弹出来；想换成自己的应用名就设环境变量
#: ``MDX_REVIEW_TOAST_AUMID``。
AUMID = os.environ.get("MDX_REVIEW_TOAST_AUMID", DEFAULT_TOAST_AUMID)


def _load_toaster():
    """构造一个 toaster；依赖缺失或平台不支持时返回 ``None``。"""
    try:
        from windows_toasts import Toast, ToastDuration, WindowsToaster
    except Exception as exc:  # noqa: BLE001 —— 缺依赖/非 Windows 都要降级
        log.warning("Windows 通知不可用（缺少 windows-toasts？）：%s", exc)
        return None

    try:
        return WindowsToaster(AUMID), Toast, ToastDuration
    except Exception as exc:  # noqa: BLE001
        log.warning("Windows 通知不可用（无法创建 toaster）：%s", exc)
        return None


def is_available() -> bool:
    """当前环境能不能弹 Windows 通知。"""
    return _load_toaster() is not None


def build_context_message(translation: str) -> str:
    """通知副标题：翻译的第一行，超长截断。"""
    line = (translation or "").split("\n")[0].strip()
    if len(line) <= CONTEXT_MESSAGE_MAX_LENGTH:
        return line
    return f"{line[: CONTEXT_MESSAGE_MAX_LENGTH - 1]}…"


def build_body(sentence: str) -> str:
    """通知正文：例句，超长截断。"""
    text = (sentence or "").strip()
    if not text:
        return NOTIFY_FALLBACK_BODY
    if len(text) <= EXAMPLE_MAX_LENGTH:
        return text
    return f"{text[: EXAMPLE_MAX_LENGTH - 1]}…"


def send_notification(
    title: str,
    body: str,
    launch_url: Optional[str] = None,
    attribution: str = TOAST_ATTRIBUTION,
) -> bool:
    """弹一条 Windows 通知，返回是否成功。

    ``launch_url`` 会写进 toast 的 ``launch`` 属性并标记为协议激活，
    点击通知即由系统用默认浏览器打开该地址。
    """
    loaded = _load_toaster()
    if loaded is None:
        return False

    toaster, Toast, ToastDuration = loaded

    try:
        toast = Toast(
            text_fields=[title, body, attribution],
            duration=ToastDuration.Long,
            launch_action=launch_url or None,
        )
        toaster.show_toast(toast)
    except Exception as exc:  # noqa: BLE001
        log.warning("发送 Windows 通知失败：%s", exc)
        return False

    log.info("已推送 Windows 通知：%s → %s", title, launch_url)
    return True


def send_review_notification(
    word: str,
    translation: str,
    sentence: str,
    launch_url: Optional[str],
) -> bool:
    """推送一条复习提醒。

    通知刻意只当门铃：标题就是生词本身——「咦，这个词……？」才是复习的触发点，
    Windows 折叠后的气泡也只显示一行。正文放例句、副标题放翻译，展开时信息才够用。
    """
    return send_notification(
        title=word,
        body=build_body(sentence),
        launch_url=launch_url,
    )


def send_test_notification(launch_url: Optional[str] = None) -> bool:
    """测试通知：确认权限、系统通知中心、协议激活都正常。"""
    return send_notification(
        title="mdx 复习提醒",
        body="这是一条测试通知，点击应打开复习页面。",
        launch_url=launch_url,
    )
