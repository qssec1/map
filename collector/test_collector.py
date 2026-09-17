#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import WindCollectorApp
from collector_core import (
    CancelledError,
    InsufficientSpaceError,
    NETWORK_PRESETS,
    VIBRATION_TYPES,
    build_fan_hosts,
    classify_master_file,
    compact_master_fan_input,
    copy_local_atomic,
    iter_dates,
    master_file_selected,
    master_file_matches_day,
    master_output_directory,
    normalize_keywords,
    parse_date_value,
    parse_fan_spec,
    parse_ip_range,
    parse_master_targets,
    fan_from_master_ip,
    pitch_file_matches,
    pitch_output_directory,
    require_private_ipv4,
    require_transfer_space,
    safe_windows_relative_segments,
    transient_file_fan,
    transient_file_info,
    transient_output_directory,
    validate_network_address,
    vibration_output_directory,
    vibration_remote_directory,
)


class ParseTests(unittest.TestCase):
    def test_master_saved_ip_range_is_compacted_for_display(self):
        self.assertEqual(
            compact_master_fan_input("192.168.151.1-3,192.168.151.8"),
            "1-3,8",
        )
        self.assertEqual(
            compact_master_fan_input("192.168.152.1-3"),
            "192.168.152.1-3",
        )
        self.assertEqual(
            compact_master_fan_input("192.168.151.1，12"),
            "1,12",
        )

    def test_fan_ranges_and_lists(self):
        self.assertEqual(parse_fan_spec("1-3, 8，10 至 11"), [1, 2, 3, 8, 10, 11])

    def test_separate_prefix_and_multiple_fans_build_expected_hosts(self):
        self.assertEqual(
            build_fan_hosts("192.168.151", "1,12", "主控 IP 前缀"),
            [("192.168.151.1", 1), ("192.168.151.12", 12)],
        )
        self.assertEqual(
            build_fan_hosts("192.168.180.", "1-3", "变桨 IP 前缀"),
            [
                ("192.168.180.1", 1),
                ("192.168.180.2", 2),
                ("192.168.180.3", 3),
            ],
        )

    def test_fan_field_rejects_full_ip_and_prefix_rejects_fan_octet(self):
        with self.assertRaisesRegex(ValueError, "无法识别风机号"):
            build_fan_hosts("192.168.151", "192.168.151.1,12", "主控 IP 前缀")
        with self.assertRaisesRegex(ValueError, "必须是三段"):
            build_fan_hosts("192.168.151.1", "12", "主控 IP 前缀")

    def test_reverse_range_and_duplicates(self):
        self.assertEqual(parse_fan_spec("3-1,2"), [1, 2, 3])

    def test_invalid_fan(self):
        with self.assertRaises(ValueError):
            parse_fan_spec("0,255")

    def test_date_range(self):
        start = parse_date_value("2026-08-17")
        end = parse_date_value("20260819")
        self.assertEqual(
            iter_dates(start, end),
            [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)],
        )

    def test_full_leap_year_is_allowed_but_more_is_rejected(self):
        days = iter_dates(date(2024, 1, 1), date(2024, 12, 31))
        self.assertEqual(len(days), 366)
        with self.assertRaises(ValueError):
            iter_dates(date(2024, 1, 1), date(2025, 1, 1))

    def test_only_private_numeric_ipv4_is_allowed(self):
        self.assertEqual(require_private_ipv4("192.168.149.222"), "192.168.149.222")
        self.assertEqual(require_private_ipv4("10.20.30.40"), "10.20.30.40")
        with self.assertRaises(ValueError):
            require_private_ipv4("example.com")
        with self.assertRaises(ValueError):
            require_private_ipv4("8.8.8.8")

    def test_network_address_and_mask_validation(self):
        self.assertEqual(
            validate_network_address("192.168.151.201", "255.255.248.0"),
            ("192.168.151.201", "255.255.248.0", 21),
        )
        with self.assertRaises(ValueError):
            validate_network_address("192.168.151.201", "255.0.255.0")
        with self.assertRaises(ValueError):
            validate_network_address("8.8.8.8", "255.255.255.0")

    def test_network_presets_match_screenshot_order(self):
        self.assertEqual(
            list(NETWORK_PRESETS),
            ["风机IP", "变桨IP", "净空IP", "雷达IP", "变流IP", "交换机IP", "消防IP", "振动IP"],
        )

    def test_pitch_file_date_filter_and_output_layout(self):
        self.assertTrue(pitch_file_matches("PT20260816_103222.897_60064.txt", "20260816"))
        self.assertFalse(pitch_file_matches("PT20260816_103222.897_60064.txt", "20260817"))
        self.assertEqual(
            pitch_output_directory(Path("E:/out"), "192.168.180.17", date(2026, 8, 16), 17),
            Path("E:/out/pitch/192.168.180.17/F017/20260816"),
        )

    def test_vibration_sibling_directories_and_layout(self):
        self.assertEqual(
            vibration_remote_directory(
                "/data/GwTempData/CMSDATA/", "BMSDATA", "650227", date(2026, 3, 31), 1
            ),
            "/data/GwTempData/CMSDATA/BMSDATA/650227/2026-03/650227001/2026-03-31",
        )
        self.assertEqual(
            vibration_remote_directory(
                "/data/GwTempData/CMSDATA/", "WAVEDATA", "650227", date(2026, 3, 31), 1
            ).split("/")[-5],
            "WAVEDATA",
        )
        self.assertEqual(
            vibration_output_directory(
                Path("E:/out"), "192.168.160.100", "650227", "TMSDATA", date(2026, 3, 31), 1
            ),
            Path("E:/out/vibration/192.168.160.100/650227/F001/2026-03-31/TMSDATA"),
        )
        self.assertEqual(tuple(VIBRATION_TYPES), ("BMSDATA", "TMSDATA", "WAVEDATA"))
        for name, values in NETWORK_PRESETS.items():
            self.assertLessEqual(len(values), 1, name)

    def test_master_log_path_must_be_relative(self):
        self.assertEqual(safe_windows_relative_segments(r"FTP\LOG"), ["FTP", "LOG"])
        with self.assertRaises(ValueError):
            safe_windows_relative_segments(r"C:\FTP\LOG")
        with self.assertRaises(ValueError):
            safe_windows_relative_segments(r"..\LOG")

    def test_master_ip_range_matches_fenglian_style(self):
        self.assertEqual(
            parse_ip_range("192.168.151.1-3"),
            ["192.168.151.1", "192.168.151.2", "192.168.151.3"],
        )
        self.assertEqual(
            parse_ip_range("192.168.151.1-192.168.151.2,192.168.151.2"),
            ["192.168.151.1", "192.168.151.2"],
        )
        self.assertEqual(fan_from_master_ip("192.168.151.13"), 13)
        self.assertEqual(
            parse_master_targets("1-2,4", "192.168.151"),
            [("192.168.151.1", 1), ("192.168.151.2", 2), ("192.168.151.4", 4)],
        )
        with self.assertRaises(ValueError):
            parse_ip_range("8.8.8.8")


