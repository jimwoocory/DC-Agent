from __future__ import annotations

from pathlib import Path

RPA_SCRIPT = Path("scripts-tools/feishu_rpa_sender.mjs")


def test_rpa_sender_waits_for_chat_ready_before_typing() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert "async function waitForChatReady" in source
    assert "目标会话仍在加载，重新打开 messenger 后重试" in source
    assert "await waitForChatReady(page);" in source
    assert 'url.includes("/next/messenger")' in source
    assert "当前页面不是飞书消息页，新开页面进入飞书 messenger" in source

    wait_call = source.index("await waitForChatReady(page);")
    composer_click = source.index("await composer.click", wait_call)
    keyboard_type = source.index("await page.keyboard.type(textToSend", wait_call)

    assert wait_call < composer_click < keyboard_type


def test_rpa_sender_requires_right_panel_header_and_disables_keyboard_fallback() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert "async function targetNameAppearsInRightChatPanel" in source
    assert "左侧会话列表也会包含目标名，不能再用 body 文本命中作为通过条件" in source
    assert "右侧聊天面板未出现目标会话头部" in source
    assert "DOM 文本已包含目标会话名，跳过可见性误判" not in source
    assert 'console.log("[Feishu RPA] 兜底键盘发送")' not in source
    assert "await page.keyboard.type(textToSend, { delay: 30 })" not in source
    assert "未找到右侧聊天输入框，拒绝兜底键盘发送" in source
    assert "async function activateTargetConversationFromLeftList" in source
    assert "右侧头部未出现，尝试从左侧会话列表重新激活目标" in source
    assert "never count as delivery evidence by itself" in source

    header_check = source.index("await targetNameAppearsInRightChatPanel(page, TARGET_NAME)")
    list_reactivate = source.index("await activateTargetConversationFromLeftList(page, TARGET_NAME)", header_check)
    composer_lookup = source.index("await findChatComposer(page)", list_reactivate)
    no_fallback = source.index("未找到右侧聊天输入框，拒绝兜底键盘发送", composer_lookup)
    assert header_check < list_reactivate < composer_lookup < no_fallback


def test_rpa_persistent_browser_defaults_to_direct_feishu_and_proxy_is_opt_in() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert 'const RPA_PROXY_SERVER = process.env.FEISHU_RPA_PROXY || ""' in source
    assert 'const RPA_PROXY_BYPASS = "localhost,127.0.0.1,::1,.local"' in source
    assert "function sanitizeProxyEnvironmentForFeishuRpa" in source
    assert "delete process.env[key]" in source
    assert "HTTP_PROXY" in source
    assert "NO_PROXY" in source
    assert "server: RPA_PROXY_SERVER" in source
    assert "bypass: RPA_PROXY_BYPASS" in source
    assert "const FEISHU_DIRECT_HOST_RESOLVER_RULES = [" in source
    assert "MAP o0ain5w98jh.feishu.cn 139.177.246.206" in source
    assert "MAP open.feishu.cn 139.177.246.206" in source
    assert '"--no-proxy-server"' in source
    assert "--host-resolver-rules=${FEISHU_DIRECT_HOST_RESOLVER_RULES}" in source
    sanitize_call = source.index("sanitizeProxyEnvironmentForFeishuRpa();")
    launch_call = source.index("chromium.launchPersistentContext", sanitize_call)
    assert sanitize_call < launch_call
    bypass_line = source.partition("const RPA_PROXY_BYPASS = ")[2].split("\n", 1)[0]
    assert ".feishu.cn" not in bypass_line


def test_rpa_removes_blank_quickjump_larklet_modal() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert "const isLargeBlankLarklet = Boolean" in source
    assert 'node.classList?.contains("larklet-modal-container")' in source
    assert "text.length < 500" in source
    assert "rect.width > window.innerWidth * 0.45" in source
    assert "rect.height > window.innerHeight * 0.45" in source
    assert "|| isLargeBlankLarklet" in source

    blank_larklet_check = source.index("const isLargeBlankLarklet = Boolean")
    remove_call = source.index("|| isLargeBlankLarklet", blank_larklet_check)
    blocking_check = source.index("async function hasBlockingOverlay", remove_call)
    assert blank_larklet_check < remove_call < blocking_check


def test_rpa_delivery_problem_detection_ignores_generic_framework_error_classes() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert "const visibleFailureText = /发送失败|重新发送|未发送|重试|发送中/" in source
    assert "const deliveryStateAttr =" in source
    assert "send-failed|send-fail|message-failed|retry|sending" in source
    assert "failed|error/i" not in source
    assert "suspicious.test(`${nearbyText} ${attrs}`)" not in source
    assert "Feishu components often include generic" in source
    assert "Only fail on visible loading text in the right chat panel" in source
    assert "const visibleLoading = Array.from(document.querySelectorAll('body *')).some" in source
    assert "body-wide aggregate text caused false" in source
    assert "child.innerText || child.textContent || '').includes('正在加载...')" in source
    assert "child.innerText || child.textContent || '').includes(short)" in source


def test_rpa_checks_proxy_connectivity_before_launching_persistent_browser() -> None:
    source = RPA_SCRIPT.read_text(encoding="utf-8")

    assert "async function verifyRpaProxyConnectivity" in source
    assert "if (!RPA_PROXY_SERVER) return;" in source
    assert "CONNECT www.feishu.cn:443 HTTP/1.1" in source
    assert "RPA proxy CONNECT failed" in source
    assert "tls.connect" in source
    assert "RPA proxy TLS failed after CONNECT" in source
    proxy_check = source.index("await verifyRpaProxyConnectivity();")
    launch_call = source.index("chromium.launchPersistentContext", proxy_check)

    assert proxy_check < launch_call
