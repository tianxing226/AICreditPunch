#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 多账号每日签到（仅 Python 标准库）。"""
from __future__ import annotations

import argparse
import base64
import binascii
import copy
import getpass
import hashlib
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

VERSION = "2.2.0"
DEFAULT_API_BASE = "https://www.codebuddy.cn"
# WorkBuddy 5.3.14 desktop AuthService uses this path; the older path returns an empty inactive payload.
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 2
DEBUG = False



@dataclass
class Account:
    name: str
    access_token: str
    uid: Optional[str] = None
    domain: str = "www.workbuddy.cn"
    enterprise_id: Optional[str] = None
    api_base: str = DEFAULT_API_BASE


@dataclass
class RequestResult:
    http_status: Optional[int]
    payload: Optional[Dict[str, Any]]
    error: Optional[str] = None


@dataclass
class AuthCandidate:
    """One account extracted from a WorkBuddy desktop auth snapshot."""

    name: str
    access_token: str
    uid: Optional[str]
    domain: str
    enterprise_id: Optional[str]
    source: Path
    freshness: float
    expires_at: Optional[float] = None


def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def clean_token(value: Any) -> str:
    """Normalize a token copied from a header or an auth JSON field."""
    token = clean_text(value)
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def enabled_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return clean_text(value).lower() not in {"0", "false", "no", "off", "disabled"}


