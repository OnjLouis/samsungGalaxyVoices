# license: GPL-2.0-or-later
"""Signed GitHub release updater used by Samsung Galaxy Voices."""

import base64
import builtins
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import urllib.request

import addonHandler
import core
import gui
import synthDriverHandler
from logHandler import log
from systemUtils import ExecAndPump
import wx

from synthDrivers._samsungGalaxyVoices import voiceStore

_ = getattr(builtins, "_", lambda text: text)
addonHandler.initTranslation()


MANIFEST_URL = "https://github.com/OnjLouis/samsungGalaxyVoices/releases/latest/download/samsungGalaxyVoices-update.json"
PUBLIC_MODULUS = "1Ceaocem8nO0jpu4DYBq3RJKaXiDBLvqGCyn30b2yoDy0QRWzczx970xDyrSIivavfxJP2X9E96j6e0M5zXT4aneH+N/9X71eVDK6Wult6WxEVlsXyTfewpPdkew0WBHjTsbUEcfdHiVQOD+gdLkvW7aBNYNn9eN+x5dzo6F5HKkuLpgnJgIHnvbT3h8qEp6JnMRK4NdmcD8ZOyrFfj1e1sVSuoC0V2J3glmIa+aWuqmf+oBGVKmXOaIF/hMgclFv0OKq6YE4vi0nVHDVvypRf/EJAb0bXMbthly/RJJj/Mifrc/Nas3yrqh8vCcXysCvml/laBzDsTf16wyivQouf18o6r6gYyxWGqmgZ3l2RMnIo+EDZgZY/mJRV7h44IekDFCc8nDWMjf7KyvL0JqiqOdHefLE5b4TJrpWUiWx7mUhhu4wOSvnz9fN73Zclh+AzxFty9U6ZE84LpecwSZEb0F0cqYcmpfU+nUTWwiGaMiwufqhW8CXv8jgx7YN3+5"
PUBLIC_EXPONENT = "AQAB"
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _version(value):
	parts = []
	for part in str(value).strip().lstrip("vV").split("."):
		try:
			parts.append(int(part))
		except ValueError:
			break
	return tuple(parts or (0,))


def _signedPayload(info):
	unsigned = dict(info)
	unsigned.pop("signature", None)
	return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _verifySignature(info):
	try:
		signature = base64.b64decode(info["signature"], validate=True)
		n = int.from_bytes(base64.b64decode(PUBLIC_MODULUS), "big")
		e = int.from_bytes(base64.b64decode(PUBLIC_EXPONENT), "big")
	except (KeyError, ValueError, TypeError) as error:
		raise RuntimeError("The update manifest has no valid signature.") from error
	length = (n.bit_length() + 7) // 8
	if len(signature) != length:
		raise RuntimeError("The update manifest signature has an invalid length.")
	encoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(length, "big")
	digestInfo = SHA256_DIGEST_INFO + hashlib.sha256(_signedPayload(info)).digest()
	expected = b"\x00\x01" + (b"\xff" * (length - len(digestInfo) - 3)) + b"\x00" + digestInfo
	if not hmac.compare_digest(encoded, expected):
		raise RuntimeError("The update manifest signature is not valid.")


def _currentVersion():
	for addon in addonHandler.getAvailableAddons():
		if addon.name == "samsungGalaxyVoices":
			return str(addon.manifest.get("version") or "0")
	return "0"


