#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import ctypes
import ftplib
import ipaddress
import json
import os
import posixpath
import re
import shutil
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence


FAN_TOKEN_SPLIT = re.compile(r"[,\uFF0C;\uFF1B\s]+")
FAN_RANGE = re.compile(r"^(\d{1,3})(?:-|~|\u81F3)(\d{1,3})$")
TRANSIENT_FILE = re.compile(r"^.*?(\d{4,})[^\d]+(\d{8})(?:\D|$)", re.IGNORECASE)
PITCH_FILE = re.compile(r"^PT(\d{8})_(.+)\.txt$", re.IGNORECASE)
MASTER_TYPES = ("B", "F", "O")
MASTER_DATE_TOKEN = re.compile(r"(?<!\d)(\d{6}|\d{8})(?!\d)")
MASTER_TYPED_FILE = re.compile(r"^([BFO])(\d{6}|\d{8})(?:\D|$)", re.IGNORECASE)
VIBRATION_TYPES = ("BMSDATA", "TMSDATA", "WAVEDATA")
VIBRATION_TYPE_LABELS = {
    "BMSDATA": "BM（BMS）",
    "TMSDATA": "TM（TMS）",
    "WAVEDATA": "WA（波形）",
}
NETWORK_PRESETS = {
    "风机IP": [("192.168.151.201", "255.255.248.0")],
    "变桨IP": [("192.168.180.201", "255.255.255.0")],
    "净空IP": [("192.168.143.201", "255.255.255.0")],
    "雷达IP": [("192.168.144.201", "255.255.255.0")],
    "变流IP": [("192.168.153.201", "255.255.255.0")],
    "交换机IP": [("192.168.148.201", "255.255.255.0")],
    "消防IP": [("192.168.119.201", "255.255.255.0")],
    "振动IP": [("192.168.160.201", "255.255.255.0")],
}
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class CollectorError(RuntimeError):
    pass


class CancelledError(CollectorError):
    pass


class InsufficientSpaceError(CollectorError):
    pass


class NoMatchingFilesError(CollectorError):
    pass


@dataclass(frozen=True)
class CopyResult:
    source: str
    destination: Path
    size: int
    status: str
    resumed_from: int = 0


def partial_resume_offset(
    destination: Path, source_id: str, size: int, modified: int
) -> int:
    temporary = destination.with_name(destination.name + ".part")
    metadata_path = destination.with_name(destination.name + ".part.json")
    if not temporary.is_file() or not metadata_path.is_file():
        return 0
    metadata = load_json(metadata_path, {})
    expected = {"source": source_id, "size": int(size), "modified": int(modified)}
    if any(metadata.get(key) != value for key, value in expected.items()):
        return 0
    partial_size = temporary.stat().st_size
    return partial_size if 0 <= partial_size <= size else 0


def _prepare_partial(
    destination: Path,
    source_id: str,
    size: int,
    modified: int,
    overwrite: bool,
) -> tuple[Path, Path, int]:
    temporary = destination.with_name(destination.name + ".part")
    metadata_path = destination.with_name(destination.name + ".part.json")
    if overwrite:
        temporary.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
    offset = partial_resume_offset(destination, source_id, size, modified)
    if temporary.exists() and offset == 0:
        temporary.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
    save_json_atomic(
        metadata_path,
        {"source": source_id, "size": int(size), "modified": int(modified)},
    )
    return temporary, metadata_path, offset


