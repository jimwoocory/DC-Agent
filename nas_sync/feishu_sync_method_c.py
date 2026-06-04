#!/usr/bin/env python3
"""
飞书群组文档自动同步系统（Method C）
员工在飞书群组分享文档链接 → 自动下载 → 按部门分类 → 自动摄入知识库

使用方法：
    python feishu_sync_method_c.py                    # 执行一次同步
    python feishu_sync_method_c.py --watch           # 监听模式（持续同步）
    python feishu_sync_method_c.py --dry-run         # 测试模式，不实际下载
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import requests
import yaml

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================
# 配置加载
# ============================================================
class Config:
    """从 config.yaml 和环境变量读取配置"""

    def __init__(self, config_file: str = "config.yaml"):
        self.config_file = config_file
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        if not os.path.exists(self.config_file):
            raise FileNotFoundError(f"配置文件不存在: {self.config_file}")

        with open(self.config_file, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # NAS 配置
        self.nas_config = config.get("nas", {})
        self.watch_config = config.get("watch", {})
        self.astrbot_config = config.get("astrbot", {})

        # 飞书应用配置
        self.feishu_app_id = os.getenv("FEISHU_APP_ID", "cli_a939424636799bc9")
        self.feishu_app_secret = os.getenv("FEISHU_APP_SECRET", "")

        # NAS 挂载点
        self.mount_point = self.nas_config.get("mount_point", "/Users/dianchi/nas_kb")
        self.inbox_dir = os.path.join(
            self.mount_point, self.watch_config.get("inbox_dir", "inbox")
        )

        # 监听群组配置（在 config.yaml 中设置）
        self.feishu_config = config.get("feishu", {})
        self.group_chat_id = self.feishu_config.get("group_chat_id", "")
        self.department_mapping = self.feishu_config.get("department_mapping", {})

        # 同步状态文件
        self.sync_state_file = os.path.join(
            os.path.dirname(__file__), ".feishu_method_c_state.json"
        )

        logger.info(f"配置加载完成: inbox_dir={self.inbox_dir}")

    def validate(self):
        """验证必要的配置"""
        if not self.feishu_app_id:
            raise ValueError("未设置 FEISHU_APP_ID")
        if not self.feishu_app_secret:
            raise ValueError("未设置 FEISHU_APP_SECRET")
        if not os.path.exists(self.mount_point):
            raise FileNotFoundError(f"NAS 挂载点不存在: {self.mount_point}")
        if not os.path.exists(self.inbox_dir):
            raise FileNotFoundError(f"inbox 目录不存在: {self.inbox_dir}")
        if not self.group_chat_id:
            raise ValueError("未配置 feishu.group_chat_id")


# ============================================================
# 飞书 API 客户端（扩展版本）
# ============================================================
class FeishuClient:
    """飞书 Drive + IM API 客户端"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self.token_expire_time = 0
        self._authenticate()

        # 缓存用户信息
        self.user_cache = {}

    def _authenticate(self):
        """获取访问令牌"""
        logger.info("正在认证飞书应用...")

        url = f"{self.BASE_URL}/auth/v3/app_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                raise Exception(f"认证失败: {data.get('msg')}")

            self.access_token = data["app_access_token"]
            self.token_expire_time = time.time() + data["expire"]
            logger.info("认证成功")

        except requests.exceptions.RequestException as e:
            logger.error(f"认证失败: {e}")
            raise

    def _ensure_token_valid(self):
        """确保令牌有效"""
        if time.time() >= self.token_expire_time - 60:
            self._authenticate()

    def _get_headers(self) -> dict:
        """获取请求头"""
        self._ensure_token_valid()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def list_group_messages(
        self, group_chat_id: str, page_size: int = 50, start_time: str | None = None
    ) -> list[dict]:
        """
        列出群组消息

        Args:
            group_chat_id: 群组 ID
            page_size: 每页消息数
            start_time: 起始时间（时间戳，单位毫秒）

        Returns:
            消息列表
        """
        logger.info(f"正在获取群组 {group_chat_id} 的消息...")

        messages = []
        page_token = None

        while True:
            url = f"{self.BASE_URL}/im/v1/messages"
            params = {
                "container_id_type": "chat",
                "container_id": group_chat_id,
                "page_size": page_size,
                "sort_type": "ByCreateTimeAsc",
            }

            if page_token:
                params["page_token"] = page_token
            if start_time:
                params["start_time"] = start_time

            try:
                response = requests.get(
                    url, params=params, headers=self._get_headers(), timeout=10
                )
                response.raise_for_status()
                data = response.json()

                if data.get("code") != 0:
                    logger.error(f"消息查询失败: {data.get('msg')}")
                    break

                items = data.get("data", {}).get("items", [])
                messages.extend(items)

                page_token = data.get("data", {}).get("page_token")
                if not page_token:
                    break

            except requests.exceptions.RequestException as e:
                logger.error(f"消息查询失败: {e}")
                break

        logger.info(f"找到 {len(messages)} 条消息")
        return messages

    def get_file_by_token(self, file_token: str) -> dict:
        """
        获取文件信息

        Args:
            file_token: 文件 token

        Returns:
            文件信息字典
        """
        url = f"{self.BASE_URL}/drive/v1/files/{file_token}"

        try:
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                logger.error(f"获取文件信息失败: {data.get('msg')}")
                return {}

            return data.get("data", {})

        except requests.exceptions.RequestException as e:
            logger.error(f"获取文件信息失败: {e}")
            return {}

    def download_file(self, file_token: str, save_path: str):
        """下载文件"""
        url = f"{self.BASE_URL}/drive/v1/files/{file_token}/download"

        try:
            response = requests.get(
                url, headers=self._get_headers(), timeout=30, stream=True
            )
            response.raise_for_status()

            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info(f"下载成功: {save_path}")

        except requests.exceptions.RequestException as e:
            logger.error(f"下载失败 ({file_token}): {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            raise

    def export_file(self, file_token: str, file_type: str, save_path: str):
        """导出文件"""
        url = f"{self.BASE_URL}/drive/v1/files/{file_token}/export"
        params = {"file_extension": file_type}

        try:
            response = requests.get(
                url, params=params, headers=self._get_headers(), timeout=30, stream=True
            )
            response.raise_for_status()

            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info(f"导出成功: {save_path}")

        except requests.exceptions.RequestException as e:
            logger.error(f"导出失败 ({file_token}): {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            raise

    def get_user_info(self, user_id: str) -> dict:
        """
        获取用户信息

        Args:
            user_id: 用户 ID

        Returns:
            用户信息字典
        """
        # 检查缓存
        if user_id in self.user_cache:
            return self.user_cache[user_id]

        url = f"{self.BASE_URL}/contact/v3/users/{user_id}"

        try:
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                logger.warning(f"获取用户信息失败: {user_id}")
                return {}

            user_info = data.get("data", {}).get("user", {})
            # 缓存用户信息
            self.user_cache[user_id] = user_info
            return user_info

        except requests.exceptions.RequestException as e:
            logger.error(f"获取用户信息失败 ({user_id}): {e}")
            return {}


# ============================================================
# 链接解析和文件提取
# ============================================================
class LinkParser:
    """解析飞书消息中的文档链接"""

    # 飞书链接正则表达式
    FEISHU_LINK_PATTERNS = [
        # 云空间文件: https://xxx.feishu.cn/drive/folder/abc123
        r"https://[^/]+\.feishu\.cn/drive/[a-z]+/([a-zA-Z0-9]+)",
        # Wiki 文档: https://xxx.feishu.cn/wiki/wikcn...
        r"https://[^/]+\.feishu\.cn/wiki/([a-zA-Z0-9]+)",
        # 新版文档: https://xxx.feishu.cn/docx/xxx
        r"https://[^/]+\.feishu\.cn/docx/([a-zA-Z0-9]+)",
        # 表格: https://xxx.feishu.cn/sheets/xxx
        r"https://[^/]+\.feishu\.cn/sheets/([a-zA-Z0-9]+)",
    ]

    @staticmethod
    def extract_links(text: str) -> list[str]:
        """
        从消息文本中提取飞书链接

        Args:
            text: 消息文本

        Returns:
            链接列表
        """
        links = []
        for pattern in LinkParser.FEISHU_LINK_PATTERNS:
            matches = re.findall(pattern, text)
            links.extend(matches)
        return links

    @staticmethod
    def extract_file_token_from_link(link: str) -> str | None:
        """
        从链接中提取文件 token

        Args:
            link: 飞书链接

        Returns:
            文件 token
        """
        # 尝试多种格式
        patterns = [
            r"drive/[a-z]+/([a-zA-Z0-9]+)",
            r"wiki/([a-zA-Z0-9]+)",
            r"docx/([a-zA-Z0-9]+)",
            r"sheets/([a-zA-Z0-9]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, link)
            if match:
                return match.group(1)

        return None


# ============================================================
# Method C 同步管理器
# ============================================================
class MethodCSyncManager:
    """Method C：从群组消息同步文档"""

    FILE_TYPE_MAP = {
        "file": "file",
        "docx": "markdown",
        "sheet": "csv",
        "bitable": "csv",
    }

    def __init__(
        self, config: Config, feishu_client: FeishuClient, dry_run: bool = False
    ):
        self.config = config
        self.feishu = feishu_client
        self.dry_run = dry_run
        self.sync_state = self._load_sync_state()

    def _load_sync_state(self) -> dict:
        """加载同步状态"""
        if os.path.exists(self.config.sync_state_file):
            try:
                with open(self.config.sync_state_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取同步状态失败: {e}")
        return {"processed_messages": {}}

    def _save_sync_state(self):
        """保存同步状态"""
        try:
            with open(self.config.sync_state_file, "w") as f:
                json.dump(self.sync_state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存同步状态失败: {e}")

    def _is_message_processed(self, message_id: str) -> bool:
        """检查消息是否已处理"""
        return message_id in self.sync_state.get("processed_messages", {})

    def _mark_message_processed(self, message_id: str, file_tokens: list[str]):
        """标记消息为已处理"""
        if "processed_messages" not in self.sync_state:
            self.sync_state["processed_messages"] = {}
        self.sync_state["processed_messages"][message_id] = {
            "timestamp": datetime.now().isoformat(),
            "file_tokens": file_tokens,
        }

    def _get_department_from_user_id(self, user_id: str) -> str:
        """
        根据用户 ID 获取部门

        Args:
            user_id: 用户 ID

        Returns:
            部门名称
        """
        # 从配置中的 department_mapping 查找
        # 结构: { "user_id": "department_name" }
        return self.config.department_mapping.get(user_id, "其他")

    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名"""
        # 移除不合法的字符
        invalid_chars = r'[<>:"|?*\x00-\x1f]'
        return re.sub(invalid_chars, "_", filename)

    def sync_once(self):
        """执行一次同步"""
        logger.info("=" * 60)
        logger.info("开始从群组消息同步文档...")
        logger.info("=" * 60)

        try:
            # 获取群组消息
            messages = self.feishu.list_group_messages(self.config.group_chat_id)

            synced_count = 0
            failed_count = 0
            skipped_count = 0

            for message in messages:
                try:
                    # 跳过已处理的消息
                    message_id = message.get("message_id")
                    if self._is_message_processed(message_id):
                        logger.debug(f"消息已处理，跳过: {message_id}")
                        skipped_count += 1
                        continue

                    # 处理消息
                    if self._process_message(message):
                        synced_count += 1
                        self._mark_message_processed(message_id, [])
                    else:
                        skipped_count += 1

                except Exception:
                    logger.error(f"处理消息失败: {message.get('message_id')}")
                    failed_count += 1

            self._save_sync_state()

            logger.info("=" * 60)
            logger.info(
                f"同步完成: 成功 {synced_count}, 失败 {failed_count}, 跳过 {skipped_count}"
            )
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"同步失败: {e}")

    def _process_message(self, message: dict) -> bool:
        """
        处理单条消息，提取链接和文件

        Args:
            message: 消息字典

        Returns:
            是否处理成功
        """
        message_id = message.get("message_id")
        sender_id = message.get("sender", {}).get("id", "")
        create_time = message.get("create_time", "")

        # 提取消息内容中的链接
        content = message.get("body", {}).get("content", "")

        # 解析链接
        file_tokens = LinkParser.extract_links(content)
        if not file_tokens:
            logger.debug(f"消息中没有找到文件链接: {message_id}")
            return False

        logger.info(f"在消息 {message_id} 中找到 {len(file_tokens)} 个文件链接")

        # 获取发送者信息和部门
        user_info = self.feishu.get_user_info(sender_id)
        user_name = user_info.get("name", "Unknown")
        department = self._get_department_from_user_id(sender_id)

        # 处理每个文件链接
        downloaded_files = 0
        for file_token in file_tokens:
            try:
                if self._download_file_by_token(
                    file_token, user_name, department, create_time
                ):
                    downloaded_files += 1
            except Exception as e:
                logger.error(f"下载文件失败 ({file_token}): {e}")

        if downloaded_files > 0:
            self._mark_message_processed(message_id, file_tokens)
            return True

        return False

    def _download_file_by_token(
        self, file_token: str, sender_name: str, department: str, create_time: str
    ) -> bool:
        """
        通过文件 token 下载文件

        Args:
            file_token: 文件 token
            sender_name: 发送者名称
            department: 部门名称
            create_time: 创建时间

        Returns:
            是否下载成功
        """
        # 获取文件信息
        file_info = self.feishu.get_file_by_token(file_token)
        if not file_info:
            logger.error(f"无法获取文件信息: {file_token}")
            return False

        file_name = file_info.get("name", "unknown")
        file_type = file_info.get("type", "file")

        logger.info(f"正在同步: {file_name} (类型: {file_type}, 部门: {department})")

        # 生成保存路径：inbox/部门/filename_sendername_date.ext
        date_str = datetime.fromtimestamp(int(create_time) / 1000).strftime("%Y%m%d")
        base_name, ext = os.path.splitext(file_name)
        safe_sender = self._sanitize_filename(sender_name)

        # 清理文件名
        safe_base = self._sanitize_filename(base_name)
        new_filename = f"{safe_base}_{safe_sender}_{date_str}{ext}"

        # 创建部门目录
        dept_dir = os.path.join(self.config.inbox_dir, department)
        save_path = os.path.join(dept_dir, new_filename)

        # 避免重复
        if os.path.exists(save_path):
            counter = 1
            base, ext = os.path.splitext(new_filename)
            while os.path.exists(save_path):
                save_path = os.path.join(dept_dir, f"{base}_{counter}{ext}")
                counter += 1

        # 下载或导出文件
        if self.dry_run:
            logger.info(f"[测试模式] 将下载到: {save_path}")
            return True

        try:
            # 根据文件类型选择下载方式
            if file_type in ["docx", "sheet", "bitable"]:
                export_type = self.FILE_TYPE_MAP.get(file_type, "pdf")
                _, ext = os.path.splitext(save_path)
                export_path = save_path.replace(ext, f".{export_type}")
                self.feishu.export_file(file_token, export_type, export_path)
                save_path = export_path
            else:
                self.feishu.download_file(file_token, save_path)

            logger.info(f"✓ 同步成功: {new_filename}")
            return True

        except Exception as e:
            logger.error(f"✗ 同步失败: {file_name} - {e}")
            return False


# ============================================================
# 主程序
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="飞书群组文档自动同步脚本 (Method C)")
    parser.add_argument("--dry-run", action="store_true", help="测试模式，不实际下载")
    parser.add_argument("--watch", action="store_true", help="监听模式，持续同步")
    parser.add_argument("--interval", type=int, default=300, help="监听间隔（秒）")

    args = parser.parse_args()

    try:
        # 加载配置
        config = Config()
        config.validate()

        # 检查必要配置
        if not config.feishu_app_secret:
            logger.error("未设置 FEISHU_APP_SECRET 环境变量")
            logger.error("请运行: export FEISHU_APP_SECRET='your_feishu_app_secret'")
            return False

        # 创建飞书客户端
        feishu = FeishuClient(config.feishu_app_id, config.feishu_app_secret)

        # 创建同步管理器
        sync_mgr = MethodCSyncManager(config, feishu, dry_run=args.dry_run)

        # 执行同步
        if args.watch:
            logger.info(f"进入监听模式，同步间隔: {args.interval} 秒")
            try:
                while True:
                    sync_mgr.sync_once()
                    logger.info(f"等待 {args.interval} 秒后进行下一次同步...")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                logger.info("收到退出信号，停止同步")
        else:
            sync_mgr.sync_once()

        return True

    except Exception as e:
        logger.error(f"程序错误: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
