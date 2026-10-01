"""Check translation loading and manual selection without running NVDA."""

import ast
import builtins
import gettext
import inspect
from pathlib import Path
from string import Formatter
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "addon"


class LocalizationTests(unittest.TestCase):

	def test_each_translated_module_initializes_its_own_catalog(self):
		for relative in (
			"globalPlugins/samsungGalaxyVoices.py",
			"globalPlugins/_signedWebUpdater.py",
			"synthDrivers/samsungGalaxyVoices.py",
		):
			with self.subTest(module=relative):
				tree = ast.parse((ADDON / relative).read_text(encoding="utf-8"))
				nodes = []
				for node in tree.body:
					if isinstance(node, ast.Assign) and any(
						isinstance(target, ast.Name) and target.id == "_" for target in node.targets
					):
						nodes.append(node)
					elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
						call = node.value.func
						if isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name) and (
							call.value.id == "addonHandler" and call.attr == "initTranslation"
						):
							nodes.append(node)
				translator = lambda text: "translated:" + text
				def initialize():
					inspect.currentframe().f_back.f_globals["_"] = translator
				namespace = {"builtins": builtins, "addonHandler": types.SimpleNamespace(initTranslation=initialize)}
				exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, "exec"), namespace)
				self.assertEqual("translated:Samsung Galaxy Voices", namespace["_"]("Samsung Galaxy Voices"))

	def test_help_uses_nvdas_localized_manual_resolution(self):
		tree = ast.parse((ADDON / "globalPlugins/samsungGalaxyVoices.py").read_text(encoding="utf-8"))
		method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_openManual")
		manual = ADDON / "doc/zh_CN/readme.html"
		addon = types.SimpleNamespace(path=str(ADDON), getDocFilePath=mock.Mock(return_value=str(manual)))
		start = mock.Mock()
		namespace = {
			"addonHandler": types.SimpleNamespace(getCodeAddon=lambda: addon),
			"os": types.SimpleNamespace(path=types.SimpleNamespace(isfile=lambda path: True), startfile=start),
			"log": mock.Mock(), "ui": mock.Mock(), "_": lambda text: text,
		}
		exec(compile(ast.Module(body=[method], type_ignores=[]), "manual", "exec"), namespace)
		namespace["_openManual"](object())
		addon.getDocFilePath.assert_called_once_with()
		start.assert_called_once_with(str(manual))

	def test_translated_catalog_is_usable_by_gettext(self):
		catalog = gettext.translation("nvda", ADDON / "locale", languages=["zh_CN"])
		self.assertEqual("三星 Galaxy 语音", catalog.gettext("Samsung Galaxy Voices"))
		self.assertEqual("下载或更新(&D)", catalog.gettext("&Download or update"))
		self.assertEqual(0, catalog.plural(0))
		self.assertEqual(0, catalog.plural(2))

	def test_translations_preserve_format_fields_and_unique_mnemonics(self):
		catalog = gettext.translation("nvda", ADDON / "locale", languages=["zh_CN"])
		formatter = Formatter()
		keys = []
		for message, translated in catalog._catalog.items():
			if not isinstance(message, str) or not message:
				continue
			fields = lambda text: [(field, spec, conversion) for _, field, spec, conversion in formatter.parse(text) if field]
			self.assertEqual(sorted(fields(message)), sorted(fields(translated)), message)
			if "&" in message:
				self.assertEqual(1, translated.count("&"), message)
				keys.append(translated.split("&", 1)[1][0].casefold())
		self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
	unittest.main()
