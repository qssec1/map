import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import MapFanSim as app


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        dirs = {key: root / key for key in app.DIRS}
        for directory in dirs.values():
            directory.mkdir(parents=True, exist_ok=True)
        for key in ("rules", "input_maps"):
            shutil.copytree(app.DIRS[key], dirs[key], dirs_exist_ok=True)
        for name, value in (("ROOT", root), ("DIRS", dirs),
                            ("CONFIG_PATH", dirs["data"] / "config.json"),
                            ("DEVICE_MAPS_PATH", dirs["data"] / "device_maps.csv"),
                            ("RELATIONS_PATH", dirs["data"] / "relations.csv"),
                            ("EXTRA_RULES_PATH", dirs["data"] / "extra_rules.txt"),
                            ("CURRENT_FARM_PATH", dirs["data"] / "current_wind_farm.txt")):
            ctx = patch.object(app, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)

    def test_main_pages_have_visible_save_and_restore_controls(self):
        window = app.App()
        self.addCleanup(window.destroy)
        window.geometry("1180x760")
        window.update()
        for page, expected in (("settings", "保存"), ("cloud_replace", "全场")):
            window.show_page(page)
            window.update()
            pending = list(window.pages[page].winfo_children())
            found = []
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if widget.winfo_class() in ("Button", "TButton") and expected in str(widget.cget("text")):
                    found.append(widget)
            self.assertTrue(found, page)
            for widget in found:
                self.assertTrue(widget.winfo_ismapped(), (page, widget.cget("text"), widget.winfo_geometry(), widget.master.winfo_geometry()))
                self.assertGreater(widget.winfo_height(), 10)
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                     window.winfo_rooty() + window.winfo_height())

    def test_each_farm_replaces_only_requested_column_and_keeps_sources(self):
        farms = [p.name for p in app.DIRS["rules"].iterdir()
                 if list((app.DIRS["input_maps"] / p.name).glob("*.map"))]
        self.assertEqual(len(farms), 4)
        outputs = []
        for farm in farms:
            with self.subTest(farm=farm):
                app.set_current_wind_farm(farm)
                source = next(app.farm_runtime_dir("input_maps").glob("*.map"))
                original = source.read_bytes()
                maps = app.load_legacy_device_maps()
                fans = list(maps)[:2]
                profile = app.load_rule_profile()
                lines, _ = app._read_map_csv_lines(source)
                index = app._build_map_index_by_profile(lines, profile)
                column = int(profile["mapParser"]["replaceColumnIndex"])
                expected = [app._split_line_by_profile(line, profile) for line in lines]
                matches = 0
                for dst, src in zip(maps[fans[0]], maps[fans[1]]):
                    dk, sk = app._key_for_address(dst, profile), app._key_for_address(src, profile)
                    if dk in index and sk in index:
                        expected[index[dk]][column] = app._split_line_by_profile(lines[index[sk]], profile)[column]
                        matches += 1
                self.assertGreater(matches, 0)
                output, report, _ = app.run_local_simulation(
                    app.Config(remoteFile=source.name), [app.Relation(enabled=True, local_fan=fans[0], target_fan=fans[1])],
                    source, None, "", lambda _: None)
                actual, _ = app._read_map_csv_lines(output)
                self.assertEqual([app._split_line_by_profile(line, profile) for line in actual], expected)
                self.assertEqual(source.read_bytes(), original)
                self.assertTrue(report.is_file())
                self.assertIn(farm, str(output))
                outputs.append(output)
        self.assertEqual(len(set(outputs)), 4)

    def test_cross_farm_target_rejected_before_output(self):
        farms = [p for p in app.DIRS["input_maps"].iterdir() if p.is_dir() and list(p.glob("*.map"))]
        app.set_current_wind_farm(farms[0].name)
        source = next(farms[0].glob("*.map"))
        foreign = next(farms[1].glob("*.map"))
        with self.assertRaisesRegex(RuntimeError, "不在当前风场"):
            app.run_local_simulation(app.Config(), [], source, foreign, "", lambda _: None)
        self.assertEqual(list(app.farm_runtime_dir("output_maps").glob("*.map")), [])

    def test_full_cancel_uploads_original_source_not_download_or_backup(self):
        farm = next(p for p in app.DIRS["rules"].iterdir()
                    if list((app.DIRS["input_maps"] / p.name).glob("*.map")))
        app.set_current_wind_farm(farm.name)
        source = next(app.farm_runtime_dir("input_maps").glob("*.map"))
        download = app.farm_runtime_path("download", source.name)
        download.write_bytes(b"already simulated")
        state = SimpleNamespace(cfg=app.Config(remoteFile=source.name),
                                local_map_var=SimpleNamespace(get=lambda: str(download)),
                                save_settings_no_popup=lambda: None, log=lambda _: None,
                                run_bg=lambda title, work: work())
        state._source_map_for_full_cancel = lambda: app.App._source_map_for_full_cancel(state)
        with patch.object(app, "RemoteClient") as remote:
            app.App.task_cancel_full_farm_simulation(state)
            remote.return_value.upload.assert_called_once_with(source.resolve())

    def test_restore_preserves_backup_mtime_and_rejects_foreign_marker(self):
        import json
        import os
        app.set_current_wind_farm(app.DEFAULT_WIND_FARM)
        backup = app.farm_runtime_path("backup", "original.map")
        backup.write_bytes(b"original")
        os.utime(backup, (1600000000, 1600000000))
        marker = app.farm_runtime_path("backup", "last_backup.json")
        marker.write_text(json.dumps({"backup_file": str(backup)}), encoding="utf-8")
        state = SimpleNamespace(cfg=app.Config(), save_settings_no_popup=lambda: None,
                                log=lambda _: None, run_bg=lambda title, work: work())
        with patch.object(app, "RemoteClient") as remote:
            app.App.task_restore_backup(state)
            restored = remote.return_value.upload.call_args.args[0]
            self.assertEqual(restored.stat().st_mtime, 1600000000)
            marker.write_text(json.dumps({"backup_file": str(backup), "host": "192.168.1.99"}), encoding="utf-8")
            remote.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "服务器"):
                app.App.task_restore_backup(state)
            remote.assert_not_called()