class SelectionTests(unittest.TestCase):
    def test_transient_fan_is_last_three_digits(self):
        self.assertEqual(transient_file_fan("real_650227001_20260818.arc", "20260818"), 1)
        self.assertEqual(transient_file_fan("real_650227020_20260818.arc", "20260818"), 20)
        self.assertEqual(transient_file_fan("real_650227020_20260818.zip", "20260818"), 20)
        self.assertEqual(transient_file_fan("650227011-20260818.dat", "20260818"), 11)
        self.assertEqual(transient_file_fan("real_650227012_20260818", "20260818"), 12)
        self.assertIsNone(transient_file_fan("real_650227020_20260817.arc", "20260818"))
        self.assertIsNone(transient_file_fan("O20260818.csv", "20260818"))
        self.assertEqual(
            transient_file_info("real_650227011_20260818.arc", "20260818"),
            ("650227", 11),
        )

    def test_master_type_and_keyword_filter(self):
        self.assertEqual(classify_master_file("B260817_2034_BladeLoad.txt"), "B")
        self.assertEqual(classify_master_file("f20260817_other.html"), "F")
        self.assertIsNone(classify_master_file("Other_20260817.txt"))
        self.assertIsNone(classify_master_file("readme.txt"))
        keywords = normalize_keywords("VibrWarn, BladeLoad")
        self.assertTrue(master_file_selected("B260817_BladeLoad.txt", ["B"], keywords))
        self.assertFalse(master_file_selected("B260817_RegularData.txt", ["B"], keywords))
        self.assertFalse(master_file_selected("F20260817_VibrWarn.html", ["B"], keywords))

    def test_master_all_bfo_still_filters_to_bfo(self):
        self.assertFalse(master_file_selected("RegularData_20260817.txt", ["B", "F", "O"]))
        self.assertFalse(master_file_selected("readme_20260817.txt", ["B", "F", "O"]))

    def test_master_whole_folder_keeps_all_files(self):
        self.assertTrue(
            master_file_selected("RegularData_20260817.txt", ["B", "F", "O"], whole_date_folder=True)
        )
        self.assertTrue(
            master_file_selected("readme_20260817.txt", [], whole_date_folder=True)
        )
        self.assertFalse(
            master_file_selected(
                "readme_20260817.txt", ["B", "F", "O"], ["BladeLoad"], whole_date_folder=True
            )
        )

    def test_master_file_date_matches_short_or_full_date(self):
        self.assertTrue(master_file_matches_day("B260817_BladeLoad.txt", date(2026, 8, 17)))
        self.assertTrue(master_file_matches_day("O20260817.csv", date(2026, 8, 17)))
        self.assertFalse(master_file_matches_day("F20260818_VibrWarn.html", date(2026, 8, 17)))


