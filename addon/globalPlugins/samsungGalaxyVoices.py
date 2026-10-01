# license: GPL-2.0-or-later
"""Voice download manager for Samsung Galaxy Voices."""

import builtins
import concurrent.futures
import config
import json
import os
import struct
import subprocess
import threading
import time
import weakref

import addonHandler
import globalPluginHandler
import gui
from logHandler import log
import nvwave
import synthDriverHandler
from synthDrivers._samsungGalaxyVoices import voiceStore
from ._samsungGalaxyUpdater import SignedWebUpdater
import ui
import wx

_ = getattr(builtins, "_", lambda text: text)
addonHandler.initTranslation()

_CATALOG_CODES = voiceStore.CATALOG_CODES
_DOWNLOADABLE_CODES = voiceStore.DOWNLOADABLE_CODES
_COMPACT_CODES = frozenset(voiceStore.COMPACT_CODES)

_FRAME_LENGTH = struct.Struct("<I")
_PREVIEW_TEXT = _("This is a sample of the selected Samsung Galaxy voice.")
_activeUpdater = None


class _VoicePreview:
	def __init__(self, panelRef):
		self._panelRef = panelRef
		self._lock = threading.RLock()
		self._process = None
		self._player = None
		self._generation = 0

	@staticmethod
	def _readExact(stream, size):
		data = bytearray()
		while len(data) < size:
			part = stream.read(size - len(data))
			if not part:
				raise RuntimeError("The Samsung speech helper stopped during the sample.")
			data.extend(part)
		return bytes(data)

	@classmethod
	def _readFrame(cls, process):
		kind = cls._readExact(process.stdout, 1)
		size = _FRAME_LENGTH.unpack(cls._readExact(process.stdout, 4))[0]
		if size > 16 << 20:
			raise RuntimeError("The Samsung speech helper returned an invalid sample.")
		return kind, cls._readExact(process.stdout, size) if size else b""

	@staticmethod
	def _writeFrame(process, kind, payload=b""):
		process.stdin.write(kind + _FRAME_LENGTH.pack(len(payload)) + payload)
		process.stdin.flush()

	def play(self, code):
		self.stop()
		self._generation += 1
		generation = self._generation
		threading.Thread(
			target=self._worker,
			args=(code, generation),
			name="Samsung Galaxy voice sample",
			daemon=True,
		).start()

	def _worker(self, code, generation):
		error = None
		process = None
		player = None
		try:
			definitions = voiceStore.loadVoiceDefinitions()
			details = definitions[voiceStore.voiceId(code)]
			addon = addonHandler.getCodeAddon()
			dataDir = os.path.join(addon.path, "synthDrivers", "_samsungGalaxyVoices")
			runtimeDir = os.path.join(dataDir, "runtime")
			process = subprocess.Popen(
				[
					os.path.join(runtimeDir, "samsungGalaxyHost.exe"), "--server",
					details["enginePath"], details["path"], os.path.join(dataDir, "android"),
					details["family"], str(details["speaker"]),
				],
				stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
				bufsize=0, cwd=runtimeDir,
				creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
			)
			with self._lock:
				if generation != self._generation:
					return
				self._process = process
			kind, payload = self._readFrame(process)
			if kind != b"R":
				raise RuntimeError(payload.decode("utf-8", "replace") or "The Samsung voice could not start.")
			metadata = json.loads(payload.decode("utf-8"))
			sampleRate = int(metadata["sampleRate"])
			try:
				player = nvwave.WavePlayer(
					channels=1, samplesPerSec=sampleRate, bitsPerSample=16,
					outputDevice=config.conf["audio"]["outputDevice"],
				)
			except Exception:
				player = nvwave.WavePlayer(1, sampleRate, 16)
			with self._lock:
				if generation != self._generation:
					return
				self._player = player
			self._writeFrame(process, b"P", struct.pack("<ii", 100, 100))
			if self._readFrame(process)[0] != b"K":
				raise RuntimeError("The Samsung voice rejected the sample settings.")
			self._writeFrame(process, b"S", _PREVIEW_TEXT.encode("utf-8"))
			while generation == self._generation:
				kind, payload = self._readFrame(process)
				if kind == b"A":
					player.feed(payload)
				elif kind == b"D":
					player.idle()
					break
				elif kind == b"E":
					raise RuntimeError(payload.decode("utf-8", "replace"))
		except Exception as caught:
			error = str(caught)
			log.error("Samsung Galaxy voice sample failed", exc_info=True)
		finally:
			if player is not None:
				try:
					player.stop()
				except Exception:
					pass
			if process is not None and process.poll() is None:
				try:
					self._writeFrame(process, b"Q")
					process.wait(2)
				except Exception:
					process.kill()
			with self._lock:
				if self._process is process:
					self._process = None
					self._player = None
			wx.CallAfter(_callPanel, self._panelRef, "_previewFinished", generation, error)

	def stop(self):
		with self._lock:
			self._generation += 1
			process = self._process
			player = self._player
			self._process = None
			self._player = None
		if player is not None:
			try:
				player.stop()
			except Exception:
				pass
		if process is not None and process.poll() is None:
			try:
				process.terminate()
			except OSError:
				pass


