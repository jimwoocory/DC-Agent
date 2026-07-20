"""Admin asset plugin for Feishu business MVP inventory workflows."""

from __future__ import annotations

from pathlib import Path

from dc_engines.feishu_business_mvp import AssetMovement, BusinessMvpStore

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult


@register(
    "admin_asset_plugin",
    "dc_agent",
    "综合行政：办公用品库存、申领、归还和低库存预警",
    "0.1.0",
)
class AdminAssetPlugin(Star):
    """Expose admin asset commands and Web APIs."""

    def __init__(self, context: Context, config=None) -> None:
        """Initialize plugin state.

        Args:
            context: AstrBot plugin context.
            config: Optional plugin configuration.
        """

        super().__init__(context)
        cfg = config or {}
        project_root = Path(__file__).resolve().parents[3]
        self.enabled = bool(cfg.get("enabled", True))
        self.db_path = Path(
            cfg.get("db_path") or project_root / "data" / "feishu_business_mvp.db"
        )
        self.store = BusinessMvpStore(self.db_path)

    async def initialize(self) -> None:
        """Initialize storage and register Web APIs.

        Returns:
            None.
        """

        await self.store.initialize()
        self.context.feishu_business_asset_store = self.store
        try:
            self.context.register_web_api(
                "/feishu_business/admin/assets",
                self._api_assets,
                ["GET"],
                "综合行政库存、低库存和流水入口",
            )
            self.context.register_web_api(
                "/feishu_business/admin/assets/movement",
                self._api_asset_movement,
                ["POST"],
                "综合行政办公用品申领/归还/入库流水",
            )
            logger.info("[admin_asset] API ready under /api/plug/admin_asset_plugin")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[admin_asset] register API failed: %s", exc)

    @filter.command("asset-list", desc="综合行政库存列表")
    async def asset_list_command(self, event: AstrMessageEvent) -> None:
        """Reply with a compact asset inventory list.

        Args:
            event: Message event.
        """

        if not self.enabled:
            self._reply(event, "综合行政库存插件当前未启用。")
            return
        items = await self.store.list_asset_items(limit=20)
        if not items:
            self._reply(event, "当前还没有办公用品库存记录。")
            return
        lines = ["办公用品库存："]
        for item in items:
            marker = "低库存" if item.low_stock else "正常"
            lines.append(f"- {item.item_id} {item.name}: {item.stock}（{marker}）")
        self._reply(event, "\n".join(lines))

    @filter.command("asset-low", desc="综合行政低库存预警")
    async def asset_low_command(self, event: AstrMessageEvent) -> None:
        """Reply with low-stock asset items.

        Args:
            event: Message event.
        """

        items = await self.store.list_asset_items(low_stock_only=True, limit=20)
        if not items:
            self._reply(event, "当前没有低库存办公用品。")
            return
        lines = ["低库存办公用品："]
        for item in items:
            lines.append(f"- {item.name}: {item.stock}/{item.warning_threshold}")
        self._reply(event, "\n".join(lines))

    @filter.command(
        "asset-claim", desc="申领办公用品：/asset-claim <item_id> [quantity]"
    )
    async def asset_claim_command(self, event: AstrMessageEvent) -> None:
        """Apply an asset claim movement from chat.

        Args:
            event: Message event.
        """

        item_id, quantity = self._parse_item_quantity(
            event.message_str or "", "/asset-claim"
        )
        if not item_id:
            self._reply(event, "用法：/asset-claim <item_id> [quantity]")
            return
        try:
            updated = await self.store.apply_asset_movement(
                AssetMovement(
                    movement_id="",
                    item_id=item_id,
                    movement_type="claim",
                    quantity=quantity,
                    actor_id=str(getattr(event, "get_sender_id", lambda: "")() or ""),
                    note="chat command claim",
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"申领失败：{exc}")
            return
        self._reply(event, f"已记录申领：{updated.name}，剩余库存 {updated.stock}。")

    @filter.command(
        "asset-return", desc="归还办公用品：/asset-return <item_id> [quantity]"
    )
    async def asset_return_command(self, event: AstrMessageEvent) -> None:
        """Apply an asset return movement from chat.

        Args:
            event: Message event.
        """

        item_id, quantity = self._parse_item_quantity(
            event.message_str or "", "/asset-return"
        )
        if not item_id:
            self._reply(event, "用法：/asset-return <item_id> [quantity]")
            return
        try:
            updated = await self.store.apply_asset_movement(
                AssetMovement(
                    movement_id="",
                    item_id=item_id,
                    movement_type="return",
                    quantity=quantity,
                    actor_id=str(getattr(event, "get_sender_id", lambda: "")() or ""),
                    note="chat command return",
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._reply(event, f"归还失败：{exc}")
            return
        self._reply(event, f"已记录归还：{updated.name}，当前库存 {updated.stock}。")

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=70,
    )
    async def on_asset_question(self, event: AstrMessageEvent) -> None:
        """Handle natural-language office supply questions.

        Args:
            event: Message event.
        """

        if not self.enabled:
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        if not self._should_handle_asset_question(text):
            return
        items = await self.store.list_asset_items(limit=50)
        if not items:
            self._reply(
                event,
                "办公用品领用：说明用途后由综合行政确认；当前本地库存台账还没有记录。",
            )
            return
        matched = [item for item in items if item.name and item.name in text]
        visible_items = (
            matched or [item for item in items if item.status == "active"][:5]
        )
        lines = ["办公用品领用：说明用途后由综合行政确认，库存足够即可登记领取。"]
        for item in visible_items:
            state = "可申领" if item.stock > 0 else "暂无库存"
            lines.append(
                f"- {item.name}: 库存 {item.stock}，{state}，编号 {item.item_id}"
            )
        if matched and matched[0].stock > 0:
            lines.append(f"可登记申领：/asset-claim {matched[0].item_id} 1")
        self._reply(event, "\n".join(lines))

    async def _api_assets(self, *args, **kwargs):
        """Return asset dashboard data.

        Returns:
            JSON-friendly asset payload.
        """

        items = await self.store.list_asset_items(limit=200)
        low_stock = await self.store.list_asset_items(low_stock_only=True, limit=50)
        movements = await self.store.list_asset_movements(limit=50)
        return {
            "status": "ok",
            "message": None,
            "data": {
                "enabled": self.enabled,
                "items": [BusinessMvpStore._asset_item_to_dict(item) for item in items],
                "low_stock": [
                    BusinessMvpStore._asset_item_to_dict(item) for item in low_stock
                ],
                "movements": [
                    {
                        "movement_id": movement.movement_id,
                        "item_id": movement.item_id,
                        "movement_type": movement.movement_type,
                        "quantity": movement.quantity,
                        "actor_id": movement.actor_id,
                        "actor_name": movement.actor_name,
                        "note": movement.note,
                        "created_at": movement.created_at,
                        "metadata": movement.metadata,
                    }
                    for movement in movements
                ],
            },
        }

    async def _api_asset_movement(self, *args, **kwargs):
        """Apply an asset movement from Dashboard or automation.

        Returns:
            JSON-friendly movement result.
        """

        from astrbot.api.web import request

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return {"status": "error", "message": "invalid json body", "data": None}
        item_id = str(payload.get("item_id") or "").strip()
        movement_type = str(payload.get("movement_type") or "").strip()
        quantity = int(payload.get("quantity") or 0)
        if not item_id or not movement_type:
            return {
                "status": "error",
                "message": "item_id/movement_type required",
                "data": None,
            }
        if movement_type != "inventory_adjust" and quantity <= 0:
            return {
                "status": "error",
                "message": "quantity must be positive",
                "data": None,
            }
        if movement_type == "inventory_adjust" and quantity < 0:
            return {
                "status": "error",
                "message": "inventory quantity cannot be negative",
                "data": None,
            }
        try:
            updated = await self.store.apply_asset_movement(
                AssetMovement(
                    movement_id=str(payload.get("movement_id") or ""),
                    item_id=item_id,
                    movement_type=movement_type,
                    quantity=quantity,
                    actor_id=str(payload.get("actor_id") or ""),
                    actor_name=str(payload.get("actor_name") or ""),
                    note=str(payload.get("note") or ""),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc), "data": None}
        return {
            "status": "ok",
            "message": None,
            "data": BusinessMvpStore._asset_item_to_dict(updated),
        }

    @staticmethod
    def _parse_item_quantity(text: str, prefix: str) -> tuple[str, int]:
        """Parse item_id and quantity from a command.

        Args:
            text: Raw command text.
            prefix: Command prefix.

        Returns:
            Tuple of item_id and quantity.
        """

        args = text.replace(prefix, "", 1).strip().split()
        if not args:
            return "", 1
        quantity = 1
        if len(args) > 1:
            try:
                quantity = max(1, int(args[1]))
            except ValueError:
                quantity = 1
        return args[0], quantity

    @staticmethod
    def _should_handle_asset_question(text: str) -> bool:
        """Return whether a message is an office supply question.

        Args:
            text: User message text.

        Returns:
            True when the text asks about office supplies.
        """

        normalized = text.replace(" ", "")
        triggers = (
            "怎么领办公用品",
            "办公用品怎么领",
            "领办公用品",
            "申请办公用品",
            "办公用品库存",
            "库存还有",
            "领用办公用品",
            "申领办公用品",
        )
        return any(trigger in normalized for trigger in triggers)

    @staticmethod
    def _reply(event: AstrMessageEvent, text: str) -> None:
        """Send a plain text command reply.

        Args:
            event: Message event.
            text: Reply text.
        """

        event.set_result(MessageEventResult().message(text).use_t2i(False))
