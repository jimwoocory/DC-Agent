"""
NAS 知识库文件监听器
====================
监听 NAS inbox 目录，自动将新文件摄入 AstrBot 知识库。

运行方式：
    python nas_sync/watcher.py           # 前台运行
    python nas_sync/watcher.py --once    # 扫描一次 inbox 后退出（适合 cron）

依赖：
    pip install watchdog pyyaml requests
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

try:
    from watchdog.events import FileSystemEventHandler
except ModuleNotFoundError:

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass


# ----------------------------------------------------------------
# 路径配置
# ----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
STATE_PATH = Path(__file__).resolve().parent / "state.json"


# ----------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_keychain_secret(service: str) -> str:
    """Read a secret from macOS Keychain by generic-password service name."""
    if not service:
        raise RuntimeError("Keychain service name is empty.")
    backup_path = Path.home() / ".config" / "nas_sync" / f"{service}.bak"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("macOS security command not found.") from exc
    except subprocess.CalledProcessError as exc:
        if backup_path.exists():
            secret = backup_path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        raise RuntimeError(
            f"Keychain secret not found: {service}. "
            "Create it with security add-generic-password before running watcher.py, "
            f"or provide a chmod 600 fallback at {backup_path}."
        ) from exc
    secret = result.stdout.strip()
    if not secret:
        raise RuntimeError(f"Keychain secret is empty: {service}")
    return secret


# ----------------------------------------------------------------
# 状态持久化（记录哪些文件已摄入，避免重复）
# ----------------------------------------------------------------
class IngestState:
    def __init__(self):
        self._path = STATE_PATH
        self._data: dict = {"ingested": {}}
        self._load()

    def _load(self):
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)

    def _save(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def is_ingested(self, rel_path: str, file_hash: str) -> bool:
        entry = self._data["ingested"].get(rel_path)
        if not entry:
            return False
        return entry.get("file_hash") == file_hash

    def mark_ingested(self, rel_path: str, file_hash: str, doc_id: str):
        self._data["ingested"][rel_path] = {
            "file_hash": file_hash,
            "doc_id": doc_id,
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save()

    def remove(self, rel_path: str):
        self._data["ingested"].pop(rel_path, None)
        self._save()


# ----------------------------------------------------------------
# AstrBot API 客户端
# ----------------------------------------------------------------
class AstrBotKBClient:
    def __init__(self, cfg: dict):
        astrbot_cfg = cfg["astrbot"]
        self._api_base = astrbot_cfg["api_base"].rstrip("/")
        self._username = astrbot_cfg["username"]
        self._password = read_keychain_secret(astrbot_cfg["password_keychain_key"])
        self._kb_id: str = astrbot_cfg.get("kb_id", "") or ""
        self._embedding_provider_id: str = (
            astrbot_cfg.get("embedding_provider_id", "") or ""
        )
        self._kb_mapping: dict = astrbot_cfg.get("kb_mapping", {})
        self._token: str = ""
        self._token_fetched_at: float = 0
        self._token_ttl: int = int(astrbot_cfg.get("token_refresh_interval", 3600))
        self._chunk_size: int = int(astrbot_cfg.get("chunk_size", 512))
        self._chunk_overlap: int = int(astrbot_cfg.get("chunk_overlap", 50))
        self._session = requests.Session()
        self._session.trust_env = False
        self.log = logging.getLogger("nas.client")

    # ---- Auth ----

    def _md5(self, s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()

    def _ensure_token(self):
        if self._token and (time.time() - self._token_fetched_at) < self._token_ttl:
            return
        self.log.info("获取 AstrBot JWT Token...")
        candidates = [self._password]
        legacy_md5 = self._md5(self._password)
        if legacy_md5 != self._password:
            candidates.append(legacy_md5)

        last_message = "未知错误"
        for candidate in candidates:
            resp = self._session.post(
                f"{self._api_base}/auth/login",
                json={"username": self._username, "password": candidate},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") in (200, "ok"):
                self._token = data["data"]["token"]
                self._token_fetched_at = time.time()
                self.log.info("Token 获取成功。")
                return
            last_message = data.get("message", last_message)

        raise RuntimeError(f"登录失败：{last_message}")

    def _headers(self) -> dict:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    def get_kb_id_for_file(self, file_path: str) -> str | None:
        """根据文件路径返回对应的知识库 ID"""
        if self._kb_mapping:
            for folder_prefix, kb_id in self._kb_mapping.items():
                if folder_prefix in file_path:
                    return kb_id
        return None

    # ---- KB 管理 ----

    def ensure_kb(self) -> str:
        """确保目标知识库存在，返回 kb_id。"""
        if self._kb_id:
            return self._kb_id

        # 查找名为 nas_knowledge 的 KB
        resp = self._session.get(
            f"{self._api_base}/kb/list",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        for kb in data.get("data", {}).get("items", []):
            if kb["kb_name"] == "nas_knowledge":
                self._kb_id = kb["kb_id"]
                self.log.info(f"使用已有知识库 nas_knowledge（{self._kb_id}）")
                return self._kb_id

        # 自动创建
        if not self._embedding_provider_id:
            raise RuntimeError(
                "知识库不存在且 config.yaml 中未配置 embedding_provider_id，"
                "请先在 AstrBot Dashboard 创建知识库，或填写 embedding_provider_id。"
            )
        self.log.info("创建新知识库 nas_knowledge ...")
        resp = self._session.post(
            f"{self._api_base}/kb/create",
            headers=self._headers(),
            json={
                "kb_name": "nas_knowledge",
                "description": "公司 NAS 知识库（自动摄入）",
                "emoji": "🗄️",
                "embedding_provider_id": self._embedding_provider_id,
                "chunk_size": self._chunk_size,
                "chunk_overlap": self._chunk_overlap,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in (200, "ok"):
            raise RuntimeError(f"创建知识库失败：{data.get('message')}")
        self._kb_id = data["data"]["kb_id"]
        self.log.info(f"知识库已创建：{self._kb_id}")
        return self._kb_id

    # ---- 文档摄入 ----

    def upload_file(self, file_path: Path) -> str:
        """将文件上传到知识库，返回 doc_id。"""
        # 先尝试从 kb_mapping 获取知识库 ID
        kb_id = self.get_kb_id_for_file(str(file_path))
        if not kb_id:
            kb_id = self.ensure_kb()
        self.log.info(f"上传文件：{file_path.name} → KB {kb_id}")

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "application/octet-stream")}
            data = {
                "kb_id": kb_id,
                "chunk_size": str(self._chunk_size),
                "chunk_overlap": str(self._chunk_overlap),
            }
            resp = self._session.post(
                f"{self._api_base}/kb/document/upload",
                headers=self._headers(),
                files=files,
                data=data,
                timeout=120,
            )

        resp.raise_for_status()
        result = resp.json()
        if result.get("status") not in (200, "ok"):
            raise RuntimeError(f"上传失败：{result.get('message')}")

        task_id = result["data"]["task_id"]
        self.log.info(f"上传任务已提交，task_id={task_id}，等待完成...")
        return self._wait_for_task(task_id, file_path.name)

    def _wait_for_task(self, task_id: str, file_name: str, timeout: int = 300) -> str:
        """轮询任务进度，返回 doc_id。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self._session.get(
                f"{self._api_base}/kb/document/upload/progress",
                headers=self._headers(),
                params={"task_id": task_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            status = data.get("status", "")

            if status == "completed":
                uploaded = data.get("result", {}).get("uploaded", [])
                if uploaded:
                    doc_id = uploaded[0].get("doc_id", task_id)
                    self.log.info(f"摄入完成：{file_name}（doc_id={doc_id}）")
                    return doc_id
                raise RuntimeError(f"任务完成但无上传记录：{data}")

            if status == "failed":
                raise RuntimeError(f"任务失败：{data.get('error', '未知错误')}")

            stage = data.get("stage", "")
            current = data.get("current", 0)
            total = data.get("total", 100)
            self.log.debug(f"  进度 [{stage}] {current}/{total}")
            time.sleep(2)

        raise TimeoutError(f"等待任务超时（{timeout}s）：{task_id}")


# ----------------------------------------------------------------
# 文件事件处理器
# ----------------------------------------------------------------
def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def quarantine_file(
    path: Path, root: Path, failed_root: Path, reason: str
) -> Path | None:
    if not path.exists() or not _is_relative_to(path, root):
        return None

    rel = path.relative_to(root)
    dest = failed_root / rel
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    dest.with_name(f"{dest.name}.error.txt").write_text(
        "\n".join(
            [
                f"failed_at={datetime.now().isoformat(timespec='seconds')}",
                f"source={path}",
                f"reason={reason}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return dest


def should_quarantine_error(reason: str) -> bool:
    parser_failure_markers = (
        "文档解析失败",
        "任务完成但无上传记录",
        "任务失败",
        "文件格式受支持",
        "文件内容未损坏",
    )
    non_retryable_upload_markers = (
        "413 Request Entity Too Large",
        "capacity limit",
    )
    return any(
        marker in reason
        for marker in parser_failure_markers + non_retryable_upload_markers
    )


class NASIngestHandler(FileSystemEventHandler):
    def __init__(self, cfg: dict, client: AstrBotKBClient, state: IngestState):
        super().__init__()
        self._cfg = cfg
        self._client = client
        self._state = state
        self._inbox = Path(cfg["nas"]["mount_point"]) / cfg["watch"]["inbox_dir"]
        self._processed = (
            Path(cfg["nas"]["mount_point"]) / cfg["watch"]["processed_dir"]
        )
        self._failed = Path(cfg["nas"]["mount_point"]) / cfg["watch"].get(
            "failed_dir", "failed"
        )
        self._extensions = set(cfg["watch"]["supported_extensions"])
        self._settle = float(cfg["watch"].get("settle_seconds", 3))
        self.log = logging.getLogger("nas.handler")

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() in self._extensions:
            self.log.info(f"检测到新文件：{path.name}，等待写入完成...")
            time.sleep(self._settle)
            self._ingest(path)

    def on_moved(self, event):
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() in self._extensions:
            time.sleep(self._settle)
            self._ingest(path)

    def _ingest(self, path: Path):
        if not path.exists():
            return
        try:
            file_hash = md5_file(path)
            rel = str(path.relative_to(self._inbox))
            if self._state.is_ingested(rel, file_hash):
                self.log.debug(f"跳过（已摄入）：{path.name}")
                return

            doc_id = self._client.upload_file(path)
            self._state.mark_ingested(rel, file_hash, doc_id)

            # 移动到 processed
            dest = self._processed / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            self.log.info(f"已归档到 processed/：{rel}")

        except Exception as e:
            self.log.error(f"摄入失败：{path.name}  原因：{e}")
            reason = str(e)
            if should_quarantine_error(reason):
                failed_path = quarantine_file(path, self._inbox, self._failed, reason)
                if failed_path:
                    self.log.warning(
                        f"已隔离到 failed/：{failed_path.relative_to(self._failed)}"
                    )


# ----------------------------------------------------------------
# 全量扫描（处理启动前已存在的文件 / --once 模式）
# ----------------------------------------------------------------
def scan_inbox(
    cfg: dict,
    client: AstrBotKBClient,
    state: IngestState,
    *,
    full_scan_override: bool | None = None,
) -> dict[str, int]:
    mount = Path(cfg["nas"]["mount_point"])
    full_scan: bool = bool(cfg["watch"].get("full_scan", False))
    if full_scan_override is not None:
        full_scan = full_scan_override
    extensions = set(cfg["watch"]["supported_extensions"])
    exclude_dirs = set(
        cfg["watch"].get("exclude_dirs", ["#recycle", "processed", "archive"])
    )
    log = logging.getLogger("nas.scan")
    stats = {"scanned": 0, "ingested": 0, "skipped": 0, "failed": 0, "quarantined": 0}

    if not mount.exists():
        log.warning(f"挂载点不存在：{mount}（NAS 是否已挂载？）")
        stats["failed"] += 1
        return stats

    if full_scan:
        # 递归扫描整个挂载目录，跳过排除目录
        scan_root = mount
        log.info(f"全量扫描模式：{scan_root}")

        all_files = []
        for dirpath, dirnames, filenames in os.walk(scan_root):
            # 过滤排除目录
            dirnames[:] = [
                d for d in dirnames if d not in exclude_dirs and not d.startswith(".")
            ]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in extensions:
                    all_files.append(fpath)

        if not all_files:
            log.info("未找到可索引的文件。")
            return stats

        log.info(f"发现 {len(all_files)} 个可索引文件，开始摄入...")
        for f in all_files:
            stats["scanned"] += 1
            try:
                file_hash = md5_file(f)
                rel = str(f.relative_to(mount))
                if state.is_ingested(rel, file_hash):
                    stats["skipped"] += 1
                    log.debug(f"跳过（已摄入）：{rel}")
                    continue

                doc_id = client.upload_file(f)
                state.mark_ingested(rel, file_hash, doc_id)
                stats["ingested"] += 1
                log.info(f"已摄入：{rel}")

            except Exception as e:
                stats["failed"] += 1
                log.error(f"摄入失败：{f}  原因：{e}")

    else:
        # 只扫描 inbox 目录（原有逻辑）
        inbox = mount / cfg["watch"]["inbox_dir"]
        processed = mount / cfg["watch"]["processed_dir"]
        failed = mount / cfg["watch"].get("failed_dir", "failed")

        if not inbox.exists():
            log.warning(f"inbox 目录不存在：{inbox}")
            stats["failed"] += 1
            return stats

        files = []
        for dirpath, dirnames, filenames in os.walk(inbox):
            dirnames[:] = [
                d for d in dirnames if d not in exclude_dirs and not d.startswith(".")
            ]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in extensions:
                    files.append(fpath)
        if not files:
            log.info("inbox 目录无待处理文件。")
            return stats

        log.info(f"扫描到 {len(files)} 个文件，开始批量摄入...")
        for f in files:
            stats["scanned"] += 1
            try:
                file_hash = md5_file(f)
                rel = str(f.relative_to(inbox))
                if state.is_ingested(rel, file_hash):
                    stats["skipped"] += 1
                    log.debug(f"跳过（已摄入）：{rel}")
                    dest = processed / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if f.exists():
                        shutil.move(str(f), str(dest))
                        log.info(f"已归档到 processed/：{rel}")
                    continue

                doc_id = client.upload_file(f)
                state.mark_ingested(rel, file_hash, doc_id)
                stats["ingested"] += 1

                dest = processed / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
                log.info(f"已归档到 processed/：{rel}")

            except Exception as e:
                log.error(f"摄入失败：{f.name}  原因：{e}")
                reason = str(e)
                if should_quarantine_error(reason):
                    failed_path = quarantine_file(f, inbox, failed, reason)
                    if failed_path:
                        stats["quarantined"] += 1
                        log.warning(
                            f"已隔离到 failed/：{failed_path.relative_to(failed)}"
                        )
                        continue
                stats["failed"] += 1

    return stats


# ----------------------------------------------------------------
# 入口
# ----------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="NAS 知识库文件监听摄入器")
    parser.add_argument(
        "--once", action="store_true", help="扫描一次 inbox 后退出（适合 cron）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inbox-only",
        action="store_true",
        help="本轮只扫描 inbox，忽略 config.yaml 的 full_scan 设置",
    )
    mode.add_argument(
        "--full-scan",
        action="store_true",
        help="本轮扫描整个挂载目录，忽略 config.yaml 的 full_scan 设置",
    )
    args = parser.parse_args()
    full_scan_override = None
    if args.inbox_only:
        full_scan_override = False
    elif args.full_scan:
        full_scan_override = True

    cfg = load_config()

    # 日志配置
    log_level = getattr(logging, cfg["logging"]["level"].upper(), logging.INFO)
    log_file = PROJECT_ROOT / cfg["logging"]["file"]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    log = logging.getLogger("nas")

    # 检查 NAS 挂载
    mount_point = Path(cfg["nas"]["mount_point"])
    if not mount_point.exists():
        log.error(f"挂载点不存在：{mount_point}，请先运行 ./nas_sync/mount.sh mount")
        sys.exit(1)

    client = AstrBotKBClient(cfg)
    state = IngestState()

    # --once 模式：扫描一次退出
    if args.once:
        log.info("--once 模式：扫描 inbox 一次后退出")
        stats = scan_inbox(cfg, client, state, full_scan_override=full_scan_override)
        log.info("本轮摄入统计：%s", stats)
        if stats["failed"] > 0:
            sys.exit(1)
        return

    # 常驻模式：先全量扫描，再 watchdog 监听
    try:
        from watchdog.observers import Observer
    except ModuleNotFoundError:
        log.error(
            "常驻监听模式需要安装 watchdog；如只做定时同步，请使用 --once --inbox-only"
        )
        sys.exit(2)

    log.info("启动 NAS 知识库监听器...")
    scan_inbox(cfg, client, state, full_scan_override=full_scan_override)

    inbox = mount_point / cfg["watch"]["inbox_dir"]
    handler = NASIngestHandler(cfg, client, state)
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=True)
    observer.start()

    poll_interval = int(cfg["watch"].get("poll_interval", 60))
    log.info(f"监听中：{inbox}  （每 {poll_interval}s 额外全量扫描）")

    try:
        elapsed = 0
        while True:
            time.sleep(5)
            elapsed += 5
            if elapsed >= poll_interval:
                elapsed = 0
                scan_inbox(cfg, client, state, full_scan_override=full_scan_override)
    except KeyboardInterrupt:
        log.info("收到中断信号，退出...")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