class SignedWebUpdater:
	def __init__(self):
		self._timer = None
		self._lock = threading.Lock()
		self._stopped = False
		self._interval = voiceStore.loadSettings()["updateInterval"]

	def start(self):
		self._schedule(initial=True)

	def stop(self):
		self._stopped = True
		if self._timer is not None:
			self._timer.Stop()
			self._timer = None

	def setInterval(self, interval):
		self._interval = interval
		if self._timer is not None:
			self._timer.Stop()
			self._timer = None
		self._schedule(initial=False)

	def _schedule(self, initial=False):
		if self._stopped or self._interval == "never":
			return
		delay = 3_600_000 if self._interval == "hourly" else 86_400_000
		self._timer = core.callLater(delay, self._automaticCheck)

	def _automaticCheck(self):
		self._timer = None
		self.checkNow(False)

	def checkNow(self, fromGui=True):
		if not self._lock.acquire(blocking=False):
			if fromGui:
				wx.CallAfter(gui.messageBox, _("An update check is already running."), _("Samsung Galaxy Voices"), wx.OK)
			return
		threading.Thread(
			target=self._checkWorker,
			args=(fromGui,),
			name="Samsung Galaxy Voices update check",
			daemon=True,
		).start()

	def _checkWorker(self, fromGui):
		try:
			request = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "SamsungGalaxyVoices updater"})
			with urllib.request.urlopen(request, timeout=20) as response:
				info = json.loads(response.read(128 * 1024).decode("utf-8"))
			_verifySignature(info)
			for key in ("version", "url", "sha256", "size"):
				if key not in info:
					raise RuntimeError(f"The signed update manifest is missing {key}.")
			if _version(info["version"]) > _version(_currentVersion()):
				wx.CallAfter(self._offerUpdate, info)
			elif fromGui:
				wx.CallAfter(gui.messageBox, _("Samsung Galaxy Voices is up to date."), _("No update available"), wx.OK | wx.ICON_INFORMATION)
		except Exception:
			log.error("Samsung Galaxy Voices update check failed", exc_info=True)
			if fromGui:
				wx.CallAfter(gui.messageBox, _("Unable to check for updates right now."), _("Update check failed"), wx.OK | wx.ICON_ERROR)
		finally:
			self._lock.release()
			wx.CallAfter(self._schedule, False)

	def _offerUpdate(self, info):
		changes = str(info.get("changes") or "").strip()
		message = _("Samsung Galaxy Voices {version} is available.").format(version=info["version"])
		if changes:
			message += "\n\n" + changes
		message += _("\n\nInstall it now? NVDA will restart after installation.")
		if gui.messageBox(message, _("Update available"), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION) != wx.YES:
			return
		threading.Thread(target=self._downloadWorker, args=(info,), name="Samsung Galaxy Voices update download", daemon=True).start()

	def _downloadWorker(self, info):
		path = None
		try:
			with tempfile.NamedTemporaryFile(prefix="samsungGalaxyVoices-", suffix=".nvda-addon", delete=False) as output:
				path = output.name
				request = urllib.request.Request(str(info["url"]), headers={"User-Agent": "SamsungGalaxyVoices updater"})
				with urllib.request.urlopen(request, timeout=120) as response:
					shasum = hashlib.sha256()
					total = 0
					while True:
						block = response.read(1024 * 1024)
						if not block:
							break
						output.write(block)
						shasum.update(block)
						total += len(block)
			if total != int(info["size"]) or shasum.hexdigest().casefold() != str(info["sha256"]).casefold():
				raise RuntimeError("The downloaded update did not match its signed manifest.")
			bundle = addonHandler.AddonBundle(path)
			if str(bundle.manifest.get("name") or "") != "samsungGalaxyVoices":
				raise RuntimeError("The downloaded package is not Samsung Galaxy Voices.")
			if _version(bundle.manifest.get("version")) != _version(info["version"]):
				raise RuntimeError("The downloaded package version does not match its signed manifest.")
			wx.CallAfter(self._install, bundle, path)
			path = None
		except Exception:
			log.error("Samsung Galaxy Voices update download failed", exc_info=True)
			wx.CallAfter(gui.messageBox, _("The update could not be downloaded or verified."), _("Update failed"), wx.OK | wx.ICON_ERROR)
		finally:
			if path:
				try:
					os.remove(path)
				except OSError:
					pass

	def _install(self, bundle, path):
		try:
			current = synthDriverHandler.getSynth()
			if getattr(current, "name", "") == "samsungGalaxyVoices":
				if not synthDriverHandler.setSynth("oneCore"):
					raise RuntimeError("NVDA could not switch away from Samsung Galaxy Voices.")
			previousAddon = None
			for addon in addonHandler.getAvailableAddons():
				if addon.name == bundle.manifest.get("name") and not getattr(addon, "isPendingInstall", False):
					previousAddon = addon
					break
			result = ExecAndPump(addonHandler.installAddonBundle, bundle)
			if getattr(bundle, "_installExceptions", None):
				raise RuntimeError("NVDA reported an error while staging the add-on update.")
			if previousAddon:
				previousAddon.requestRemove()
			if result.funcRes:
				result.funcRes._cleanupAddonImports()
			gui.messageBox(_("The update is ready. NVDA will now restart."), _("Update complete"), wx.OK | wx.ICON_INFORMATION)
			core.restart()
		except Exception:
			log.error("Samsung Galaxy Voices update installation failed", exc_info=True)
			gui.messageBox(_("The update could not be installed."), _("Update failed"), wx.OK | wx.ICON_ERROR)
		finally:
			try:
				os.remove(path)
			except OSError:
				pass