def _voiceId(code):
	return voiceStore.voiceId(code)


def _languageId(code):
	return voiceStore.languageId(code)


def _voiceLabel(code):
	return voiceStore.voiceLabel(code)


def _voicePath(code):
	return voiceStore.voicePath(code)


def _isInstalled(code):
	return voiceStore.isInstalled(code)




_downloadLock = threading.RLock()
_downloadState = {
	"busy": False,
	"percent": 0,
	"status": "",
	"details": {},
	"selected": (),
	"activeCode": None,
	"completedSerial": 0,
}


def _callPanel(panelRef, methodName, *args):
	panel = panelRef()
	if panel is None:
		return
	try:
		if panel.IsBeingDeleted():
			return
		getattr(panel, methodName)(*args)
	except RuntimeError:
		return


def _downloadSnapshot():
	with _downloadLock:
		return dict(_downloadState)


def _setDownloadProgress(percent, status, activeCode=None, details=None):
	with _downloadLock:
		_downloadState["percent"] = max(0, min(100, int(percent)))
		_downloadState["status"] = status
		_downloadState["activeCode"] = activeCode
		if details is not None:
			_downloadState["details"] = dict(details)


def _refreshActiveSynth():
	try:
		from synthDrivers import samsungGalaxyVoices as samsungSynth
		synth = synthDriverHandler.getSynth()
		if synth.name == "samsungGalaxyVoices":
			synth.refreshAvailableVoices()
		else:
			samsungSynth.reloadInstalledVoices()
	except Exception:
		log.error("Could not refresh Samsung voices after download", exc_info=True)


