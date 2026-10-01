import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "addon" / "globalPlugins" / "_signedWebUpdater.py"
MANIFEST = ROOT / "tests" / "fixtures" / "signed-test-manifest.json"
sys.path.insert(0, str(ROOT / "addon"))

for name in ("addonHandler", "core", "gui", "synthDriverHandler", "wx"):
	sys.modules[name] = types.ModuleType(name)
sys.modules["addonHandler"].initTranslation = lambda: None
sys.modules["wx"].OK = 1
sys.modules["wx"].ICON_INFORMATION = 2
sys.modules["wx"].ICON_ERROR = 4
log_handler = types.ModuleType("logHandler")
log_handler.log = types.SimpleNamespace(error=lambda *args, **kwargs: None)
sys.modules["logHandler"] = log_handler
system_utils = types.ModuleType("systemUtils")
system_utils.ExecAndPump = lambda *args, **kwargs: None
sys.modules["systemUtils"] = system_utils
global_vars = types.ModuleType("globalVars")
global_vars.appArgs = types.SimpleNamespace(configPath=str(ROOT / "unused-test-config"))
sys.modules["globalVars"] = global_vars

spec = importlib.util.spec_from_file_location("signedUpdaterUnderTest", MODULE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class SignatureTests(unittest.TestCase):
	def test_valid_signature_is_accepted(self):
		info = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
		updater._verifySignature(info)

	def test_changed_manifest_is_rejected(self):
		info = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
		info["url"] = "https://example.invalid/tampered.nvda-addon"
		with self.assertRaises(RuntimeError):
			updater._verifySignature(info)


class InstallTests(unittest.TestCase):
	def test_update_marks_existing_addon_for_removal_before_restart(self):
		existing = mock.Mock()
		existing.name = "samsungGalaxyVoices"
		existing.isPendingInstall = False
		pending = mock.Mock()
		pending.name = "samsungGalaxyVoices"
		pending.isPendingInstall = True
		staged = mock.Mock()
		bundle = types.SimpleNamespace(
			_installExceptions=[],
			manifest={"name": "samsungGalaxyVoices"},
		)
		result = types.SimpleNamespace(funcRes=staged)
		order = []
		existing.requestRemove.side_effect = lambda: order.append("remove")
		updater.core.restart = lambda: order.append("restart")
		updater.gui.messageBox = mock.Mock()
		updater.synthDriverHandler.getSynth = lambda: types.SimpleNamespace(name="oneCore")
		updater.addonHandler.getAvailableAddons = lambda: [pending, existing]
		updater.addonHandler.installAddonBundle = mock.Mock()
		updater.ExecAndPump = lambda *args, **kwargs: result

		instance = object.__new__(updater.SignedWebUpdater)
		instance._install(bundle, str(ROOT / "unused-update.nvda-addon"))

		existing.requestRemove.assert_called_once_with()
		pending.requestRemove.assert_not_called()
		staged._cleanupAddonImports.assert_called_once_with()
		self.assertEqual(["remove", "restart"], order)


if __name__ == "__main__":
	unittest.main()