def normalize_api_base(value: str) -> str:
    base = clean_text(value) or DEFAULT_API_BASE
    if "://" not in base:
        base = "https://" + base
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    allowed = (
        host == "codebuddy.cn"
        or host.endswith(".codebuddy.cn")
        or host == "workbuddy.cn"
        or host.endswith(".workbuddy.cn")
        or host == "copilot.tencent.com"
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise ValueError(f"API 地址校验失败: {base}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"API 地址不应带 query/fragment: {base}")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"https://{host}{port}{path}"


def account_from_mapping(raw: Dict[str, Any], index: int = 1) -> Account:
    token = clean_token(raw.get("access_token") or raw.get("accessToken") or raw.get("token"))
    if not token:
        raise ValueError(f"第 {index} 个账号缺少 access_token")
    return Account(
        name=clean_text(raw.get("name") or raw.get("account_name") or raw.get("nickname")) or f"账号{index}",
        access_token=token,
        uid=clean_text(raw.get("uid") or raw.get("user_id")) or None,
        domain=clean_text(raw.get("domain")) or "www.workbuddy.cn",
        enterprise_id=clean_text(raw.get("enterprise_id") or raw.get("enterpriseId")) or None,
        api_base=normalize_api_base(clean_text(raw.get("api_base")) or os.environ.get("WORKBUDDY_API_BASE", DEFAULT_API_BASE)),
    )


def parse_accounts_json(raw: str) -> List[Account]:
    source = raw.strip()
    if source.startswith("@"):
        source = Path(source[1:]).expanduser().read_text(encoding="utf-8-sig")
    data = json.loads(source)
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        data = data["accounts"]
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("账号配置应为 JSON 对象或数组")
    if not data:
        raise ValueError("未配置账号，请先运行 --setup 或编辑 config.json")
    accounts: List[Account] = []
    seen = set()
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个账号配置不是 JSON 对象")
        if not enabled_value(item.get("enabled")):
            continue
        account = account_from_mapping(item, index)
        identity = account.uid or account.access_token
        if identity in seen:
            log(f"跳过重复账号：{account.name}")
            continue
        seen.add(identity)
        accounts.append(account)
    if not accounts:
        raise ValueError("账号配置中没有已启用的账号")
    return accounts


def _auth_file_values(explicit: Optional[Any] = None) -> List[str]:
    """Normalize one or more explicit auth paths plus the environment value."""
    values: List[str] = []
    if isinstance(explicit, (list, tuple)):
        values.extend(clean_text(item) for item in explicit if clean_text(item))
    elif clean_text(explicit):
        values.append(clean_text(explicit))
    # A command-line path is intentionally authoritative; this makes
    # --auth-file useful when an unrelated environment value is present.
    if values:
        return values
    env_path = clean_text(os.environ.get("WORKBUDDY_AUTH_FILE"))
    if env_path:
        values.extend(item.strip() for item in env_path.split(os.pathsep) if item.strip())
    return values


def _known_auth_directories() -> List[Path]:
    """Return bounded, non-recursive auth directories for desktop platforms."""
    home = Path.home()
    candidates: List[Path] = []
    local_app_data = clean_text(os.environ.get("LOCALAPPDATA"))
    windows_roots = [Path(local_app_data) if local_app_data else home / "AppData" / "Local"]
    app_data = clean_text(os.environ.get("APPDATA"))
    if app_data:
        windows_roots.append(Path(app_data))
    for root in windows_roots:
        for product in ("CodeBuddyExtension", "WorkBuddy"):
            candidates.append(root / product / "Data" / "Public" / "auth")
    for product in ("CodeBuddyExtension", "WorkBuddy"):
        candidates.append(home / "Library" / "Application Support" / product / "Data" / "Public" / "auth")
    xdg_data = Path(clean_text(os.environ.get("XDG_DATA_HOME")) or str(home / ".local" / "share")).expanduser()
    xdg_config = Path(clean_text(os.environ.get("XDG_CONFIG_HOME")) or str(home / ".config")).expanduser()
    for root in (xdg_data, xdg_config):
        for product in ("CodeBuddyExtension", "WorkBuddy"):
            candidates.append(root / product / "Data" / "Public" / "auth")
    result: List[Path] = []
    seen = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def local_auth_paths(explicit: Optional[Any] = None) -> Iterable[Path]:
    """Yield exact auth files for the legacy no-config fallback."""
    candidates: List[Path] = []
    for value in _auth_file_values(explicit):
        path = Path(value).expanduser()
        if path.is_dir():
            candidates.append(path / "workbuddy-desktop.info")
        else:
            candidates.append(path)
    for directory in _known_auth_directories():
        candidates.append(directory / "workbuddy-desktop.info")
    seen = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            yield path


def account_from_auth_file(path: Path) -> Optional[Account]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"auth 文件读取失败（{type(exc).__name__}）: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"auth 文件顶层不是 JSON 对象: {path}")
    auth = data.get("auth") or {}
    account = data.get("account") or {}
    if not isinstance(auth, dict):
        raise ValueError(f"auth 文件缺少有效 auth 对象: {path}")
    if not isinstance(account, dict):
        account = {}
    token = clean_token(auth.get("accessToken") or auth.get("access_token"))
    if not token:
        raise ValueError(f"auth 文件缺少 auth.accessToken: {path}")
    return account_from_mapping(
        {
            "name": account.get("nickname") or "本地账号",
            "access_token": token,
            "uid": account.get("uid"),
            "domain": auth.get("domain") or "www.workbuddy.cn",
            "enterprise_id": account.get("enterpriseId") or account.get("enterprise_id"),
        }
    )


def _jwt_subject(token: str) -> Optional[str]:
    """Read a JWT subject locally when the auth file has no uid field."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error):
        return None
    return clean_text(payload.get("sub")) or None if isinstance(payload, dict) else None


def _numeric_timestamp(value: Any) -> Optional[float]:
    raw = clean_text(value)
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    if number > 100_000_000_000:
        number /= 1000.0
    return number


def _account_objects(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for key in ("account", "accounts", "allAccounts"):
        value = data.get(key)
        if isinstance(value, dict):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, dict))
    return values


def _extract_auth_candidate(path: Path) -> Optional[AuthCandidate]:
    """Extract one account from a desktop auth snapshot without logging secrets."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        log(f"跳过认证文件：{path}（读取或 JSON 解析失败）")
        return None
    if not isinstance(data, dict):
        log(f"跳过认证文件：{path}（顶层不是 JSON 对象）")
        return None
    auth = data.get("auth")
    if not isinstance(auth, dict):
        log(f"跳过认证文件：{path}（缺少 auth 对象）")
        return None
    token = clean_token(auth.get("accessToken") or auth.get("access_token"))
    if not token:
        log(f"跳过认证文件：{path}（缺少 accessToken）")
        return None

    uid_from_token = _jwt_subject(token)
    objects = _account_objects(data)
    account: Dict[str, Any] = {}
    if uid_from_token:
        account = next(
            (
                item
                for item in objects
                if clean_text(item.get("uid") or item.get("user_id")) == uid_from_token
            ),
            {},
        )
    if not account:
        account = next(
            (
                item
                for item in objects
                if item.get("lastLogin") is True
                or clean_text(item.get("lastLogin")).lower() in {"true", "1", "yes"}
            ),
            objects[0] if objects else {},
        )

    uid = clean_text(account.get("uid") or account.get("user_id")) or uid_from_token
    name = clean_text(
        account.get("nickname")
        or account.get("name")
        or account.get("displayName")
        or account.get("username")
        or account.get("uin")
    )
    if not name:
        name = f"账号-{uid[-8:]}" if uid else path.stem
    domain = clean_text(auth.get("domain") or account.get("domain")) or "www.workbuddy.cn"
    enterprise_id = clean_text(
        account.get("enterpriseId")
        or account.get("enterprise_id")
        or account.get("tenantId")
        or account.get("tenant_id")
        or auth.get("enterpriseId")
        or auth.get("enterprise_id")
        or auth.get("tenantId")
        or auth.get("tenant_id")
    ) or None
    try:
        file_mtime = path.stat().st_mtime
    except OSError:
        file_mtime = 0.0
    freshness = max(
        file_mtime,
        _numeric_timestamp(auth.get("lastRefreshTime") or auth.get("lastRefreshAt") or auth.get("updatedAt")) or 0.0,
    )
    return AuthCandidate(
        name=name,
        access_token=token,
        uid=uid,
        domain=domain,
        enterprise_id=enterprise_id,
        source=path,
        freshness=freshness,
        expires_at=_numeric_timestamp(auth.get("expiresAt") or auth.get("expires_at")),
    )


def _setup_auth_files(explicit: Optional[Sequence[str]]) -> List[Path]:
    """Collect explicit/env files and known directory snapshots, non-recursively."""
    values = _auth_file_values(explicit)
    paths: List[Path] = []
    if values:
        for value in values:
            path = Path(value).expanduser()
            if path.is_dir():
                paths.extend(sorted(path.glob("workbuddy-desktop*.info")))
            else:
                paths.append(path)
    else:
        for directory in _known_auth_directories():
            if directory.is_dir():
                paths.extend(sorted(directory.glob("workbuddy-desktop*.info")))

    result: List[Path] = []
    seen = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            result.append(resolved)
        elif values:
            log(f"未找到认证文件：{path}")
    return result


def _discover_setup_candidates(explicit: Optional[Sequence[str]]) -> List[AuthCandidate]:
    selected: Dict[str, AuthCandidate] = {}
    for path in _setup_auth_files(explicit):
        candidate = _extract_auth_candidate(path)
        if candidate is None:
            continue
        identity = candidate.uid or "token:" + hashlib.sha256(candidate.access_token.encode("utf-8")).hexdigest()
        previous = selected.get(identity)
        if previous is None or (candidate.freshness, str(candidate.source)) > (previous.freshness, str(previous.source)):
            selected[identity] = candidate
    return sorted(selected.values(), key=lambda item: (item.name, item.uid or "", str(item.source)))


def _mask_uid(uid: Optional[str]) -> str:
    value = clean_text(uid)
    return f"***{value[-8:]}" if len(value) > 8 else ("***" if value else "未提供")


def _prompt_setup_candidate() -> AuthCandidate:
    log("未找到本地 WorkBuddy 认证文件，进入手动配置；access_token 输入时不会回显")
    try:
        token = clean_token(getpass.getpass("access_token: "))
    except (EOFError, KeyboardInterrupt) as exc:
        raise ValueError("已取消手动配置") from exc
    if not token:
        raise ValueError("access_token 不能为空")
    derived_uid = _jwt_subject(token) or ""
    try:
        uid = input(f"uid [{derived_uid}]: " if derived_uid else "uid: ").strip() or derived_uid
        name = input("name [WorkBuddy账号]: ").strip() or "WorkBuddy账号"
        domain = input("domain [www.workbuddy.cn]: ").strip() or "www.workbuddy.cn"
        enterprise_id = input("enterprise_id（可留空）: ").strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise ValueError("已取消手动配置") from exc
    if not uid:
        raise ValueError("uid 不能为空；请从登录文件获取 uid，或输入 JWT 的 sub")
    return AuthCandidate(
        name=name,
        access_token=token,
        uid=uid,
        domain=domain,
        enterprise_id=enterprise_id or None,
        source=Path("<manual>"),
        freshness=time.time(),
    )


def _load_setup_config(path: Path) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Load setup records while preserving an optional top-level wrapper."""
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"配置文件读取失败（{type(exc).__name__}）: {path}") from exc
    wrapper: Optional[Dict[str, Any]] = None
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("accounts"), list):
        wrapper = copy.deepcopy(data)
        records = data["accounts"]
    elif isinstance(data, dict):
        records = [data]
    else:
        raise ValueError("配置文件顶层必须是数组或包含 accounts 数组的对象")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("配置文件中的每个账号必须是 JSON 对象")
    return [copy.deepcopy(item) for item in records], wrapper


def _record_uid(record: Dict[str, Any]) -> Optional[str]:
    uid = clean_text(record.get("uid") or record.get("user_id"))
    if uid:
        return uid
    # A hand-written record may omit uid; recover it locally from a JWT when
    # possible so setup still rotates the existing account instead of adding a duplicate.
    return _jwt_subject(_record_token(record))


def _record_token(record: Dict[str, Any]) -> str:
    return clean_token(record.get("access_token") or record.get("accessToken") or record.get("token"))


def _candidate_record(candidate: AuthCandidate, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a config record and retain intentional local metadata."""
    record = copy.deepcopy(existing) if existing is not None else {}
    if "enabled" not in record:
        record["enabled"] = True
    if not clean_text(record.get("name")):
        record["name"] = candidate.name
    # Normalize the credential key so future updates cannot leave an old token
    # under accessToken/token while the new value is written elsewhere.
    record["access_token"] = candidate.access_token
    record.pop("accessToken", None)
    record.pop("token", None)
    if candidate.uid:
        record["uid"] = candidate.uid
    # These values belong to the current login and must rotate with its token;
    # in particular, clear an old enterprise id when an account becomes personal.
    record["domain"] = candidate.domain or clean_text(record.get("domain")) or "www.workbuddy.cn"
    record["enterprise_id"] = candidate.enterprise_id or ""
    if not clean_text(record.get("api_base")):
        record["api_base"] = DEFAULT_API_BASE
    return record


def _merge_setup_records(
    existing: Sequence[Dict[str, Any]],
    candidates: Sequence[AuthCandidate],
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Merge imported accounts by UID (or token when UID is absent)."""
    result = [copy.deepcopy(item) for item in existing]
    by_uid: Dict[str, int] = {}
    by_token: Dict[str, int] = {}
    for index, record in enumerate(result):
        uid = _record_uid(record)
        if uid and uid not in by_uid:
            by_uid[uid] = index
        token = _record_token(record)
        if token:
            token_digest_value = hashlib.sha256(token.encode("utf-8")).hexdigest()
            by_token.setdefault(token_digest_value, index)

    updated = 0
    added = 0
    for candidate in candidates:
        token_digest_value = hashlib.sha256(candidate.access_token.encode("utf-8")).hexdigest()
        index = by_uid.get(candidate.uid) if candidate.uid else None
        if index is None and not candidate.uid:
            index = by_token.get(token_digest_value)
        if index is None:
            result.append(_candidate_record(candidate))
            index = len(result) - 1
            added += 1
        else:
            result[index] = _candidate_record(candidate, result[index])
            updated += 1
        if candidate.uid:
            by_uid[candidate.uid] = index
        by_token[token_digest_value] = index

    # Remove pre-existing duplicate identities while keeping the first record's
    # ordering and metadata.  This also makes repeated setup runs idempotent.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for record in result:
        identity = _record_uid(record)
        if identity:
            identity = "uid:" + identity
        else:
            token = _record_token(record)
            identity = "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest() if token else "record:" + str(len(deduped))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(record)
    return deduped, updated, added


def _serialize_setup_config(records: Sequence[Dict[str, Any]], wrapper: Optional[Dict[str, Any]]) -> str:
    output: Any = list(records)
    if wrapper is not None:
        output = copy.deepcopy(wrapper)
        output["accounts"] = list(records)
    return json.dumps(output, ensure_ascii=False, indent=2) + "\n"


def _atomic_setup_write(path: Path, content: str) -> Optional[Path]:
    """Write config atomically and retain a sibling .bak when it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if path.exists():
        backup = Path(str(path) + ".bak")
        shutil.copy2(path, backup)
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return backup


def _setup_config(config_path: Path, auth_file: Optional[str], dry_run: bool, manual: bool) -> int:
    if manual:
        if auth_file:
            log("--manual 与 --auth-file 请分开使用")
            return 1
        if not sys.stdin.isatty():
            log("--setup --manual 需要交互终端；请在终端直接运行，或从已登录电脑复制 config.json")
            return 1
        candidates = [_prompt_setup_candidate()]
    else:
        candidates = []
    explicit = [auth_file] if auth_file else []
    if auth_file and not manual:
        requested = Path(auth_file).expanduser()
        if not requested.exists():
            log(f"指定认证路径不存在：{requested}")
            return 1
    if not manual:
        candidates = _discover_setup_candidates(explicit)
    if not candidates:
        if auth_file:
            log(f"指定路径中没有可用的 workbuddy-desktop*.info：{Path(auth_file).expanduser()}")
            return 1
        log("未发现可用认证文件；请先登录 WorkBuddy，或在交互终端运行 --setup --manual")
        return 1

    existing, wrapper = _load_setup_config(config_path)
    merged, updated, added = _merge_setup_records(existing, candidates)
    log(f"发现可用账号：{len(candidates)} 个")
    for candidate in candidates:
        source = "手动输入" if str(candidate.source) == "<manual>" else candidate.source.name
        expiry = "；已过期" if candidate.expires_at is not None and candidate.expires_at <= time.time() else ""
        log(f"- {candidate.name}；UID={_mask_uid(candidate.uid)}；token_length={len(candidate.access_token)}；来源={source}{expiry}")
    log(f"配置合并：更新 {updated} 个，新增 {added} 个，最终保留 {len(merged)} 个账号")
    if dry_run:
        log("Setup dry-run 完成：未写入 config.json")
        return 0
    backup = _atomic_setup_write(config_path, _serialize_setup_config(merged, wrapper))
    if backup:
        log(f"配置已更新：{config_path}；备份={backup}")
    else:
        log(f"配置已创建：{config_path}")
    log("一键配置完成；接下来运行脚本即可签到")
    return 0


def load_accounts(config_path: Optional[str] = None, auth_file: Optional[Any] = None) -> Tuple[List[Account], str]:
    raw_accounts = clean_text(os.environ.get("WORKBUDDY_ACCOUNTS"))
    if raw_accounts:
        return parse_accounts_json(raw_accounts), "环境变量 WORKBUDDY_ACCOUNTS"

    token = clean_text(os.environ.get("WORKBUDDY_ACCESS_TOKEN"))
    if token:
        account = account_from_mapping(
            {
                "name": os.environ.get("WORKBUDDY_ACCOUNT_NAME") or "环境变量账号",
                "access_token": token,
                "uid": os.environ.get("WORKBUDDY_UID"),
                "domain": os.environ.get("WORKBUDDY_DOMAIN") or "www.workbuddy.cn",
                "enterprise_id": os.environ.get("WORKBUDDY_ENTERPRISE_ID"),
                "api_base": os.environ.get("WORKBUDDY_API_BASE") or DEFAULT_API_BASE,
            }
        )
        return [account], "环境变量 WORKBUDDY_ACCESS_TOKEN"

    candidates: List[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser())
    env_config = clean_text(os.environ.get("WORKBUDDY_CONFIG"))
    if env_config:
        candidates.append(Path(env_config).expanduser())
    candidates.append(Path(__file__).resolve().parent / "config.json")
    seen_paths = set()
    for path in candidates:
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        if path.is_file():
            return parse_accounts_json(path.read_text(encoding="utf-8-sig")), f"配置文件 {path}"

    if auth_file:
        path = Path(auth_file).expanduser()
        account = account_from_auth_file(path)
        if account:
            return [account], f"本地 auth 文件 {path}"

    for path in local_auth_paths(auth_file):
        account = account_from_auth_file(path)
        if account:
            return [account], f"本地 auth 文件 {path}"

    raise RuntimeError("未找到账号配置；请上传同目录 config.json，或设置 WORKBUDDY_ACCOUNTS")


def build_headers(account: Account) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"WorkBuddy-Checkin/{VERSION}",
    }
    if account.uid:
        headers["X-User-Id"] = account.uid
    if account.domain:
        headers["X-Domain"] = account.domain
    if account.enterprise_id:
        headers["X-Enterprise-Id"] = account.enterprise_id
        headers["X-Tenant-Id"] = account.enterprise_id
    return headers


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(marker in lower for marker in ("token", "auth", "cookie", "secret")):
                result[key] = "***"
            else:
                result[key] = redact_payload(item)
        return result
    if isinstance(value, list):
        return [redact_payload(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


def request_json(
    url: str,
    account: Account,
    timeout: int,
    retries: int,
) -> RequestResult:
    body = b"{}"
    headers = build_headers(account)
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                raw = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    return RequestResult(status, None, "响应 JSON 顶层不是对象")
                if DEBUG:
                    log("调试响应: " + json.dumps(redact_payload(payload), ensure_ascii=False)[:1500])
                return RequestResult(status, payload)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            payload = None
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    pass
            if DEBUG and payload is not None:
                log("调试错误响应: " + json.dumps(redact_payload(payload), ensure_ascii=False)[:1500])
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                return RequestResult(int(exc.code), payload, f"HTTP {exc.code} {exc.reason}")
            wait_seconds = min(8, 2 ** attempt)
            log(f"遇到 HTTP {exc.code}，{wait_seconds} 秒后重试（{attempt + 1}/{retries}）")
            time.sleep(wait_seconds)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt >= retries:
                return RequestResult(None, None, f"网络请求失败: {exc}")
            wait_seconds = min(8, 2 ** attempt)
            log(f"网络波动，{wait_seconds} 秒后重试（{attempt + 1}/{retries}）")
            time.sleep(wait_seconds)
        except Exception as exc:
            return RequestResult(None, None, f"请求异常: {type(exc).__name__}: {exc}")
    return RequestResult(None, None, "请求重试结束")


def message_of(payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    nested = data.get("message") if isinstance(data, dict) else ""
    return clean_text(payload.get("message") or payload.get("msg") or nested)


def code_of(payload: Optional[Dict[str, Any]]) -> Any:
    return payload.get("code") if isinstance(payload, dict) else None


def is_business_ok(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if code not in (None, 0, 200, "0", "200"):
        return False
    if payload.get("success") is False:
        return False
    return True


def data_of(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def truthy_field(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return clean_text(value).lower() in {"true", "1", "yes", "y", "checked", "checked_in"}


def already_checked_in(payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    message = message_of(payload).lower()
    if code in (10001, "10001"):
        return True
    if any(text in message for text in ("已签到", "已经签到", "already checked", "already claimed")):
        return True
    data = data_of(payload) or {}
    return any(
        truthy_field(data.get(field))
        for field in ("today_checked_in", "checked_in", "checkedIn", "claimed")
    )


def claim_has_success_evidence(payload: Optional[Dict[str, Any]]) -> bool:
    if not is_business_ok(payload):
        return False
    data = data_of(payload)
    if not isinstance(data, dict) or not data or data.get("success") is False:
        return False
    if data.get("success") is True:
        return True
    return any(
        field in data
        for field in (
            "credit",
            "daily_credit",
            "today_credit",
            "points",
            "streak_days",
            "total_credits",
            "today_checked_in",
            "checked_in",
            "checkedIn",
            "claimed",
        )
    )


def describe_status(payload: Optional[Dict[str, Any]]) -> str:
    data = data_of(payload) or {}
    fields = []
    for key in ("active", "today_checked_in", "streak_days", "daily_credit", "today_credit", "total_credits"):
        if key in data:
            fields.append(f"{key}={data.get(key)}")
    return ", ".join(fields) if fields else "状态字段未返回"


def first_present(data: Dict[str, Any], fields: Iterable[str]) -> Any:
    for field in fields:
        if field in data and data.get(field) is not None:
            return data.get(field)
    return None


def describe_credits(payload: Optional[Dict[str, Any]], pending: bool = False) -> str:
    data = data_of(payload) or {}
    reward = data.get("reward")
    containers = [data]
    if isinstance(reward, dict):
        containers.append(reward)

    def find(fields: Iterable[str]) -> Any:
        for container in containers:
            value = first_present(container, fields)
            if value is not None:
                return value
        return None

    earned = find(("credit", "credits", "points", "reward_credit", "reward_points", "awarded_credit"))
    daily = find(("today_credit", "daily_credit", "today_points", "daily_points"))
    total = find(("total_credits", "total_credit", "total_points"))
    balance = find(("credit_balance", "credits_balance", "point_balance", "points_balance", "balance"))
    streak = find(("streak_days", "streakDays", "consecutive_days"))

    details = []
    if earned is not None:
        details.append(f"本次获得积分={earned}")
    elif daily is not None:
        details.append(f"{'今日可领' if pending else '今日积分'}={daily}")
    if total is not None:
        details.append(f"本期累计积分={total}")
    if balance is not None:
        details.append(f"可用积分={balance}")
    if streak is not None:
        details.append(f"连续签到={streak}天")
    return "，".join(details)


def with_credit_detail(message: str, *details: str) -> str:
    unique = []
    for group in details:
        for detail in group.replace("；", "，").split("，"):
            detail = detail.strip()
            if detail and detail not in unique:
                unique.append(detail)
    return message + ("；" + "，".join(unique) if unique else "")


def run_account(account: Account, status_only: bool, timeout: int, retries: int) -> Tuple[bool, str]:
    status_url = account.api_base + STATUS_PATH
    claim_url = account.api_base + CLAIM_PATH
    log(f"[{account.name}] 查询签到状态；API={account.api_base}；domain={account.domain}")
    status_result = request_json(status_url, account, timeout, retries)
    if already_checked_in(status_result.payload):
        detail = describe_status(status_result.payload)
        credits = describe_credits(status_result.payload)
        log(with_credit_detail(f"[{account.name}] 今日已签到；{detail}", credits))
        return True, with_credit_detail(f"{account.name}: 今日已签到", credits)

    if status_result.payload is not None and is_business_ok(status_result.payload):
        status_data = data_of(status_result.payload) or {}
        status_credits = describe_credits(status_result.payload, pending=True)
        log(with_credit_detail(f"[{account.name}] {describe_status(status_result.payload)}", status_credits))
        if status_only:
            return True, with_credit_detail(f"{account.name}: 状态查询成功，今日待签到", status_credits)
        if "active" in status_data and not truthy_field(status_data.get("active")):
            log(f"[{account.name}] 状态接口返回 active=false，仍提交领取并以领取结果及回查为准")
    else:
        detail = message_of(status_result.payload) or status_result.error or "未知错误"
        log(f"[{account.name}] 状态查询异常：HTTP={status_result.http_status} code={code_of(status_result.payload)} {detail}")
        if status_result.http_status in (401, 403):
            return False, f"{account.name}: 凭证失效或权限不足"
        if status_only:
            return False, f"{account.name}: 状态查询失败"
        log(f"[{account.name}] 继续尝试领取，以领取接口结果为准")

    log(f"[{account.name}] 提交每日签到")
    claim_result = request_json(claim_url, account, timeout, retries)
    if already_checked_in(claim_result.payload):
        credits = describe_credits(claim_result.payload)
        log(with_credit_detail(f"[{account.name}] 领取接口确认今日已签到", credits))
        return True, with_credit_detail(f"{account.name}: 今日已签到", credits)
    if claim_result.payload is None or not is_business_ok(claim_result.payload):
        detail = message_of(claim_result.payload) or claim_result.error or "未知错误"
        log(f"[{account.name}] 签到失败：HTTP={claim_result.http_status} code={code_of(claim_result.payload)} {detail}")
        return False, f"{account.name}: 签到失败 - {detail[:80]}"

    data = data_of(claim_result.payload) or {}
    if not claim_has_success_evidence(claim_result.payload):
        detail = clean_text(data.get("message")) or "领取接口未返回可确认的成功证据"
        log(f"[{account.name}] 签到失败：{detail}")
        return False, f"{account.name}: 签到失败 - {detail[:80]}"
    claim_credits = describe_credits(claim_result.payload)
    log(with_credit_detail(f"[{account.name}] 领取接口已受理，回查签到状态", claim_credits))
    verify_result = request_json(status_url, account, timeout, retries)
    if already_checked_in(verify_result.payload):
        verify_credits = describe_credits(verify_result.payload)
        log(with_credit_detail(
            f"[{account.name}] 回查确认今日已签到；{describe_status(verify_result.payload)}",
            claim_credits,
            verify_credits,
        ))
        return True, with_credit_detail(
            f"{account.name}: 签到成功并已回查确认",
            claim_credits,
            verify_credits,
        )

    detail = message_of(verify_result.payload) or verify_result.error or describe_status(verify_result.payload)
    log(
        f"[{account.name}] 领取接口已受理，但回查未确认签到："
        f"HTTP={verify_result.http_status} code={code_of(verify_result.payload)} {detail}"
    )
    return False, f"{account.name}: 领取后回查未确认签到"


def try_notify(title: str, content: str) -> None:
    if not env_bool("WORKBUDDY_NOTIFY", False):
        return
    # Keep the two conventional panel locations for existing deployments;
    # standalone users still resolve notify.py from the script directory.
    for path in ("/ql/data/scripts", "/ql/scripts", str(Path(__file__).resolve().parent)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        module = importlib.import_module("notify")
        sender = getattr(module, "send", None)
        if callable(sender):
            sender(title, content)
            log("通知模块已调用")
        else:
            log("已启用通知，但 notify.py 中未找到 send()")
    except Exception as exc:
        log(f"通知调用异常：{type(exc).__name__}: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WorkBuddy 多账号每日签到（仅 Python 标准库）")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--auth-file", help="本地 workbuddy-desktop.info 路径")
    parser.add_argument("--setup", action="store_true", help="自动读取本机登录信息并创建或更新 config.json")
    parser.add_argument("--manual", action="store_true", help="与 --setup 同用，隐藏输入 token 手动配置")
    parser.add_argument("--status-only", action="store_true", help="仅查询状态，不领取")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置，不发网络请求")
    parser.add_argument("--debug", action="store_true", help="输出脱敏后的响应结构")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args()


def main() -> int:
    global DEBUG
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args()
    DEBUG = bool(args.debug or env_bool("WORKBUDDY_DEBUG", False))
    if args.manual and not args.setup:
        log("--manual 需与 --setup 同时使用")
        return 2
    timeout = env_int("WORKBUDDY_TIMEOUT", DEFAULT_TIMEOUT, 5, 120)
    retries = env_int("WORKBUDDY_RETRIES", DEFAULT_RETRIES, 0, 5)
    if args.setup:
        if args.config:
            setup_path = Path(args.config).expanduser()
        elif clean_text(os.environ.get("WORKBUDDY_CONFIG")):
            setup_path = Path(clean_text(os.environ["WORKBUDDY_CONFIG"])).expanduser()
        else:
            setup_path = Path(__file__).resolve().parent / "config.json"
        try:
            return _setup_config(setup_path, args.auth_file, args.dry_run, args.manual)
        except (OSError, ValueError) as exc:
            log(f"设置失败：{type(exc).__name__}: {exc}")
            return 1
        except KeyboardInterrupt:
            log("已取消设置")
            return 130

    log(f"WorkBuddy 多账号签到 v{VERSION} 启动")
    try:
        accounts, source = load_accounts(args.config, args.auth_file)
    except Exception as exc:
        log(f"账号配置错误：{type(exc).__name__}: {exc}")
        return 1
    log(f"已加载 {len(accounts)} 个账号；来源={source}；timeout={timeout}s；retries={retries}")
    for account in accounts:
        log(f"配置检查：账号={account.name}；token_length={len(account.access_token)}；domain={account.domain}；api={account.api_base}")
    if args.dry_run:
        log("Dry-run 完成：配置有效，未发出网络请求")
        return 0

    results: List[Tuple[bool, str]] = []
    for account in accounts:
        try:
            results.append(run_account(account, args.status_only, timeout, retries))
        except Exception as exc:
            log(f"[{account.name}] 未处理异常：{type(exc).__name__}: {exc}")
            results.append((False, f"{account.name}: 执行异常"))

    success_count = sum(1 for ok, _ in results if ok)
    summary = "\n".join(text for _, text in results)
    log(f"执行完成：成功 {success_count}/{len(results)}")
    try_notify("WorkBuddy 每日签到", summary)
    return 0 if success_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())