def _startVoiceDownloads(codes):
	codes = tuple(codes)
	with _downloadLock:
		if _downloadState["busy"]:
			return False
		_downloadState.update(
			busy=True,
			percent=0,
			status=_("Preparing to download {count} selected voice(s)...").format(count=len(codes)),
			details={"stage": _("Preparing")},
			selected=codes,
		)

	def worker():
		installedNames = []
		errors = []
		transfer = {"code": None, "startedAt": 0.0, "startedBytes": 0}

		def progress(position, code, received, total):
			if transfer["code"] != code:
				transfer.update(code=code, startedAt=time.monotonic(), startedBytes=received)
			percent = min(100, round(received * 100 / total)) if total else 0
			overall = round(((position - 1) + ((received / total) if total else 0)) * 100 / len(codes))
			speedText = ""
			elapsed = time.monotonic() - transfer["startedAt"]
			transferred = max(0, received - transfer["startedBytes"])
			if elapsed > 0 and transferred > 0:
				speedText = _(", {speed:.1f} MB/s").format(
					speed=transferred / elapsed / (1024 * 1024),
				)
			_setDownloadProgress(
				overall,
				_("Downloading {voice}, {position} of {count}: {percent}% ({received:.1f} of {total:.1f} MB){speed}").format(
					voice=_voiceLabel(code), position=position, count=len(codes), percent=percent,
					received=received / (1024 * 1024), total=total / (1024 * 1024), speed=speedText,
				),
				code,
				{
					"voice": _voiceLabel(code),
					"stage": _("Downloading"),
					"item": _("{position} of {count}").format(position=position, count=len(codes)),
					"progress": _("{percent}%").format(percent=percent),
					"downloaded": _("{received:.1f} of {total:.1f} MB").format(
						received=received / (1024 * 1024), total=total / (1024 * 1024),
					),
					"speed": speedText.lstrip(", "),
				},
			)

		for position, code in enumerate(codes, start=1):
			transfer["code"] = None
			_setDownloadProgress(
				round((position - 1) * 100 / len(codes)),
				_("Contacting Samsung for {voice} ({position} of {count})...").format(
					voice=_voiceLabel(code), position=position, count=len(codes),
				),
				code,
				{
					"voice": _voiceLabel(code),
					"stage": _("Starting"),
					"item": _("{position} of {count}").format(position=position, count=len(codes)),
				},
			)
			try:
				name = voiceStore.installVoice(
					code,
					lambda received, total, currentCode=code, currentPosition=position: progress(
						currentPosition, currentCode, received, total,
					),
				)
			except Exception as error:
				log.error("Samsung Galaxy voice download failed for %s", code, exc_info=True)
				errors.append((code, str(error)))
			else:
				installedNames.append(name)
		if installedNames:
			wx.CallAfter(_refreshActiveSynth)
		if errors and installedNames:
			message = _("Installed {installed} voice(s); {failed} failed.").format(
				installed=len(installedNames), failed=len(errors),
			)
		elif errors:
			message = _("No voices were installed.")
		else:
			message = _("Installed {count} voice(s). They are now available.").format(count=len(installedNames))
		details = "; ".join(
			_("{voice}: {error}").format(voice=_voiceLabel(code), error=error)
			for code, error in errors
		)
		result = f"{message} {details}" if details else message
		with _downloadLock:
			_downloadState.update(
				busy=False,
				percent=100 if not errors else _downloadState["percent"],
				status=result,
				details={"result": result},
				activeCode=None,
				completedSerial=_downloadState["completedSerial"] + 1,
			)
		wx.CallAfter(ui.message, result)

	threading.Thread(target=worker, name="Samsung Galaxy voice downloads", daemon=True).start()
	return True


