"""Keep updater imports private within NVDA's shared globalPlugins namespace."""

import ast
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = "samsungGalaxyVoices"


class UpdaterIsolationTests(unittest.TestCase):
	def test_both_real_updaters_use_their_own_feed_in_either_load_order(self):
		roots = {ADDON_ID: ROOT, "samsungTVVoices": ROOT.parent / "samsungTVVoices"}
		if not (roots["samsungTVVoices"] / "addon/globalPlugins/samsungTVVoices.py").exists():
			self.skipTest("The companion TV source is required for the shared-namespace test")
		for order in (tuple(roots), tuple(reversed(roots))):
			with self.subTest(order=order):
				package = types.ModuleType("globalPlugins")
				package.__path__ = [str(roots[name] / "addon/globalPlugins") for name in order]
				modules = {
					"globalPlugins": package,
					"addonHandler": types.SimpleNamespace(initTranslation=lambda: None,
						getAvailableAddons=lambda: [types.SimpleNamespace(name=name,
							manifest={"version": "1.0.0"}) for name in roots]),
					"core": types.ModuleType("core"), "gui": types.ModuleType("gui"),
					"synthDriverHandler": types.ModuleType("synthDriverHandler"),
					"logHandler": types.SimpleNamespace(log=mock.Mock()),
					"systemUtils": types.SimpleNamespace(ExecAndPump=mock.Mock()),
					"wx": types.SimpleNamespace(CallAfter=lambda func, *args: func(*args)),
				}
				for name, storeName in ((ADDON_ID, "voiceStore"), ("samsungTVVoices", "firmwareStore")):
					interval = "daily" if name == ADDON_ID else "never"
					store = types.SimpleNamespace(loadSettings=lambda interval=interval:
						{"updateInterval": interval})
					modules[f"synthDrivers._{name}"] = types.SimpleNamespace(**{storeName: store})
				with mock.patch.dict(sys.modules, modules):
					loaded = []
					for name in order:
						tree = ast.parse((roots[name] / f"addon/globalPlugins/{name}.py").read_text(encoding="utf-8"))
						statement = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
							and any(alias.name == "SignedWebUpdater" for alias in node.names))
						namespace = {"__package__": "globalPlugins"}
						exec(compile(ast.Module(body=[statement], type_ignores=[]), "plugin-import", "exec"), namespace)
						cls = namespace["SignedWebUpdater"]
						module = importlib.import_module(cls.__module__)
						loaded.append(cls)
						self.assertEqual("1.0.0", module._currentVersion())
						instance = cls()
						self.assertEqual("daily" if name == ADDON_ID else "never", instance._interval)
						instance._offerUpdate = mock.Mock()
						instance._schedule = mock.Mock()
						response = mock.MagicMock()
						response.__enter__.return_value.read.return_value = b'{"version":"9.0.0","url":"https://example.invalid/test","sha256":"test","size":1}'
						with mock.patch.object(module.urllib.request, "urlopen", return_value=response) as request, mock.patch.object(module, "_verifySignature"):
							instance._lock.acquire()
							instance._checkWorker(False)
							self.assertEqual(f"https://github.com/OnjLouis/{name}/releases/latest/download/{name}-update.json",
								request.call_args.args[0].full_url)
						instance._offerUpdate.assert_called_once()
						instance._schedule.assert_called_once_with(False)
					self.assertIsNot(*loaded)

	def test_cached_updater_from_another_addon_is_not_used(self):
		plugin_dir = ROOT / "addon/globalPlugins"
		tree = ast.parse((plugin_dir / f"{ADDON_ID}.py").read_text(encoding="utf-8"))
		statement = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
			and any(alias.name == "SignedWebUpdater" for alias in node.names))
		foreign_class = type("ForeignUpdater", (), {})
		foreign_module = types.ModuleType("globalPlugins._signedWebUpdater")
		foreign_module.SignedWebUpdater = foreign_class
		package = types.ModuleType("globalPlugins")
		package.__path__ = [str(plugin_dir)]
		store = types.SimpleNamespace(loadSettings=lambda: {"updateInterval": "daily"})
		modules = {
			"globalPlugins": package,
			"globalPlugins._signedWebUpdater": foreign_module,
			"addonHandler": types.SimpleNamespace(initTranslation=lambda: None,
				getAvailableAddons=lambda: [types.SimpleNamespace(name=ADDON_ID, manifest={"version": "1.2.3"})]),
			"core": types.ModuleType("core"), "gui": types.ModuleType("gui"),
			"synthDriverHandler": types.ModuleType("synthDriverHandler"),
			"logHandler": types.SimpleNamespace(log=mock.Mock()),
			"systemUtils": types.SimpleNamespace(ExecAndPump=mock.Mock()),
			"wx": types.ModuleType("wx"),
			"synthDrivers._samsungGalaxyVoices": types.SimpleNamespace(voiceStore=store),
		}
		with mock.patch.dict(sys.modules, modules):
			namespace = {"__package__": "globalPlugins"}
			exec(compile(ast.Module(body=[statement], type_ignores=[]), "plugin-import", "exec"), namespace)
			updater_class = namespace["SignedWebUpdater"]
			self.assertIsNot(foreign_class, updater_class)
			updater = importlib.import_module(updater_class.__module__)
			self.assertEqual(
				f"https://github.com/OnjLouis/{ADDON_ID}/releases/latest/download/{ADDON_ID}-update.json",
				updater.MANIFEST_URL,
			)
			self.assertEqual("1.2.3", updater._currentVersion())
			self.assertEqual("daily", updater_class()._interval)


if __name__ == "__main__":
	unittest.main()