class OutputTests(unittest.TestCase):
    @staticmethod
    def _parallel_test_app():
        app = object.__new__(WindCollectorApp)
        app.cancel_event = SimpleNamespace(is_set=lambda: False)
        app._emit = lambda *args: None
        return app

    def test_master_parallel_failure_retries_only_failed_fan_serially(self):
        app = self._parallel_test_app()
        calls = {}

        def run_target(_host, fan, _options):
            calls[fan] = calls.get(fan, 0) + 1
            if fan == 2 and calls[fan] == 1:
                return 0, 0, 0, 1, 0
            return 1, 0, 0, 0, 10

        app._run_master_target = run_target
        summary = app._run_master(
            {
                "test_only": False,
                "targets": [("192.168.151.1", 1), ("192.168.151.2", 2)],
                "max_workers": 2,
            }
        )
        self.assertEqual(calls, {1: 1, 2: 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 0", summary)

    def test_master_serial_fallback_exception_does_not_stop_next_fan(self):
        app = self._parallel_test_app()
        calls = {}

        def run_target(_host, fan, _options):
            calls[fan] = calls.get(fan, 0) + 1
            if fan in (2, 3) and calls[fan] == 1:
                return 0, 0, 0, 1, 0
            if fan == 2:
                raise OSError("still offline")
            return 1, 0, 0, 0, 10

        app._run_master_target = run_target
        summary = app._run_master(
            {
                "test_only": False,
                "targets": [
                    ("192.168.151.1", 1),
                    ("192.168.151.2", 2),
                    ("192.168.151.3", 3),
                ],
                "max_workers": 3,
            }
        )
        self.assertEqual(calls, {1: 1, 2: 2, 3: 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 1", summary)

    def test_transient_parallel_failure_falls_back_to_serial_retry(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            fan = options["fans"][0]
            calls[fan] = calls.get(fan, 0) + 1
            if fan == 2 and options.get("_suppress_progress"):
                raise OSError("parallel refused")
            return 1, 0, 0, 10

        app._run_transient = run_child
        summary = WindCollectorApp._run_transient(
            app,
            {
                "test_only": False,
                "local_root": "",
                "fans": [1, 2],
                "days": [date(2026, 9, 17)],
            },
        )
        self.assertEqual(calls, {1: 1, 2: 2})
        self.assertIn("拷取 2", summary)

    def test_transient_serial_fallback_exception_continues_next_unit(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            fan = options["fans"][0]
            calls[fan] = calls.get(fan, 0) + 1
            if fan in (2, 3) and options.get("_suppress_progress"):
                raise OSError("parallel refused")
            if fan == 2:
                raise OSError("still offline")
            return 1, 0, 0, 10

        app._run_transient = run_child
        summary = WindCollectorApp._run_transient(
            app,
            {
                "test_only": False,
                "local_root": "",
                "fans": [1, 2, 3],
                "days": [date(2026, 9, 17)],
            },
        )
        self.assertEqual(calls, {1: 1, 2: 2, 3: 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("失败 1", summary)

    def test_pitch_parallel_offline_fan_falls_back_to_serial_retry(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            fan = options["fans"][0]
            calls[fan] = calls.get(fan, 0) + 1
            if fan == 2 and options.get("_suppress_progress"):
                return 0, 0, 0, 1, 0
            return 1, 0, 0, 0, 10

        app._run_pitch = run_child
        summary = WindCollectorApp._run_pitch(
            app, {"test_only": False, "fans": [1, 2]}
        )
        self.assertEqual(calls, {1: 1, 2: 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 0", summary)

    def test_pitch_serial_fallback_exception_continues_next_fan(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            fan = options["fans"][0]
            calls[fan] = calls.get(fan, 0) + 1
            if fan in (2, 3) and options.get("_suppress_progress"):
                return 0, 0, 0, 1, 0
            if fan == 2:
                raise OSError("still offline")
            return 1, 0, 0, 0, 10

        app._run_pitch = run_child
        summary = WindCollectorApp._run_pitch(
            app, {"test_only": False, "fans": [1, 2, 3]}
        )
        self.assertEqual(calls, {1: 1, 2: 2, 3: 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 1", summary)

    def test_vibration_parallel_offline_unit_falls_back_to_serial_retry(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            unit = (options["fans"][0], options["selected_types"][0])
            calls[unit] = calls.get(unit, 0) + 1
            if unit == (2, "BMSDATA") and options.get("_suppress_progress"):
                return 0, 0, 0, 1, 0
            return 1, 0, 0, 0, 10

        app._run_vibration = run_child
        summary = WindCollectorApp._run_vibration(
            app,
            {
                "test_only": False,
                "local_root": "",
                "fans": [1, 2],
                "selected_types": ["BMSDATA"],
            },
        )
        self.assertEqual(calls, {(1, "BMSDATA"): 1, (2, "BMSDATA"): 2})
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 0", summary)

    def test_vibration_serial_fallback_exception_continues_next_unit(self):
        app = self._parallel_test_app()
        calls = {}

        def run_child(options):
            unit = (options["fans"][0], options["selected_types"][0])
            calls[unit] = calls.get(unit, 0) + 1
            if unit[0] in (2, 3) and options.get("_suppress_progress"):
                return 0, 0, 0, 1, 0
            if unit[0] == 2:
                raise OSError("still offline")
            return 1, 0, 0, 0, 10

        app._run_vibration = run_child
        summary = WindCollectorApp._run_vibration(
            app,
            {
                "test_only": False,
                "local_root": "",
                "fans": [1, 2, 3],
                "selected_types": ["BMSDATA"],
            },
        )
        self.assertEqual(
            calls,
            {(1, "BMSDATA"): 1, (2, "BMSDATA"): 2, (3, "BMSDATA"): 2},
        )
        self.assertIn("拷取 2", summary)
        self.assertIn("连接失败 1", summary)

    def test_sftp_initial_connection_retries_three_times(self):
        app = object.__new__(WindCollectorApp)
        app.cancel_event = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False)
        app._emit = lambda *args: None
        attempts = []

        def connect(_options):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("temporary refusal")
            return "client", "sftp"

        app._open_sftp_session = connect
        self.assertEqual(
            app._open_sftp_session_with_retry({}, "测试"),
            ("client", "sftp"),
        )
        self.assertEqual(len(attempts), 3)

    def test_master_automatically_falls_back_across_available_sources(self):
        app = object.__new__(WindCollectorApp)
        calls = []
        app._collect_master_via_local_folder = lambda *_args: calls.append("shared")
        app._collect_master_via_ftp = lambda *_args: calls.append("ftp")
        app._collect_master_via_smb = lambda *_args: calls.append("system_share")
        options = {
            "local_root": r"Z:\master\{fan3}",
            "username": "operator",
            "password": "test-only",
        }

        attempts = app._master_connection_attempts("192.168.151.7", 7, options)
        for attempt in attempts:
            attempt()

        self.assertEqual(calls, ["shared", "ftp", "system_share"])

    def test_transient_layout_keeps_server_hierarchy(self):
        path = transient_output_directory(
            Path("D:/WindData"),
            "192.168.149.222",
            "/opt/goldwind/drbd_dataprocess/LocalData/HistoryFile/2026/0/RealtimeData/20260818",
            date(2026, 8, 18),
            1,
            "按IP/日期",
        )
        self.assertEqual(
            path,
            Path("D:/WindData/transient/192.168.149.222/20260818"),
        )

    def test_transient_fan_layout(self):
        path = transient_output_directory(
            Path("D:/WindData"),
            "192.168.149.222",
            "/server/20260818",
            date(2026, 8, 18),
            11,
            "按IP/风机号/日期",
        )
        self.assertEqual(
            path,
            Path("D:/WindData/transient/192.168.149.222/F011/20260818"),
        )

    def test_transient_network_location_accepts_non_arc_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source" / "20260905"
            source_root.mkdir(parents=True)
            source = source_root / "real_650227003_20260905.zip"
            source.write_bytes(b"transient")
            out = root / "out"
            app = object.__new__(WindCollectorApp)
            app.cancel_event = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False)
            app._emit = lambda *args: None
            app._progress_tracker = lambda total, completed=0: ({}, lambda size: None, lambda: None)
            options = {
                "destination": out,
                "days": [date(2026, 9, 5)],
                "fans": [3],
                "host": "192.168.149.222",
                "local_root": str(root / "source" / "{date}"),
                "layout": "按IP/风机号/日期",
                "overwrite": False,
                "test_only": False,
            }
            summary = app._run_transient_from_local_source(options)
            target = out / "transient" / "192.168.149.222" / "F003" / "20260905" / source.name
            self.assertTrue(target.exists())
            self.assertIn("拷取 1", summary)

    def test_transient_network_location_per_fan_folder_copies_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source" / "003" / "20260905"
            source_root.mkdir(parents=True)
            source = source_root / "unknown-format-file"
            source.write_bytes(b"transient")
            out = root / "out"
            app = object.__new__(WindCollectorApp)
            app.cancel_event = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False)
            app._emit = lambda *args: None
            app._progress_tracker = lambda total, completed=0: ({}, lambda size: None, lambda: None)
            options = {
                "destination": out,
                "days": [date(2026, 9, 5)],
                "fans": [3],
                "host": "192.168.149.222",
                "local_root": str(root / "source" / "{fan3}" / "{date}"),
                "layout": "按IP/风机号/日期",
                "overwrite": False,
                "test_only": False,
            }
            summary = app._run_transient_from_local_source(options)
            target = out / "transient" / "192.168.149.222" / "F003" / "20260905" / source.name
            self.assertTrue(target.exists())
            self.assertIn("拷取 1", summary)

    def test_master_layout_contains_fan_ip_date_and_type(self):
        path = master_output_directory(
            Path("D:/WindData"), "192.168.151.13", date(2026, 8, 17), 13, "O"
        )
        self.assertEqual(
            path,
            Path("D:/WindData/master/F013_192.168.151.13/20260817/O"),
        )
        whole_path = master_output_directory(
            Path("D:/WindData"), "192.168.151.13", date(2026, 8, 17), 13, ""
        )
        self.assertEqual(whole_path, Path("D:/WindData/master/F013_192.168.151.13/20260817"))

    def test_master_network_location_whole_folder_is_recursive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day_dir = root / "20260905"
            nested = day_dir / "child"
            nested.mkdir(parents=True)
            (day_dir / "B260905_fault.txt").write_text("b", encoding="utf-8")
            (nested / "Other_20260905.txt").write_text("other", encoding="utf-8")
            app = object.__new__(WindCollectorApp)
            app._emit = lambda *args: None
            options = {
                "selected_types": [],
                "keywords": (),
                "whole_folder": True,
            }
            source_dir, files = app._locate_master_local_sources(root, date(2026, 9, 5), options)
            self.assertEqual(source_dir, day_dir)
            self.assertEqual(
                sorted(relative.as_posix() for _, relative in files),
                ["B260905_fault.txt", "child/Other_20260905.txt"],
            )

    def test_vibration_network_location_uses_cmsdata_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "BMSDATA" / "650227" / "2026-09" / "650227003" / "2026-09-05"
            source_dir.mkdir(parents=True)
            source = source_dir / "sample.bin"
            source.write_bytes(b"cms")
            out = root / "out"
            app = object.__new__(WindCollectorApp)
            app.cancel_event = SimpleNamespace(is_set=lambda: False)
            app._emit = lambda *args: None
            app._progress_tracker = lambda total, completed=0: ({}, lambda size: None, lambda: None)
            options = {
                "destination": out,
                "days": [date(2026, 9, 5)],
                "fans": [3],
                "host": "192.168.160.100",
                "local_root": str(root),
                "site": "650227",
                "selected_types": ["BMSDATA"],
                "overwrite": False,
                "test_only": False,
            }
            summary = app._run_vibration_from_local_source(options)
            target = out / "vibration" / "192.168.160.100" / "650227" / "F003" / "2026-09-05" / "BMSDATA" / "sample.bin"
            self.assertTrue(target.exists())
            self.assertIn("拷取 1", summary)

    def test_atomic_copy_and_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            destination = root / "nested" / "target.bin"
            source.write_bytes(b"wind-data")
            first = copy_local_atomic(source, destination, overwrite=False)
            second = copy_local_atomic(source, destination, overwrite=False)
            self.assertEqual(first.status, "copied")
            self.assertEqual(second.status, "skipped")
            self.assertEqual(destination.read_bytes(), b"wind-data")
            self.assertFalse(destination.with_name("target.bin.part").exists())

    def test_wrong_sized_existing_file_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            destination = root / "target.bin"
            source.write_bytes(b"complete-source-data")
            destination.write_bytes(b"short")
            result = copy_local_atomic(source, destination, overwrite=False, chunk_size=4)
            self.assertEqual(result.status, "copied")
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_chunked_copy_handles_large_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "large.arc"
            destination = root / "out" / "large.arc"
            expected_size = 8 * 1024 * 1024 + 123
            source.write_bytes(b"x" * expected_size)
            progressed = 0

            def record(size):
                nonlocal progressed
                progressed += size

            result = copy_local_atomic(
                source, destination, overwrite=False, progress=record, chunk_size=64 * 1024
            )
            self.assertEqual(result.status, "copied")
            self.assertEqual(progressed, expected_size)
            self.assertEqual(destination.stat().st_size, expected_size)

    def test_cancel_preserves_and_resumes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            destination = root / "target.bin"
            source.write_bytes(b"x" * 128)
            transferred = 0

            def progress(size):
                nonlocal transferred
                transferred += size

            with self.assertRaises(CancelledError):
                copy_local_atomic(
                    source,
                    destination,
                    False,
                    cancel_check=lambda: transferred >= 32,
                    progress=progress,
                    chunk_size=32,
                )
            self.assertFalse(destination.exists())
            self.assertEqual(destination.with_name("target.bin.part").stat().st_size, 32)
            result = copy_local_atomic(source, destination, False, chunk_size=32)
            self.assertEqual(result.resumed_from, 32)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse(destination.with_name("target.bin.part").exists())
            self.assertFalse(destination.with_name("target.bin.part.json").exists())

    def test_overwrite_retry_keeps_current_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            destination = root / "target.bin"
            source.write_bytes(b"new-data" * 32)
            destination.write_bytes(b"old-complete-file")
            transferred = 0

            def progress(size):
                nonlocal transferred
                transferred += size

            with self.assertRaises(CancelledError):
                copy_local_atomic(
                    source,
                    destination,
                    overwrite=True,
                    cancel_check=lambda: transferred >= 64,
                    progress=progress,
                    chunk_size=64,
                )
            self.assertEqual(destination.read_bytes(), b"old-complete-file")
            result = copy_local_atomic(
                source,
                destination,
                overwrite=True,
                chunk_size=64,
                reset_partial=False,
            )
            self.assertEqual(result.resumed_from, 64)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_disk_space_reserves_two_gib(self):
        free = 3 * 1024 * 1024 * 1024
        with patch("collector_core.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
            self.assertEqual(require_transfer_space(Path("D:/data"), 1024**3), free)
            with self.assertRaises(InsufficientSpaceError):
                require_transfer_space(Path("D:/data"), 2 * 1024**3)


if __name__ == "__main__":
    unittest.main()