class SamsungGalaxyVoicesPanel(gui.settingsDialogs.SettingsPanel):
	title = _("Samsung Galaxy Voices")

	def makeSettings(self, settingsSizer):
		self._busy = _downloadSnapshot()["busy"]
		self._preview = _VoicePreview(weakref.ref(self))
		self._settings = voiceStore.loadSettings()
		self._catalogSizes = voiceStore.loadCatalogCache()
		helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		helper.addItem(wx.StaticText(
			self,
			label=_("No Samsung voices are included. Select one or more voices to download directly from Samsung. Installed voices become available immediately."),
		))
		label = helper.addItem(wx.StaticText(self, label=_("&Available voices:")))
		self.voiceList = helper.addItem(wx.ListCtrl(
			self,
			style=wx.LC_REPORT | wx.BORDER_SUNKEN,
		))
		label.SetName(_("Available voices"))
		self.voiceList.SetName(_("Available voices"))
		self.voiceList.InsertColumn(0, _("Voice"), width=330)
		self.voiceList.InsertColumn(1, _("Status"), width=110)
		self.voiceList.InsertColumn(2, _("Size"), width=90)
		buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.downloadButton = wx.Button(self, label=_("&Download or update"))
		self.removeButton = wx.Button(self, label=_("&Remove"))
		self.playButton = wx.Button(self, label=_("&Play sample"))
		self.stopButton = wx.Button(self, label=_("&Stop sample"))
		buttonSizer.Add(self.downloadButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.removeButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.playButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.stopButton)
		helper.addItem(buttonSizer)
		self.progress = helper.addItem(wx.Gauge(self, range=100))
		self.progress.SetName(_("Voice download progress"))
		statusLabel = helper.addItem(wx.StaticText(self, label=_("Download status:")))
		self.statusList = helper.addItem(wx.ListCtrl(self, style=wx.LC_REPORT | wx.BORDER_SUNKEN, size=(-1, 135)))
		statusLabel.SetName(_("Download status"))
		self.statusList.SetName(_("Download status"))
		self.statusList.InsertColumn(0, _("Detail"), width=130)
		self.statusList.InsertColumn(1, _("Value"), width=420)
		self._statusRows = {}
		for key, label in (
			("voice", _("Voice")),
			("stage", _("Stage")),
			("item", _("Item")),
			("progress", _("Progress")),
			("downloaded", _("Downloaded")),
			("speed", _("Speed")),
			("result", _("Result")),
		):
			row = self.statusList.InsertItem(self.statusList.GetItemCount(), label)
			self.statusList.SetItem(row, 1, "")
			self._statusRows[key] = row
		self.playbackBuffer = helper.addLabeledControl(
			_("Playback &buffering (milliseconds):"),
			wx.SpinCtrl,
			min=0,
			max=500,
			initial=self._settings["playbackBufferMs"],
		)
		self.playbackBuffer.SetName(_("Playback buffering in milliseconds"))
		self.playbackBuffer.SetIncrement(25)
		updateSizer = wx.BoxSizer(wx.HORIZONTAL)
		updateLabel = wx.StaticText(self, label=_("Check for add-on &updates:"))
		self.updateInterval = wx.Choice(self, choices=(_("Never"), _("Hourly"), _("Daily")))
		intervals = ("never", "hourly", "daily")
		self.updateInterval.SetSelection(intervals.index(self._settings["updateInterval"]))
		self.checkNowButton = wx.Button(self, label=_("Check &Now"))
		updateSizer.Add(updateLabel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
		updateSizer.Add(self.updateInterval, 1, wx.RIGHT, 8)
		updateSizer.Add(self.checkNowButton)
		helper.addItem(updateSizer)
		self.helpButton = helper.addItem(wx.Button(self, label=_("&Help")))
		self.downloadButton.Bind(wx.EVT_BUTTON, self.onDownload)
		self.removeButton.Bind(wx.EVT_BUTTON, self.onRemove)
		self.playButton.Bind(wx.EVT_BUTTON, self.onPlay)
		self.stopButton.Bind(wx.EVT_BUTTON, self.onStop)
		self.checkNowButton.Bind(wx.EVT_BUTTON, self.onCheckNow)
		self.helpButton.Bind(wx.EVT_BUTTON, lambda event: self._openManual())
		self.voiceList.Bind(wx.EVT_LIST_ITEM_SELECTED, self.onSelectionChanged)
		self.voiceList.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.onSelectionChanged)
		self.Bind(wx.EVT_CHAR_HOOK, self.onCharHook)
		self.Bind(wx.EVT_WINDOW_DESTROY, self.onDestroy)
		self._refresh()
		self._loadCatalogSizes()
		snapshot = _downloadSnapshot()
		self._seenCompletedSerial = snapshot["completedSerial"]
		self._downloadTimer = wx.Timer(self)
		self.Bind(wx.EVT_TIMER, self._syncDownloadState, self._downloadTimer)
		self._syncDownloadState()
		self._downloadTimer.Start(250)

	def _refresh(self, selectedCodes=None):
		selectedCodes = set(selectedCodes or ())
		self.voiceList.DeleteAllItems()
		visibleCodes = [
			code for code in _CATALOG_CODES
			if code in _DOWNLOADABLE_CODES or _isInstalled(code)
		]
		self._visibleCodes = sorted(
			visibleCodes,
			key=lambda code: (
				not _isInstalled(code),
				code in _COMPACT_CODES,
				_voiceLabel(code).casefold(),
			),
		)
		self._rowByCode = {}
		selectedIndexes = []
		for catalogIndex, code in enumerate(self._visibleCodes):
			status = self._statusForCode(code)
			index = self.voiceList.InsertItem(self.voiceList.GetItemCount(), _voiceLabel(code))
			self.voiceList.SetItem(index, 1, status)
			cached = self._catalogSizes.get(code)
			self.voiceList.SetItem(
				index,
				2,
				_("{size:.1f} MB").format(size=cached["size"] / (1024 * 1024))
				if cached else _("Checking..."),
			)
			self.voiceList.SetItemData(index, catalogIndex)
			self._rowByCode[code] = index
			if code in selectedCodes:
				selectedIndexes.append(index)
		if not selectedIndexes and self._visibleCodes:
			selectedIndexes.append(0)
		for index in selectedIndexes:
			self.voiceList.Select(index)
		if selectedIndexes:
			self.voiceList.Focus(selectedIndexes[0])
		self._updateButtons()

	def _statusForCode(self, code):
		installed = _isInstalled(code)
		if code not in _DOWNLOADABLE_CODES:
			return _("Installed; legacy quality")
		if installed:
			return _("Installed; compact") if code in _COMPACT_CODES else _("Installed")
		return _("Available; compact") if code in _COMPACT_CODES else _("Available")

	def _loadCatalogSizes(self):
		missing = [code for code in self._visibleCodes if code not in self._catalogSizes]
		if not missing:
			return
		panelRef = weakref.ref(self)

		def worker():
			updated = dict(self._catalogSizes)

			def retrieve(code):
				try:
					metadata = voiceStore.downloadMetadata(code)
				except Exception as error:
					return code, None, error
				return code, metadata, None

			with concurrent.futures.ThreadPoolExecutor(
				max_workers=6,
				thread_name_prefix="Samsung catalogue request",
			) as executor:
				results = executor.map(retrieve, missing)
				for code, metadata, error in results:
					if error is not None:
						log.debugWarning(
							"Could not retrieve Samsung catalogue size for %s: %s",
							code,
							error,
						)
						wx.CallAfter(_callPanel, panelRef, "_showCatalogSizeUnavailable", code)
						continue
					entry = {
						"size": metadata["size"],
						"checked": time.time(),
					}
					updated[code] = entry
					wx.CallAfter(_callPanel, panelRef, "_showCatalogEntry", code, entry)
			try:
				voiceStore.saveCatalogCache(updated)
			except OSError:
				log.debugWarning("Could not save the Samsung voice catalogue cache", exc_info=True)

		threading.Thread(target=worker, name="Samsung voice catalogue", daemon=True).start()

	def _showCatalogEntry(self, code, entry):
		if self.IsBeingDeleted():
			return
		row = self._rowByCode.get(code)
		self._catalogSizes[code] = dict(entry)
		if row is not None:
			self.voiceList.SetItem(row, 1, self._statusForCode(code))
			self.voiceList.SetItem(row, 2, _("{size:.1f} MB").format(size=entry["size"] / (1024 * 1024)))
		self._updateButtons()

	def _showCatalogSizeUnavailable(self, code):
		if self.IsBeingDeleted():
			return
		row = self._rowByCode.get(code)
		if row is not None:
			self.voiceList.SetItem(row, 2, _("Unknown"))

	def _selectedCodes(self):
		codes = []
		index = self.voiceList.GetFirstSelected()
		while index >= 0:
			catalogIndex = self.voiceList.GetItemData(index)
			codes.append(self._visibleCodes[catalogIndex])
			index = self.voiceList.GetNextItem(index, wx.LIST_NEXT_ALL, wx.LIST_STATE_SELECTED)
		return codes

	def _downloadableSelection(self):
		return [
			code for code in self._selectedCodes()
			if code in _DOWNLOADABLE_CODES
		]

	def _removableSelection(self):
		return [
			code for code in self._selectedCodes()
			if _isInstalled(code)
		]

	def _updateButtons(self):
		self.downloadButton.Enable(not self._busy and bool(self._downloadableSelection()))
		self.removeButton.Enable(not self._busy and bool(self._removableSelection()))
		self.playButton.Enable(not self._busy and len(self._removableSelection()) == 1)
		self.stopButton.Enable(not self._busy)

	def onSelectionChanged(self, event):
		self._updateButtons()
		event.Skip()

	def onDownload(self, event):
		codes = self._downloadableSelection()
		if self._busy or not codes:
			return
		if not any(_isInstalled(code) for code in _CATALOG_CODES):
			answer = gui.messageBox(
				_("Samsung voice data and its matching speech engine will be downloaded directly from Samsung and stored in your NVDA user-data folder. Samsung owns these components, and this unofficial add-on is not affiliated with or endorsed by Samsung. Continue?"),
				_("Download Samsung voice"),
				wx.YES_NO | wx.NO_DEFAULT | wx.ICON_INFORMATION,
			)
			if answer != wx.YES:
				return
		if _startVoiceDownloads(codes):
			self._syncDownloadState()

	def _syncDownloadState(self, event=None):
		snapshot = _downloadSnapshot()
		self._busy = snapshot["busy"]
		self.progress.SetValue(snapshot["percent"])
		for key, row in self._statusRows.items():
			value = str(snapshot["details"].get(key, ""))
			if self.statusList.GetItemText(row, 1) != value:
				self.statusList.SetItem(row, 1, value)
		for code, row in self._rowByCode.items():
			if snapshot["busy"] and code == snapshot["activeCode"]:
				rowStatus = _("Downloading")
			else:
				rowStatus = self._statusForCode(code)
			if self.voiceList.GetItemText(row, 1) != rowStatus:
				self.voiceList.SetItem(row, 1, rowStatus)
		if snapshot["completedSerial"] != self._seenCompletedSerial:
			self._seenCompletedSerial = snapshot["completedSerial"]
			self._refresh(snapshot["selected"])
		else:
			self._updateButtons()

	def _setStatusResult(self, message):
		self.statusList.SetItem(self._statusRows["result"], 1, message)

	def onRemove(self, event):
		codes = self._removableSelection()
		if not codes:
			return
		answer = gui.messageBox(
			_("Remove {count} selected voice(s)? They can be downloaded again later.").format(count=len(codes)),
			_("Remove Samsung voices"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
		)
		if answer != wx.YES:
			return
		activeSynth = None
		fallbackName = None
		try:
			synth = synthDriverHandler.getSynth()
			if synth.name == "samsungGalaxyVoices":
				activeSynth = synth
				fallbackName = synth.prepareVoicesForRemoval(tuple(_voiceId(code) for code in codes))
		except ValueError:
			message = _("The active voice is the only installed Samsung voice. Install another voice or switch to another synthesizer before removing it.")
			self._setStatusResult(message)
			ui.message(message)
			return
		except Exception:
			log.error("Could not prepare the active Samsung voice for removal", exc_info=True)
			message = _("The active voice could not be changed safely, so no voices were removed.")
			self._setStatusResult(message)
			ui.message(message)
			return
		removed = 0
		errors = []
		for code in codes:
			try:
				voiceStore.removeVoices((code,))
			except OSError as error:
				errors.append((code, str(error)))
			else:
				removed += 1
		if errors:
			message = _("Removed {removed} voice(s); {failed} could not be removed. Switch away from Samsung Galaxy Voices, restart NVDA, and try the failed items again.").format(
				removed=removed,
				failed=len(errors),
			)
			details = "\n".join(
				_("{voice}: {error}").format(voice=_voiceLabel(code), error=error)
				for code, error in errors
			)
			self._setStatusResult(f"{message}\n{details}")
		else:
			message = _("Removed {count} voice(s).").format(count=removed)
			if fallbackName:
				message += " " + _("Samsung is now using {voice}.").format(voice=fallbackName)
			self._setStatusResult(message)
		if activeSynth is not None:
			try:
				activeSynth.refreshAvailableVoices()
			except Exception:
				log.error("Could not refresh Samsung voices after removal", exc_info=True)
		ui.message(message)
		self._refresh(codes)

	def onPlay(self, event):
		codes = self._removableSelection()
		if len(codes) != 1:
			return
		self._setStatusResult(_("Preparing a sample of {voice}...").format(voice=_voiceLabel(codes[0])))
		self._preview.play(codes[0])

	def onStop(self, event):
		self._preview.stop()
		self._setStatusResult(_("Voice sample stopped."))

	def _previewFinished(self, generation, error):
		if self.IsBeingDeleted() or generation != self._preview._generation:
			return
		if error:
			self._setStatusResult(_("The voice sample could not be played: {error}").format(error=error))
			ui.message(_("The voice sample could not be played."))
		else:
			self._setStatusResult(_("Voice sample finished."))

	def onCheckNow(self, event):
		if _activeUpdater is None:
			ui.message(_("The add-on updater is not available."))
			return
		_activeUpdater.checkNow(True)

	def onDestroy(self, event):
		if event.GetEventObject() is self:
			self._preview.stop()
			if hasattr(self, "_downloadTimer"):
				self._downloadTimer.Stop()
		event.Skip()

	def onCharHook(self, event):
		if event.GetKeyCode() == wx.WXK_F1:
			self._openManual()
			return
		event.Skip()

	def _openManual(self):
		try:
			addon = addonHandler.getCodeAddon()
			manualPath = addon.getDocFilePath()
			if manualPath and os.path.isfile(manualPath):
				os.startfile(manualPath)
				return
		except Exception:
			log.error("Could not open the Samsung Galaxy Voices manual", exc_info=True)
		ui.message(_("The Samsung Galaxy Voices manual is not available."))

	def onSave(self):
		intervals = ("never", "hourly", "daily")
		self._settings["updateInterval"] = intervals[self.updateInterval.GetSelection()]
		self._settings["playbackBufferMs"] = self.playbackBuffer.GetValue()
		voiceStore.saveSettings(self._settings)
		try:
			synth = synthDriverHandler.getSynth()
			if synth.name == "samsungGalaxyVoices":
				synth.setPlaybackBufferMilliseconds(self._settings["playbackBufferMs"])
		except Exception:
			log.debugWarning("Could not apply Samsung playback buffering immediately", exc_info=True)
		if _activeUpdater is not None:
			_activeUpdater.setInterval(self._settings["updateInterval"])


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		global _activeUpdater
		super().__init__()
		removedVoices, migratedVoices = voiceStore.maintainVoiceStore()
		if removedVoices:
			log.info("Samsung Galaxy Voices removed %d incompatible voice package(s)", removedVoices)
		if migratedVoices:
			log.info("Samsung Galaxy Voices migrated %d voice folder(s)", migratedVoices)
		if SamsungGalaxyVoicesPanel not in gui.settingsDialogs.NVDASettingsDialog.categoryClasses:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(SamsungGalaxyVoicesPanel)
		self._updater = SignedWebUpdater()
		_activeUpdater = self._updater
		self._updater.start()
		settings = voiceStore.loadSettings()
		if not settings["voicePromptShown"] and not any(_isInstalled(code) for code in _CATALOG_CODES):
			settings["voicePromptShown"] = True
			voiceStore.saveSettings(settings)
			self._firstRunTimer = wx.CallLater(1500, self._promptForVoice)
		else:
			self._firstRunTimer = None

	def _promptForVoice(self):
		answer = gui.messageBox(
			_("Samsung Galaxy Voices does not include proprietary Samsung voice files. Open its settings panel now to choose at least one voice to download directly from Samsung?"),
			_("Choose a Samsung voice"),
			wx.YES_NO | wx.YES_DEFAULT | wx.ICON_INFORMATION,
		)
		if answer == wx.YES:
			gui.mainFrame._popupSettingsDialog(gui.settingsDialogs.NVDASettingsDialog, SamsungGalaxyVoicesPanel)

	def terminate(self):
		global _activeUpdater
		if self._firstRunTimer is not None:
			self._firstRunTimer.Stop()
		self._updater.stop()
		_activeUpdater = None
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(SamsungGalaxyVoicesPanel)
		except ValueError:
			pass
		super().terminate()
