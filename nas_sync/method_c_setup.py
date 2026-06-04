#!/usr/bin/env python3
"""
Method C 快速配置助手
帮助获取群组 ID、用户 ID 等配置信息
"""

import os
import sys

import requests
import yaml


# 颜色输出
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    END = "\033[0m"
    BOLD = "\033[1m"


def print_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")


def print_info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.END}")


def print_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


def print_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.END}")


def print_header(msg):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}")
    print(f"{Colors.BOLD}{msg}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}\n")


class FeishuHelper:
    """Feishu API 辅助类"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self._authenticate()

    def _authenticate(self):
        """认证"""
        url = f"{self.BASE_URL}/auth/v3/app_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}

        try:
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()
            if data.get("code") != 0:
                print_error(f"认证失败: {data.get('msg')}")
                return False
            self.access_token = data.get("app_access_token")
            print_success("认证成功")
            return True
        except Exception as e:
            print_error(f"认证异常: {e}")
            return False

    def _get_headers(self) -> dict:
        """获取请求头"""
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def list_chats(self) -> list:
        """列出所有群组"""
        print_info("正在获取群组列表...")
        url = f"{self.BASE_URL}/im/v1/chats"
        params = {"page_size": 50}

        chats = []
        page_token = None

        while True:
            if page_token:
                params["page_token"] = page_token

            try:
                response = requests.get(
                    url, params=params, headers=self._get_headers(), timeout=10
                )
                data = response.json()

                if data.get("code") != 0:
                    print_error(f"获取群组失败: {data.get('msg')}")
                    break

                items = data.get("data", {}).get("items", [])
                chats.extend(items)

                page_token = data.get("data", {}).get("page_token")
                if not page_token:
                    break

            except Exception as e:
                print_error(f"获取群组异常: {e}")
                break

        return chats

    def list_chat_members(self, chat_id: str) -> list:
        """列出群组成员"""
        print_info(f"正在获取群组 {chat_id} 的成员...")
        url = f"{self.BASE_URL}/im/v1/chats/{chat_id}/members"
        params = {"page_size": 100}

        members = []
        page_token = None

        while True:
            if page_token:
                params["page_token"] = page_token

            try:
                response = requests.get(
                    url, params=params, headers=self._get_headers(), timeout=10
                )
                data = response.json()

                if data.get("code") != 0:
                    print_error(f"获取成员失败: {data.get('msg')}")
                    break

                items = data.get("data", {}).get("items", [])
                members.extend(items)

                page_token = data.get("data", {}).get("page_token")
                if not page_token:
                    break

            except Exception as e:
                print_error(f"获取成员异常: {e}")
                break

        return members

    def get_user_info(self, user_id: str) -> dict:
        """获取用户信息"""
        url = f"{self.BASE_URL}/contact/v3/users/{user_id}"

        try:
            response = requests.get(url, headers=self._get_headers(), timeout=10)
            data = response.json()

            if data.get("code") != 0:
                return {}

            return data.get("data", {}).get("user", {})

        except Exception as e:
            print_error(f"获取用户信息异常: {e}")
            return {}


def main():
    """主程序"""
    print_header("Method C 快速配置助手")

    # 1. 检查 App Secret
    print_info("Step 1: 检查 Feishu App Secret")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_secret:
        print_error("未设置 FEISHU_APP_SECRET 环境变量")
        print("请运行: export FEISHU_APP_SECRET='your_feishu_app_secret'")
        return False

    # 2. 检查 config.yaml
    if not os.path.exists("config.yaml"):
        print_error("config.yaml 不存在")
        return False

    # 3. 创建 Feishu 客户端
    print_info("Step 2: 连接 Feishu API")
    feishu = FeishuHelper("cli_a939424636799bc9", app_secret)
    if not feishu.access_token:
        return False

    # 4. 获取群组列表
    print_info("Step 3: 获取群组列表")
    chats = feishu.list_chats()
    if not chats:
        print_error("没有找到群组")
        return False

    print_success(f"找到 {len(chats)} 个群组")
    print("\n可用的群组:")
    for i, chat in enumerate(chats, 1):
        chat_id = chat.get("chat_id", "N/A")
        chat_name = chat.get("name", "N/A")
        print(f"  {i}. [{chat_id}] {chat_name}")

    # 5. 选择群组
    print()
    choice = input(f"请输入要监听的群组编号 (1-{len(chats)}): ")
    try:
        chat_idx = int(choice) - 1
        if chat_idx < 0 or chat_idx >= len(chats):
            print_error("编号无效")
            return False
        selected_chat = chats[chat_idx]
    except ValueError:
        print_error("请输入有效的编号")
        return False

    group_chat_id = selected_chat.get("chat_id")
    group_chat_name = selected_chat.get("name")
    print_success(f"选择群组: [{group_chat_id}] {group_chat_name}")

    # 6. 获取群组成员
    print_info("Step 4: 获取群组成员")
    members = feishu.list_chat_members(group_chat_id)
    if not members:
        print_warning("没有找到群组成员或成员列表为空")
    else:
        print_success(f"找到 {len(members)} 个成员")

        # 获取成员详细信息
        print("\n群组成员及其 ID:")
        print(f"{'序号':<4} {'姓名':<15} {'用户ID':<25} {'部门':<20}")
        print("-" * 65)

        user_mapping = {}
        for i, member in enumerate(members, 1):
            user_id = member.get("member_id", "N/A")
            # 获取用户详细信息
            user_info = feishu.get_user_info(user_id)
            user_name = user_info.get("name", "N/A")
            departments = user_info.get("departments", [])
            dept_name = (
                departments[0].get("name", "未知部门") if departments else "未知部门"
            )

            print(f"{i:<4} {user_name:<15} {user_id:<25} {dept_name:<20}")

            user_mapping[user_id] = {"name": user_name, "department": dept_name}

        # 7. 生成配置
        print_header("生成配置")

        config_text = f"""
# Feishu Method C 配置（自动生成）
# 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
# 群组: {group_chat_name}

feishu:
  group_chat_id: "{group_chat_id}"

  department_mapping:
"""

        # 按部门分组
        dept_users = {}
        for user_id, info in user_mapping.items():
            dept = info["department"]
            if dept not in dept_users:
                dept_users[dept] = []
            dept_users[dept].append((user_id, info["name"]))

        # 生成映射
        for dept in sorted(dept_users.keys()):
            config_text += f"    # {dept}\n"
            for user_id, name in sorted(dept_users[dept]):
                config_text += f'    "{user_id}": "{dept}"  # {name}\n'
            config_text += "\n"

        print(config_text)

        # 8. 保存配置
        print_header("保存配置")
        save_choice = input("是否保存配置到 config.yaml? (y/n): ")
        if save_choice.lower() == "y":
            try:
                # 读取现有配置
                with open("config.yaml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)

                # 添加 feishu 配置
                if "feishu" not in config:
                    config["feishu"] = {}

                config["feishu"]["group_chat_id"] = group_chat_id
                config["feishu"]["department_mapping"] = {}

                # 添加部门映射
                for dept in sorted(dept_users.keys()):
                    for user_id, name in sorted(dept_users[dept]):
                        config["feishu"]["department_mapping"][user_id] = dept

                # 保存配置
                with open("config.yaml", "w", encoding="utf-8") as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

                print_success("配置已保存到 config.yaml")
                return True

            except Exception as e:
                print_error(f"保存配置失败: {e}")
                return False
        else:
            print_info("配置未保存，请手动复制上述配置到 config.yaml")
            return True


if __name__ == "__main__":
    from datetime import datetime

    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print_error(f"程序异常: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