class BackgroundUiTests(unittest.TestCase):
    def test_remote_client_rejects_internet_and_dns_before_connecting(self):
        for host in ("8.8.8.8", "example.com", "127.0.0.1", "::1"):
            with self.subTest(host=host), self.assertRaises(RuntimeError):
                app.RemoteClient(app.Config(host=host), lambda _: None)
        app.RemoteClient(app.Config(host="192.168.149.222"), lambda _: None)

    def test_busy_guard_and_deferred_error_restore_controls(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        from tkinter import ttk
        combo = ttk.Combobox(root, state="readonly")
        entry = ttk.Entry(root)
        callbacks = []
        state = SimpleNamespace(winfo_children=root.winfo_children,
                                log_text_widgets=[], log=lambda _: None,
                                after=lambda delay, callback: callbacks.append(callback))

        def work():
            self.assertEqual(str(combo.cget("state")), "disabled")
            app.App.run_bg(state, "duplicate", lambda: self.fail("Overlapping task"))
            raise RuntimeError("original error")

        with patch.object(app.threading, "Thread") as thread, patch.object(app, "messagebox") as dialogs:
            thread.side_effect = lambda target, **kw: SimpleNamespace(start=target)
            app.App.run_bg(state, "test", work)
            self.assertTrue(state._task_running)
            callbacks.pop()()
            self.assertFalse(state._task_running)
            self.assertEqual(str(combo.cget("state")), "readonly")
            self.assertEqual(str(entry.cget("state")), "normal")
            self.assertIn("original error", dialogs.showerror.call_args.args[1])
            dialogs.showwarning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