def require_private_ipv4(value: str, label: str = "服务器地址") -> str:
    """Reject DNS names and any address outside the RFC1918 private ranges."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label}必须填写内网 IPv4 地址，不能使用域名：{value}") from exc
    if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError(f"{label}不是允许的内网地址：{address}")
    return str(address)


def validate_network_address(ip_value: str, mask_value: str) -> tuple[str, str, int]:
    """Validate a private IPv4 and contiguous subnet mask for local adapter setup."""
    address = require_private_ipv4(ip_value, "网卡 IPv4 地址")
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{mask_value.strip()}")
    except ValueError as exc:
        raise ValueError(f"子网掩码无效：{mask_value}") from exc
    prefix = network.prefixlen
    if prefix >= 31:
        raise ValueError("子网掩码不能小于 /30，请填写有效的局域网掩码。")
    return address, str(network.netmask), prefix


def parse_fan_spec(value: str) -> list[int]:
    """Parse values such as '1-20, 25, 31' into sorted unique fan numbers."""
    value = re.sub(r"\s*(-|~|\u81F3)\s*", r"\1", value.strip())
    if not value:
        raise ValueError("请输入风机号，例如 1-20 或 1,3,8。")

    fans: set[int] = set()
    for token in FAN_TOKEN_SPLIT.split(value):
        if not token:
            continue
        range_match = FAN_RANGE.match(token)
        if range_match:
            start, end = map(int, range_match.groups())
            if start > end:
                start, end = end, start
            fans.update(range(start, end + 1))
            continue
        if not token.isdigit():
            raise ValueError(f"无法识别风机号：{token}")
        fans.add(int(token))

    invalid = sorted(fan for fan in fans if fan < 1 or fan > 254)
    if invalid:
        raise ValueError(f"风机号必须在 1-254：{invalid}")
    if not fans:
        raise ValueError("没有有效的风机号。")
    return sorted(fans)


def build_fan_hosts(prefix_value: str, fan_value: str, label: str) -> list[tuple[str, int]]:
    """Build private IPv4 targets from a three-octet prefix and a fan selection."""
    prefix = prefix_value.strip().strip(".")
    if len(prefix.split(".")) != 3:
        raise ValueError(f"{label}必须是三段，例如 192.168.151；不要在这里填写风机号。")
    require_private_ipv4(f"{prefix}.1", label)
    fans = parse_fan_spec(fan_value)
    return [(f"{prefix}.{fan}", fan) for fan in fans]


def parse_ip_range(value: str) -> list[str]:
    text = value.strip()
    if not text:
        raise ValueError("请输入 IP 范围。")
    hosts: list[str] = []
    for raw_part in FAN_TOKEN_SPLIT.split(text):
        part = raw_part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                network = ipaddress.ip_network(part, strict=False)
            except ValueError as exc:
                raise ValueError(f"IP 网段格式不正确：{part}") from exc
            for host in network.hosts():
                address = require_private_ipv4(str(host), "主控地址")
                hosts.append(address)
            continue
        if "-" in part or "~" in part or "至" in part:
            separator = "-" if "-" in part else "~" if "~" in part else "至"
            start_text, end_text = [item.strip() for item in part.split(separator, 1)]
            try:
                start_ip = ipaddress.ip_address(start_text)
            except ValueError as exc:
                raise ValueError(f"IP 范围格式不正确：{part}") from exc
            if start_ip.version != 4:
                raise ValueError("主控地址必须是 IPv4。")
            if "." in end_text:
                try:
                    end_ip = ipaddress.ip_address(end_text)
                except ValueError as exc:
                    raise ValueError(f"IP 范围格式不正确：{part}") from exc
                if end_ip.version != 4:
                    raise ValueError("主控地址必须是 IPv4。")
            else:
                octets = start_text.split(".")
                if len(octets) != 4 or not end_text.isdigit():
                    raise ValueError(f"IP 范围格式不正确：{part}")
                end_octet = int(end_text)
                if not 0 <= end_octet <= 255:
                    raise ValueError(f"IP 末位超出范围：{end_text}")
                end_ip = ipaddress.ip_address(".".join([*octets[:3], str(end_octet)]))
            if int(end_ip) < int(start_ip):
                raise ValueError(f"IP 范围结束地址不能小于开始地址：{part}")
            if int(end_ip) - int(start_ip) > 1023:
                raise ValueError("一次最多拷取 1024 个主控 IP。")
            current = int(start_ip)
            while current <= int(end_ip):
                address = require_private_ipv4(str(ipaddress.ip_address(current)), "主控地址")
                hosts.append(address)
                current += 1
            continue
        hosts.append(require_private_ipv4(part, "主控地址"))
    deduped = list(dict.fromkeys(hosts))
    if not deduped:
        raise ValueError("请输入 IP 范围。")
    return deduped


def fan_from_master_ip(host: str) -> int:
    return int(ipaddress.ip_address(host).packed[-1])


def parse_master_targets(value: str, default_prefix: str = "192.168.151") -> list[tuple[str, int]]:
    """Accept full IP ranges or fan-number ranges for master-control tasks."""
    text = value.strip()
    if not text:
        raise ValueError("请输入主控 IP 或风机号，例如 192.168.151.1-20 或 1-20。")
    if "." in text or "/" in text:
        return [(host, fan_from_master_ip(host)) for host in parse_ip_range(text)]
    return build_fan_hosts(default_prefix, text, "主控 IP 前缀")


def compact_master_fan_input(value: str, default_prefix: str = "192.168.151") -> str:
    """Display same-prefix master IP selections as a compact fan-number list."""
    text = value.strip()
    if not text or "." not in text:
        return text
    prefix = default_prefix.strip().strip(".")
    fans: set[int] = set()
    try:
        for token in FAN_TOKEN_SPLIT.split(text):
            if not token:
                continue
            if "." in token or "/" in token:
                hosts = parse_ip_range(token)
                if any(host.rsplit(".", 1)[0] != prefix for host in hosts):
                    return text
                fans.update(fan_from_master_ip(host) for host in hosts)
            else:
                fans.update(parse_fan_spec(token))
    except ValueError:
        return text
    if not fans:
        return text
    ranges = []
    ordered_fans = sorted(fans)
    start = previous = ordered_fans[0]
    for fan in ordered_fans[1:]:
        if fan == previous + 1:
            previous = fan
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = fan
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def parse_date_value(value: str) -> date:
    cleaned = value.strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", cleaned):
        return datetime.strptime(cleaned, "%Y%m%d").date()
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式错误：{value}，请使用 YYYY-MM-DD。") from exc


def iter_dates(start: date, end: date, max_days: int = 366) -> list[date]:
    if end < start:
        raise ValueError("结束日期不能早于开始日期。")
    days = (end - start).days + 1
    if days > max_days:
        raise ValueError(f"一次最多拷取 {max_days} 天。")
    return [start + timedelta(days=offset) for offset in range(days)]


def normalize_keywords(value: str) -> list[str]:
    return [item.lower() for item in FAN_TOKEN_SPLIT.split(value.strip()) if item]


def classify_master_file(name: str) -> str | None:
    if not name:
        return None
    match = MASTER_TYPED_FILE.match(name)
    return match.group(1).upper() if match else None


def master_file_selected(
    name: str,
    selected_types: Iterable[str],
    keywords: Sequence[str] = (),
    whole_date_folder: bool = False,
) -> bool:
    if whole_date_folder:
        lowered = name.lower()
        return not keywords or any(keyword.lower() in lowered for keyword in keywords)
    allowed = {item.upper() for item in selected_types}
    category = classify_master_file(name)
    if category not in allowed:
        return False
    lowered = name.lower()
    return not keywords or any(keyword.lower() in lowered for keyword in keywords)


def master_file_matches_day(name: str, day: date) -> bool:
    """Match master log files that carry the date in the filename itself."""
    target8 = day.strftime("%Y%m%d")
    target6 = day.strftime("%y%m%d")
    return any(token in {target8, target6} for token in MASTER_DATE_TOKEN.findall(name))


def transient_file_fan(name: str, expected_date: str) -> int | None:
    info = transient_file_info(name, expected_date)
    return info[1] if info else None


def transient_file_info(name: str, expected_date: str) -> tuple[str, int] | None:
    match = TRANSIENT_FILE.match(name)
    if not match or match.group(2) != expected_date:
        return None
    equipment_id = match.group(1)
    if len(equipment_id) < 3:
        return None
    site_code = equipment_id[:-3] or "未识别场站"
    return site_code, int(equipment_id[-3:])


def pitch_file_matches(name: str, expected_date: str) -> bool:
    """Return whether a pitch trace log belongs to the requested YYYYMMDD date."""
    match = PITCH_FILE.match(name)
    return bool(match and match.group(1) == expected_date)


def vibration_remote_directory(
    base: str, data_type: str, site_code: str, day: date, fan: int
) -> str:
    """Build the CMSDATA path shown by the field transfer tool screenshots."""
    if data_type not in VIBRATION_TYPES:
        raise ValueError(f"不支持的震动数据类型：{data_type}")
    cleaned_base = "/" + base.strip().strip("/")
    site = site_code.strip()
    if not re.fullmatch(r"\d{3,20}", site):
        raise ValueError("风场号必须是数字，例如 650227。")
    if fan < 1 or fan > 999:
        raise ValueError("震动数据风机号必须在 1-999。")
    month = day.strftime("%Y-%m")
    date_text = day.strftime("%Y-%m-%d")
    equipment = f"{site}{fan:03d}"
    return posixpath.join(cleaned_base, data_type, site, month, equipment, date_text)


def pitch_output_directory(root: Path, host: str, day: date, fan: int) -> Path:
    return root / "pitch" / host / f"F{fan:03d}" / day.strftime("%Y%m%d")


def vibration_output_directory(
    root: Path, host: str, site_code: str, data_type: str, day: date, fan: int
) -> Path:
    return (
        root
        / "vibration"
        / host
        / site_code
        / f"F{fan:03d}"
        / day.strftime("%Y-%m-%d")
        / data_type
    )


def output_directory(
    root: Path, day: date, fan: int, source_type: str, category: str | None = None
) -> Path:
    base = root / day.strftime("%Y%m%d") / f"F{fan:03d}" / source_type
    return base / category if category else base


def transient_output_directory(
    root: Path,
    host: str,
    remote_directory: str,
    day: date,
    fan: int,
    layout: str,
) -> Path:
    """Organize by private server IP, then by the operator-selected lookup key."""
    base = root / "transient" / host
    if layout == "按IP/风机号/日期":
        return base / f"F{fan:03d}" / day.strftime("%Y%m%d")
    if layout == "保留服务器目录":
        return base.joinpath(*safe_remote_segments(remote_directory))
    return base / day.strftime("%Y%m%d")


def safe_remote_segments(remote_directory: str) -> list[str]:
    segments = [segment for segment in remote_directory.replace("\\", "/").split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("远程目录不能包含 . 或 .. 路径段。")
    return segments


def safe_windows_relative_segments(value: str) -> list[str]:
    segments = [segment for segment in value.replace("/", "\\").split("\\") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        raise ValueError("主控日志目录必须是 C$ 下的相对目录，不能包含 . 或 ..。")
    if any(":" in segment for segment in segments):
        raise ValueError("主控日志目录不能包含盘符。")
    return segments


def render_remote_directory(template: str, day: date) -> str:
    if not template.strip():
        raise ValueError("瞬态远程目录不能为空。")
    try:
        rendered = template.strip().format(
            year=day.strftime("%Y"), date=day.strftime("%Y%m%d")
        )
    except KeyError as exc:
        raise ValueError(f"远程目录包含未知占位符：{exc.args[0]}") from exc
    if "{date}" not in template:
        rendered = posix_join(rendered, day.strftime("%Y%m%d"))
    return "/" + rendered.strip("/")


def posix_join(left: str, right: str) -> str:
    return left.rstrip("/") + "/" + right.lstrip("/")


def master_output_directory(
    root: Path, host: str, day: date, fan: int, category: str
) -> Path:
    base = root / "master" / f"F{fan:03d}_{host}" / day.strftime("%Y%m%d")
    return base / category if category else base


def copy_local_atomic(
    source: Path,
    destination: Path,
    overwrite: bool,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
    chunk_size: int = 4 * 1024 * 1024,
    reset_partial: bool = True,
) -> CopyResult:
    source_stat = source.stat()
    size = source_stat.st_size
    source_id = str(source.resolve())
    modified = source_stat.st_mtime_ns
    if destination.exists() and not overwrite and destination.stat().st_size == size:
        return CopyResult(str(source), destination, size, "skipped")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary, metadata_path, offset = _prepare_partial(
        destination, source_id, size, modified, overwrite and reset_partial
    )
    with source.open("rb") as reader, temporary.open("ab" if offset else "wb") as writer:
        if offset:
            reader.seek(offset)
        remaining = size - offset
        while remaining:
            if cancel_check and cancel_check():
                raise CancelledError("用户已取消，已保留断点数据。")
            block = reader.read(min(chunk_size, remaining))
            if not block:
                break
            writer.write(block)
            remaining -= len(block)
            if progress:
                progress(len(block))
    if temporary.stat().st_size != size:
        raise CollectorError(f"源文件读取不完整，已保留断点：{source}")
    completed_stat = source.stat()
    if completed_stat.st_size != size or completed_stat.st_mtime_ns != modified:
        raise CollectorError(f"源文件在拷取期间发生变化，已保留断点：{source}")
    shutil.copystat(source, temporary)
    os.replace(temporary, destination)
    metadata_path.unlink(missing_ok=True)
    return CopyResult(str(source), destination, size, "copied", offset)


def copy_sftp_atomic(
    sftp,
    remote_path: str,
    destination: Path,
    overwrite: bool,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
    chunk_size: int = 4 * 1024 * 1024,
    reset_partial: bool = True,
) -> CopyResult:
    remote_stat = sftp.stat(remote_path)
    size = int(remote_stat.st_size)
    modified = int(remote_stat.st_mtime)
    if destination.exists() and not overwrite and destination.stat().st_size == size:
        return CopyResult(remote_path, destination, size, "skipped")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary, metadata_path, offset = _prepare_partial(
        destination, remote_path, size, modified, overwrite and reset_partial
    )
    with sftp.open(remote_path, "rb") as reader, temporary.open("ab" if offset else "wb") as writer:
        if offset:
            reader.seek(offset)
        remaining = size - offset
        while remaining:
            if cancel_check and cancel_check():
                raise CancelledError("用户已取消，已保留断点数据。")
            block = reader.read(min(chunk_size, remaining))
            if not block:
                break
            writer.write(block)
            remaining -= len(block)
            if progress:
                progress(len(block))
    if temporary.stat().st_size != size:
        raise CollectorError(f"远程文件读取不完整，已保留断点：{remote_path}")
    completed_stat = sftp.stat(remote_path)
    if int(completed_stat.st_size) != size or int(completed_stat.st_mtime) != modified:
        raise CollectorError(f"远程文件在下载期间发生变化，已保留断点：{remote_path}")
    os.replace(temporary, destination)
    metadata_path.unlink(missing_ok=True)
    return CopyResult(remote_path, destination, size, "copied", offset)


def copy_ftp_atomic(
    ftp,
    remote_path: str,
    destination: Path,
    overwrite: bool,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
    chunk_size: int = 4 * 1024 * 1024,
    reset_partial: bool = True,
) -> CopyResult:
    ftp.voidcmd("TYPE I")
    size = int(ftp.size(remote_path) or 0)
    modified = 0
    try:
        response = ftp.sendcmd(f"MDTM {remote_path}")
        modified = int(datetime.strptime(response.split()[-1][:14], "%Y%m%d%H%M%S").timestamp())
    except Exception:
        pass
    if destination.exists() and not overwrite and destination.stat().st_size == size:
        return CopyResult(remote_path, destination, size, "skipped")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary, metadata_path, offset = _prepare_partial(
        destination, remote_path, size, modified, overwrite and reset_partial
    )

    with temporary.open("ab" if offset else "wb") as writer:
        def write_block(block: bytes) -> None:
            if cancel_check and cancel_check():
                raise CancelledError("用户已取消，已保留断点数据。")
            writer.write(block)
            if progress:
                progress(len(block))

        try:
            ftp.retrbinary(f"RETR {remote_path}", write_block, blocksize=chunk_size, rest=offset or None)
        except CancelledError:
            raise
        except ftplib.error_perm as exc:
            if not offset or str(exc)[:3] not in {"500", "501", "502", "504"}:
                raise
            writer.seek(0)
            writer.truncate()
            offset = 0
            ftp.retrbinary(f"RETR {remote_path}", write_block, blocksize=chunk_size)

    if temporary.stat().st_size != size:
        raise CollectorError(f"FTP 文件读取不完整，已保留断点：{remote_path}")
    ftp.voidcmd("TYPE I")
    if int(ftp.size(remote_path) or 0) != size:
        raise CollectorError(f"FTP 文件在下载期间发生变化，已保留断点：{remote_path}")
    if modified:
        response = ftp.sendcmd(f"MDTM {remote_path}")
        completed_modified = int(datetime.strptime(response.split()[-1][:14], "%Y%m%d%H%M%S").timestamp())
        if completed_modified != modified:
            raise CollectorError(f"FTP 文件在下载期间发生变化，已保留断点：{remote_path}")
    os.replace(temporary, destination)
    metadata_path.unlink(missing_ok=True)
    return CopyResult(remote_path, destination, size, "copied", offset)


def load_json(path: Path, defaults: dict) -> dict:
    result = dict(defaults)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            result.update(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return result


def save_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


if os.name == "nt":
    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", ctypes.c_ulong),
            ("dwType", ctypes.c_ulong),
            ("dwDisplayType", ctypes.c_ulong),
            ("dwUsage", ctypes.c_ulong),
            ("lpLocalName", ctypes.c_wchar_p),
            ("lpRemoteName", ctypes.c_wchar_p),
            ("lpComment", ctypes.c_wchar_p),
            ("lpProvider", ctypes.c_wchar_p),
        ]


class SmbConnection(AbstractContextManager["SmbConnection"]):
    """Temporary authenticated SMB connection without exposing passwords on a CLI."""

    RESOURCE_GLOBALNET = 2
    RESOURCETYPE_DISK = 1
    CONNECT_TEMPORARY = 4
    ERROR_SESSION_CREDENTIAL_CONFLICT = 1219

    def __init__(self, share: str, username: str, password: str):
        if os.name != "nt":
            raise CollectorError("主控网络共享仅支持 Windows。")
        self.share = share.rstrip("\\")
        self.username = username
        self.password = password
        self.connected_here = False
        self.effective_username = username

    def __enter__(self) -> "SmbConnection":
        if os.path.isdir(self.share):
            return self

        resource = NETRESOURCEW()
        resource.dwScope = self.RESOURCE_GLOBALNET
        resource.dwType = self.RESOURCETYPE_DISK
        resource.lpRemoteName = self.share

        mpr = ctypes.WinDLL("mpr")
        add_connection = mpr.WNetAddConnection2W
        add_connection.argtypes = [
            ctypes.POINTER(NETRESOURCEW),
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
        ]
        add_connection.restype = ctypes.c_ulong
        candidates = [self.username or ""]
        if self.username and "\\" not in self.username and "@" not in self.username:
            host_name = self.share.split("\\")[2]
            candidates.extend([f".\\{self.username}", f"{host_name}\\{self.username}"])
        result = 1
        for candidate in dict.fromkeys(candidates):
            result = add_connection(
                ctypes.byref(resource),
                self.password or None,
                candidate or None,
                self.CONNECT_TEMPORARY,
            )
            if result == 0:
                self.effective_username = candidate
                break
            if result == self.ERROR_SESSION_CREDENTIAL_CONFLICT:
                raise CollectorError(
                    "Windows 已使用其他账号连接这台主控。请先关闭该主控的资源管理器窗口并断开旧连接。"
                )
        if result != 0:
            message = ctypes.FormatError(result).strip()
            raise CollectorError(
                f"连接 {self.share} 失败（{result}）：{message}；"
                "已尝试当前账号、.\\账号和主控名\\账号三种 Windows 登录格式。"
            )
        self.connected_here = True
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.connected_here:
            cancel = ctypes.WinDLL("mpr").WNetCancelConnection2W
            cancel.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_bool]
            cancel.restype = ctypes.c_ulong
            cancel(self.share, 0, False)


class SystemAwake(AbstractContextManager["SystemAwake"]):
    """Prevent automatic system sleep while a transfer worker is active."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self) -> "SystemAwake":
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(
                self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED
            )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)


def require_transfer_space(destination: Path, remaining_bytes: int) -> int:
    free = shutil.disk_usage(destination).free
    reserve = 2 * 1024 * 1024 * 1024
    if remaining_bytes + reserve > free:
        raise InsufficientSpaceError(
            f"目标盘空间不足：还需传输 {human_size(remaining_bytes)}，"
            f"当前可用 {human_size(free)}，并需保留 {human_size(reserve)} 安全空间。"
        )
    return free


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
