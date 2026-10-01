# license: GPL-2.0-or-later
"""NVDA driver for Samsung Galaxy voices hosted by a local ARM64 compatibility layer."""

from array import array
import builtins
from collections import OrderedDict, deque
import ctypes
from ctypes import wintypes
import json
import os
import queue
import struct
import subprocess
import threading
import time

import config
import addonHandler
from logHandler import log
import nvwave
from synthDrivers._samsungGalaxyVoices import voiceStore
from speech.commands import IndexCommand, PitchCommand
from synthDriverHandler import SynthDriver, VoiceInfo, synthDoneSpeaking, synthIndexReached

_ = getattr(builtins, "_", lambda text: text)
addonHandler.initTranslation()

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.join(_HERE, "_samsungGalaxyVoices")
_RUNTIME_DIR = os.path.join(_DATA_DIR, "runtime")
_HOST_PATH = os.path.join(_RUNTIME_DIR, "samsungGalaxyHost.exe")
_ANDROID_DIR = os.path.join(_DATA_DIR, "android")
_MAX_FRAME = 16 << 20
_FRAME_LENGTH = struct.Struct("<I")
_PARAMETERS = struct.Struct("<ii")
_LEGACY_CANCEL_ENGINE_VERSION_MAX = 499_999_999
_LEGACY_HOST_RECOVERY_WAIT_SECONDS = 0.5
_S24_HOST_RECOVERY_WAIT_SECONDS = 0.8
_MAX_WARM_HOSTS = 2

_VOICE_DEFINITIONS = OrderedDict()
_AVAILABLE_VOICES = OrderedDict()


def reloadInstalledVoices():
	"""Reload installed voice metadata while preserving live dictionary objects."""
	definitions = voiceStore.loadVoiceDefinitions()
	_VOICE_DEFINITIONS.clear()
	_VOICE_DEFINITIONS.update(definitions)
	_AVAILABLE_VOICES.clear()
	_AVAILABLE_VOICES.update(
		(voiceId, VoiceInfo(voiceId, details["name"], language=details["language"]))
		for voiceId, details in definitions.items()
	)
	return definitions


reloadInstalledVoices()


class _HostError(RuntimeError):
	pass


class _IO_COUNTERS(ctypes.Structure):
	_fields_ = (
		("ReadOperationCount", ctypes.c_ulonglong),
		("WriteOperationCount", ctypes.c_ulonglong),
		("OtherOperationCount", ctypes.c_ulonglong),
		("ReadTransferCount", ctypes.c_ulonglong),
		("WriteTransferCount", ctypes.c_ulonglong),
		("OtherTransferCount", ctypes.c_ulonglong),
	)


class _BASIC_LIMITS(ctypes.Structure):
	_fields_ = (
		("PerProcessUserTimeLimit", ctypes.c_longlong),
		("PerJobUserTimeLimit", ctypes.c_longlong),
		("LimitFlags", wintypes.DWORD),
		("MinimumWorkingSetSize", ctypes.c_size_t),
		("MaximumWorkingSetSize", ctypes.c_size_t),
		("ActiveProcessLimit", wintypes.DWORD),
		("Affinity", ctypes.c_size_t),
		("PriorityClass", wintypes.DWORD),
		("SchedulingClass", wintypes.DWORD),
	)


class _EXTENDED_LIMITS(ctypes.Structure):
	_fields_ = (
		("BasicLimitInformation", _BASIC_LIMITS),
		("IoInfo", _IO_COUNTERS),
		("ProcessMemoryLimit", ctypes.c_size_t),
		("JobMemoryLimit", ctypes.c_size_t),
		("PeakProcessMemoryUsed", ctypes.c_size_t),
		("PeakJobMemoryUsed", ctypes.c_size_t),
	)


class _ProcessJob:
	"""Terminate the helper automatically if its owning NVDA process exits."""

	_KILL_ON_JOB_CLOSE = 0x00002000
	_EXTENDED_LIMIT_INFORMATION = 9

	def __init__(self, process):
		self._handle = None
		kernel32 = ctypes.windll.kernel32
		kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
		kernel32.CreateJobObjectW.restype = wintypes.HANDLE
		kernel32.SetInformationJobObject.argtypes = (
			wintypes.HANDLE,
			ctypes.c_int,
			ctypes.c_void_p,
			wintypes.DWORD,
		)
		kernel32.SetInformationJobObject.restype = wintypes.BOOL
		kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
		kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
		kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
		kernel32.CloseHandle.restype = wintypes.BOOL
		handle = kernel32.CreateJobObjectW(None, None)
		if not handle:
			raise ctypes.WinError()
		limits = _EXTENDED_LIMITS()
		limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
		if not kernel32.SetInformationJobObject(
			handle,
			self._EXTENDED_LIMIT_INFORMATION,
			ctypes.byref(limits),
			ctypes.sizeof(limits),
		):
			kernel32.CloseHandle(handle)
			raise ctypes.WinError()
		if not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
			kernel32.CloseHandle(handle)
			raise ctypes.WinError()
		self._handle = handle

	def close(self):
		if self._handle:
			ctypes.windll.kernel32.CloseHandle(self._handle)
			self._handle = None


class _SamsungHost:
	def __init__(self):
		self._process = None
		self._job = None
		self._voice = None
		self._messages = queue.Queue(maxsize=128)
		self._writeLock = threading.Lock()
		self._lifecycleLock = threading.RLock()
		self._terminalEvent = threading.Event()
		self._terminalLock = threading.Lock()
		self._terminalKind = None
		self._diagnostics = deque(maxlen=20)
		self._lastFrameAt = None
		self._lastFrameKind = None
		self.sampleRate = 24000
		self.engineVersion = 0
		self.supportsCooperativeCancel = False

	@staticmethod
	def _readExact(stream, size):
		data = bytearray()
		while len(data) < size:
			part = stream.read(size - len(data))
			if not part:
				raise EOFError
			data.extend(part)
		return bytes(data)

	def _reader(self, process):
		messages = self._messages
		try:
			while process is self._process:
				kind = self._readExact(process.stdout, 1)
				size = _FRAME_LENGTH.unpack(self._readExact(process.stdout, 4))[0]
				if size > _MAX_FRAME:
					raise _HostError("The Samsung helper returned an oversized message.")
				payload = self._readExact(process.stdout, size) if size else b""
				if process is not self._process:
					return
				self._lastFrameAt = time.monotonic()
				self._lastFrameKind = kind
				if kind in (b"C", b"D", b"E"):
					self._publishTerminal(process, messages, kind, payload)
				else:
					messages.put((kind, payload))
		except EOFError:
			self._publishTerminal(process, messages, None, b"The Samsung helper stopped unexpectedly.")
		except Exception as error:
			self._publishTerminal(process, messages, None, str(error).encode("utf-8", "replace"))

	def _publishTerminal(self, process, messages, kind, payload):
		# A consumer can remove a queued frame before put() returns. Serialize
		# publication with consumption so an old completion cannot mark a new request done.
		with self._terminalLock:
			if process is self._process:
				messages.put((kind, payload))
				if process is self._process:
					self._terminalKind = kind
					self._terminalEvent.set()

	def _stderrReader(self, process):
		try:
			for line in iter(lambda: process.stderr.readline(1024), b""):
				if process is not self._process:
					return
				text = line.decode("utf-8", "replace").strip()
				if text:
					self._diagnostics.append(text)
		except Exception:
			pass

	def isRunning(self, voice=None):
		running = self._process is not None and self._process.poll() is None
		return running and (voice is None or voice == self._voice)

	@property
	def pid(self):
		process = self._process
		return process.pid if process is not None and process.poll() is None else None

	def start(self, voice):
		with self._lifecycleLock:
			if self.isRunning(voice):
				return
			startedAt = time.monotonic()
			self._stopUnlocked()
			details = _VOICE_DEFINITIONS[voice]
			voiceDir = details["path"]
			enginePath = details["enginePath"]
			arguments = [
				_HOST_PATH,
				"--server",
				enginePath,
				voiceDir,
				_ANDROID_DIR,
				details["family"],
				str(details["speaker"]),
			]
			try:
				process = subprocess.Popen(
					arguments,
					stdin=subprocess.PIPE,
					stdout=subprocess.PIPE,
					stderr=subprocess.PIPE,
					bufsize=0,
					cwd=_RUNTIME_DIR,
					creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
				)
			except OSError as error:
				raise _HostError("The Samsung Galaxy speech helper could not be started.") from error
			self._process = process
			self._voice = voice
			self._messages = queue.Queue(maxsize=128)
			self._terminalKind = None
			self._terminalEvent.clear()
			self._diagnostics.clear()
			self._lastFrameAt = self._lastFrameKind = None
			try:
				self._job = _ProcessJob(process)
			except Exception:
				self._job = None
				log.debugWarning("Samsung Galaxy Voices: process job protection is unavailable", exc_info=True)
			threading.Thread(target=self._reader, args=(process,), name="Samsung Galaxy host reader", daemon=True).start()
			threading.Thread(target=self._stderrReader, args=(process,), name="Samsung Galaxy host diagnostics", daemon=True).start()
			try:
				kind, payload = self._messages.get(timeout=10)
			except queue.Empty as error:
				self._stopUnlocked()
				raise _HostError("The Samsung Galaxy speech helper did not become ready.") from error
			if kind != b"R":
				detail = payload.decode("utf-8", "replace") or "; ".join(self._diagnostics)
				self._stopUnlocked()
				raise _HostError(detail or "The Samsung Galaxy speech helper failed during startup.")
			try:
				metadata = json.loads(payload.decode("utf-8"))
				self.sampleRate = int(metadata["sampleRate"])
				self.engineVersion = int(metadata["engineVersion"])
			except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
				self._stopUnlocked()
				raise _HostError("The Samsung Galaxy helper returned invalid startup information.") from error
			self.supportsCooperativeCancel = bool(metadata.get(
				"cooperativeCancel",
				self.engineVersion <= _LEGACY_CANCEL_ENGINE_VERSION_MAX,
			))
			log.debug(
				f"Samsung Galaxy Voices: host ready; pid={self.pid}; voice={voice}; "
				f"engineVersion={self.engineVersion}; cooperativeCancel={self.supportsCooperativeCancel}; "
				f"startupMs={(time.monotonic() - startedAt) * 1000:.1f}"
			)

	def send(self, kind, payload=b""):
		process = self._process
		if process is None or process.poll() is not None or process.stdin is None:
			raise _HostError("The Samsung Galaxy speech helper is not running.")
		if len(payload) > _MAX_FRAME:
			raise _HostError("The speech request is too large.")
		try:
			with self._writeLock:
				process.stdin.write(kind + _FRAME_LENGTH.pack(len(payload)) + payload)
				process.stdin.flush()
		except (OSError, ValueError) as error:
			raise _HostError("The Samsung Galaxy speech helper connection was lost.") from error

	def getMessage(self, timeout=30):
		try:
			message = self._messages.get(timeout=timeout)
		except queue.Empty as error:
			raise _HostError(
				f"The Samsung Galaxy speech helper stopped responding after {timeout:g}s; "
				f"{self.diagnosticSummary()}"
			) from error
		if message[0] in (b"C", b"D", b"E", None):
			with self._terminalLock:
				pass
		return message

	def diagnosticSummary(self):
		process = self._process
		frameAge = (
			f"{time.monotonic() - self._lastFrameAt:.2f}s"
			if self._lastFrameAt is not None else "none"
		)
		return (
			f"pid={getattr(process, 'pid', None)}; voice={self._voice}; engineVersion={self.engineVersion}; "
			f"exitCode={process.poll() if process is not None else 'closed'}; "
			f"queuedFrames={self._messages.qsize()}; lastFrame={self._lastFrameKind!r}; frameAge={frameAge}"
		)

	def beginRequest(self):
		# A terminal frame belongs to the request that follows. Clearing here
		# prevents stale completion without erasing a completion that races cancel.
		with self._terminalLock:
			self._terminalKind = None
			self._terminalEvent.clear()

	def _releaseConsumer(self):
		try:
			while True:
				self._messages.get_nowait()
		except queue.Empty:
			pass
		self._messages.put_nowait((b"X", b""))

	def requestCancel(self):
		if self.isRunning():
			try:
				self.send(b"X")
			except _HostError:
				pass
			# Release NVDA's synthesis worker immediately. The terminal event is
			# tracked separately so recovery can finish without delaying new speech.
			self._releaseConsumer()

	def abandonRequest(self):
		"""Release NVDA while allowing an engine with unsafe stop support to finish."""
		if self.isRunning():
			self._releaseConsumer()

	def waitForTerminal(self, expectedKind, timeout):
		deadline = time.monotonic() + timeout
		while True:
			if self._terminalEvent.is_set():
				# Speech can finish between cancel() and the helper receiving X.
				# Both successful endings are reusable; the next parameter reply
				# also orders reuse after the queued stop command.
				return self._terminalKind == expectedKind or (
					expectedKind == b"C" and self._terminalKind == b"D"
				)
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				return False
			# Once NVDA's consumer has exited, drain abandoned audio so the bounded
			# queue cannot prevent the helper from delivering its terminal frame.
			try:
				self._messages.get(timeout=min(0.05, remaining))
			except queue.Empty:
				pass

	def prepareReuse(self):
		with self._terminalLock:
			try:
				while True:
					self._messages.get_nowait()
			except queue.Empty:
				pass
			self._terminalKind = None
			self._terminalEvent.clear()

	def _stopUnlocked(self):
		process = self._process
		self._process = None
		self._voice = None
		if process is not None:
			# Release a reader blocked by abandoned audio before closing its pipes.
			self._releaseConsumer()
		if process is not None and process.poll() is None:
			try:
				with self._writeLock:
					process.stdin.write(b"Q" + _FRAME_LENGTH.pack(0))
					process.stdin.flush()
			except (OSError, ValueError, AttributeError):
				pass
			try:
				process.wait(timeout=2)
			except subprocess.TimeoutExpired:
				process.terminate()
				try:
					process.wait(timeout=1)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait(timeout=1)
		if self._job is not None:
			self._job.close()
			self._job = None
		if process is not None:
			for stream in (process.stdin, process.stdout, process.stderr):
				try:
					if stream is not None:
						stream.close()
				except Exception:
					pass

	def stop(self):
		with self._lifecycleLock:
			self._stopUnlocked()

	def abort(self):
		"""Stop busy synthesis and wake its consumer without a command timeout."""
		with self._lifecycleLock:
			process = self._process
			self._process = None
			self._voice = None
			if process is not None:
				self._releaseConsumer()
			if process is not None and process.poll() is None:
				try:
					process.terminate()
					process.wait(timeout=0.25)
				except subprocess.TimeoutExpired:
					process.kill()
					process.wait(timeout=1)
			if self._job is not None:
				self._job.close()
				self._job = None
			if process is not None:
				for stream in (process.stdin, process.stdout, process.stderr):
					try:
						if stream is not None:
							stream.close()
					except Exception:
						pass
			try:
				while True:
					self._messages.get_nowait()
			except queue.Empty:
				pass
			self._messages.put_nowait((None, b"The Samsung helper was cancelled."))


def _makePlayer(sampleRate=24000):
	try:
		return nvwave.WavePlayer(
			channels=1,
			samplesPerSec=sampleRate,
			bitsPerSample=16,
			outputDevice=config.conf["audio"]["outputDevice"],
		)
	except Exception:
		return nvwave.WavePlayer(1, sampleRate, 16)


class SynthDriver(SynthDriver):
	name = "samsungGalaxyVoices"
	description = _("Samsung Galaxy Voices")
	supportedSettings = (
		SynthDriver.VoiceSetting(),
		SynthDriver.RateSetting(minStep=5),
		SynthDriver.PitchSetting(minStep=5),
		SynthDriver.VolumeSetting(minStep=5),
	)
	supportedCommands = {IndexCommand, PitchCommand}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}

	@classmethod
	def check(cls):
		return all(os.path.isfile(path) for path in (
			_HOST_PATH,
			os.path.join(_RUNTIME_DIR, "libc++.dll"),
			os.path.join(_RUNTIME_DIR, "libunwind.dll"),
		)) and bool(_VOICE_DEFINITIONS) and all(
			os.path.isdir(details["path"]) and os.path.isfile(details["enginePath"])
			for details in _VOICE_DEFINITIONS.values()
		)

	def __init__(self):
		super().__init__()
		reloadInstalledVoices()
		self._voice = next(iter(_VOICE_DEFINITIONS))
		self._rate = 50
		self._pitch = 50
		self._volume = 100
		self._playbackBufferMs = voiceStore.loadSettings()["playbackBufferMs"]
		self._player = _makePlayer()
		self._playerLock = threading.Lock()
		self._activePlayer = None
		self._activePlayerDone = None
		self._host = _SamsungHost()
		self._standbyHost = _SamsungHost()
		self._hostLock = threading.RLock()
		self._hostAvailable = threading.Condition(self._hostLock)
		self._activeHost = None
		self._synthesizingHost = None
		self._retiringHosts = set()
		self._jobs = queue.Queue()
		self._stateLock = threading.Lock()
		self._token = 1
		self._stopping = threading.Event()
		self._workerThread = threading.Thread(target=self._worker, name="Samsung Galaxy synth", daemon=True)
		self._workerThread.start()
		self._warmHostPair(self._voice)

	def _get_availableVoices(self):
		return _AVAILABLE_VOICES

	def _get_voice(self):
		return self._voice

	def _set_voice(self, value):
		if value in _VOICE_DEFINITIONS and value != self._voice:
			self._voice = value
			self._replaceHostPair(value)

	def prepareVoicesForRemoval(self, voiceIds):
		"""Move away from an active voice before its files are removed."""
		voiceIds = set(voiceIds)
		if self._voice not in voiceIds:
			return None
		fallback = next((voiceId for voiceId in _VOICE_DEFINITIONS if voiceId not in voiceIds), None)
		if fallback is None:
			raise ValueError("The active Samsung voice is the only installed voice.")
		self.cancel()
		self._voice = fallback
		self._replaceHostPair(fallback, stopActive=True)
		return _VOICE_DEFINITIONS[fallback]["name"]

	def refreshAvailableVoices(self):
		"""Refresh installed voices while preserving a valid current voice."""
		definitions = reloadInstalledVoices()
		if self._voice not in definitions and definitions:
			self._voice = next(iter(definitions))
			self._replaceHostPair(self._voice, stopActive=True)

	def _get_rate(self):
		return self._rate

	def _set_rate(self, value):
		self._rate = max(0, min(100, int(value)))

	def _get_pitch(self):
		return self._pitch

	def _set_pitch(self, value):
		self._pitch = max(0, min(100, int(value)))

	def _get_volume(self):
		return self._volume

	def _set_volume(self, value):
		self._volume = max(0, min(100, int(value)))

	def setPlaybackBufferMilliseconds(self, value):
		self._playbackBufferMs = max(0, min(500, int(value)))

	@staticmethod
	def _nativeValue(value):
		return 50 + max(0, min(100, int(value)))

	@staticmethod
	def _nativeRate(value):
		value = max(0, min(100, int(value)))
		if value <= 50:
			return round(50 * (2 ** (value / 50)))
		return round(100 * (10 ** ((value - 50) / 50)))

	@staticmethod
	def _scaleVolume(data, value):
		value = max(0, min(100, value))
		if value == 100:
			return bytes(data)
		if value == 0:
			return bytes(len(data))
		samples = array("h")
		samples.frombytes(data)
		gain = value / 100.0
		for index, sample in enumerate(samples):
			samples[index] = round(sample * gain)
		return samples.tobytes()

	def _isCurrent(self, token):
		with self._stateLock:
			return not self._stopping.is_set() and token == self._token

	def _buildEvents(self, speechSequence):
		events = []
		textParts = []
		trailingIndexes = []
		pitchOffset = 0

		def flushText():
			text = "".join(textParts).strip()
			textParts.clear()
			if text:
				events.append(("text", text, pitchOffset, tuple(trailingIndexes)))
			elif trailingIndexes:
				events.extend(("index", index, 0, ()) for index in trailingIndexes)
			trailingIndexes.clear()

		for item in speechSequence:
			if isinstance(item, str):
				textParts.append(item)
			elif isinstance(item, IndexCommand):
				if any(part.strip() for part in textParts):
					trailingIndexes.append(item.index)
				else:
					events.append(("index", item.index, 0, ()))
			elif isinstance(item, PitchCommand):
				flushText()
				pitchOffset = int(getattr(item, "offset", 0) or 0)
		flushText()
		return events

	def speak(self, speechSequence):
		events = self._buildEvents(speechSequence)
		if not events:
			return
		with self._stateLock:
			token = self._token
			settings = (self._voice, self._rate, self._pitch, self._volume)
		self._jobs.put((token, events, settings))

	def cancel(self):
		with self._stateLock:
			self._token += 1
			try:
				while True:
					self._jobs.get_nowait()
					self._jobs.task_done()
			except queue.Empty:
				pass
		with self._hostLock:
			host = self._synthesizingHost
			if not self._stopping.is_set() and host is not None and host.isRunning():
				if host is self._host:
					self._host = self._standbyHost
					self._standbyHost = None
				startRetirement = host not in self._retiringHosts
				self._retiringHosts.add(host)
			else:
				host = None
				startRetirement = False
			log.debug(
				"Samsung Galaxy Voices: cancel; "
				f"busyPid={getattr(host, 'pid', None)}; "
				f"currentPid={getattr(self._host, 'pid', None)}; "
				f"standbyPid={getattr(self._standbyHost, 'pid', None)}; "
				f"retiring={len(self._retiringHosts)}"
			)
		if startRetirement:
			threading.Thread(
				target=self._recoverOrRetireHost,
				args=(host, self._voice),
				name="Samsung Galaxy retiring host",
				daemon=True,
			).start()
			player, activeDone = self._replacePlayer(createReplacement=not self._stopping.is_set())
			self._retirePlayer(player, activeDone)
		else:
			with self._playerLock:
				player = self._player
			try:
				if player is not None:
					player.stop()
			except Exception:
				log.debugWarning("Samsung Galaxy Voices: audio cancellation failed", exc_info=True)

	def _startHostWarmup(self, host, voice):
		threading.Thread(
			target=self._warmHost,
			args=(host, voice),
			name="Samsung Galaxy standby host",
			daemon=True,
		).start()

	def _warmHostPair(self, voice):
		with self._hostLock:
			hosts = (self._host, self._standbyHost)
		for host in hosts:
			if host is not None:
				self._startHostWarmup(host, voice)

	def _ensureStandbyHost(self, voice):
		with self._hostLock:
			if (
				self._stopping.is_set()
				or voice != self._voice
				or self._standbyHost is not None
				or self._retiringHosts
			):
				return
			host = _SamsungHost()
			self._standbyHost = host
		self._startHostWarmup(host, voice)

	def _replaceHostPair(self, voice, stopActive=False):
		with self._hostAvailable:
			oldHosts = {self._host, self._standbyHost, *self._retiringHosts}
			activeHost = self._activeHost
			self._host = _SamsungHost()
			self._standbyHost = _SamsungHost()
			self._retiringHosts = set()
			newHosts = (self._host, self._standbyHost)
			self._hostAvailable.notify_all()
		for host in oldHosts:
			if host is None or (host is activeHost and not stopActive):
				continue
			if stopActive:
				host.abort()
			else:
				threading.Thread(
					target=host.abort,
					name="Samsung Galaxy old-voice host",
					daemon=True,
				).start()
		for host in newHosts:
			self._startHostWarmup(host, voice)

	def _recoverOrRetireHost(self, host, voice):
		startedAt = time.monotonic()
		pid = host.pid
		if host.supportsCooperativeCancel:
			host.requestCancel()
			expectedTerminal = b"C"
			terminalTimeout = 5.0
		else:
			# Stop presenting the obsolete audio immediately, but do not invoke the
			# newer Samsung engine's unsafe stop operation. It can finish silently
			# and the already-warm process can then serve a later request.
			host.abandonRequest()
			expectedTerminal = b"D"
			terminalTimeout = 30.0
		deadline = time.monotonic() + 1.0
		while time.monotonic() < deadline:
			with self._hostLock:
				if self._synthesizingHost is not host:
					break
			time.sleep(0.005)
		with self._hostLock:
			consumerReleased = self._synthesizingHost is not host
		terminal = consumerReleased and host.waitForTerminal(expectedTerminal, terminalTimeout)
		deadline = time.monotonic() + 0.25
		while terminal and time.monotonic() < deadline:
			with self._hostLock:
				if self._synthesizingHost is not host:
					break
			time.sleep(0.005)
		with self._hostLock:
			reusable = terminal and self._synthesizingHost is not host and host.isRunning()
		assigned = False
		assignedSlot = None
		with self._hostAvailable:
			wasRetiring = host in self._retiringHosts
			self._retiringHosts.discard(host)
			if (
				reusable
				and wasRetiring
				and not self._stopping.is_set()
				and voice == self._voice
			):
				host.prepareReuse()
				if self._host is None:
					self._host = host
					assigned = True
					assignedSlot = "current"
				elif self._standbyHost is None and host is not self._host:
					self._standbyHost = host
					assigned = True
					assignedSlot = "standby"
			self._hostAvailable.notify_all()
		if assigned:
			log.debug(
				f"Samsung Galaxy Voices: interrupted host recovered; pid={pid}; slot={assignedSlot}; "
				f"terminal={expectedTerminal.decode('ascii')}; "
				f"elapsedMs={(time.monotonic() - startedAt) * 1000:.1f}"
			)
		else:
			host.abort()
			log.debug(
				f"Samsung Galaxy Voices: unresponsive host retired; pid={pid}; "
				f"terminal={terminal}; expected={expectedTerminal.decode('ascii')}; reusable={reusable}; "
				f"elapsedMs={(time.monotonic() - startedAt) * 1000:.1f}"
			)
			self._ensureStandbyHost(voice)

	def _warmHost(self, host, voice):
		try:
			if self._stopping.is_set():
				return
			host.start(voice)
			with self._hostLock:
				keepHost = not self._stopping.is_set() and host in (
					self._host, self._standbyHost, *self._retiringHosts,
				)
			if not keepHost:
				host.abort()
		except Exception:
			if not self._stopping.is_set():
				log.debugWarning("Samsung Galaxy Voices: replacement helper warm-up failed", exc_info=True)

	def _getHost(self, voice):
		startedAt = time.monotonic()
		created = False
		replacedHost = None
		with self._hostAvailable:
			# Rapid navigation can briefly leave both warm helpers unwinding a
			# cancelled request. Prefer a bounded wait for either one over a cold
			# emulator startup, which is considerably slower.
			recoveryWaitSeconds = (
				_S24_HOST_RECOVERY_WAIT_SECONDS
				if any(
					host.engineVersion > _LEGACY_CANCEL_ENGINE_VERSION_MAX
					for host in self._retiringHosts
				)
				else _LEGACY_HOST_RECOVERY_WAIT_SECONDS
			)
			deadline = time.monotonic() + recoveryWaitSeconds
			while self._host is None and self._retiringHosts and not self._stopping.is_set():
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._hostAvailable.wait(remaining)
			if self._host is None:
				# A slow cancellation must not grow the emulator pool on every key.
				# The single synthesis worker has already released these consumers.
				if len(self._retiringHosts) >= _MAX_WARM_HOSTS:
					replacedHost = self._retiringHosts.pop()
				self._host = _SamsungHost()
				created = True
			host = self._host
		if replacedHost is not None:
			log.warning(
				f"Samsung Galaxy Voices: recovery pool full; retiring pid={replacedHost.pid}; voice={voice}"
			)
			replacedHost.abort()
		if self._stopping.is_set():
			raise _HostError("The Samsung synthesizer is stopping.")
		host.start(voice)
		if self._stopping.is_set():
			host.abort()
			raise _HostError("The Samsung synthesizer is stopping.")
		message = (
			f"Samsung Galaxy Voices: host selected; pid={host.pid}; cold={created}; "
			f"waitMs={(time.monotonic() - startedAt) * 1000:.1f}"
		)
		if created:
			log.info(message)
		else:
			log.debug(message)
		return host

	def _replacePlayer(self, createReplacement):
		with self._playerLock:
			player = self._player
			activeDone = self._activePlayerDone if self._activePlayer is player else None
			self._player = _makePlayer() if createReplacement else None
			return player, activeDone

	@staticmethod
	def _retirePlayer(player, activeDone):
		if player is None:
			return
		try:
			player.stop()
		except Exception:
			log.debugWarning("Samsung Galaxy Voices: audio cancellation failed", exc_info=True)

		def finishRetiring():
			# A feed already inside NVDA's native audio layer can resume after stop.
			# Keep stopping only the retired stream until its render relinquishes it.
			deadline = time.monotonic() + 2
			while activeDone is not None and not activeDone.wait(0.01) and time.monotonic() < deadline:
				try:
					player.stop()
				except Exception:
					break
			try:
				player.close()
			except Exception:
				pass

		threading.Thread(
			target=finishRetiring,
			name="Samsung Galaxy retired audio",
			daemon=True,
		).start()

	def pause(self, switch):
		with self._playerLock:
			player = self._player
		try:
			if player is not None:
				player.pause(switch)
		except Exception:
			log.debugWarning("Samsung Galaxy Voices: audio pause failed", exc_info=True)

	def terminate(self):
		self._stopping.set()
		self.cancel()
		self._jobs.put(None)
		with self._hostAvailable:
			hosts = {self._host, self._standbyHost, self._activeHost, *self._retiringHosts}
			self._retiringHosts = set()
			self._hostAvailable.notify_all()
		for host in hosts:
			if host is not None:
				host.abort()
		self._workerThread.join(timeout=3)
		super().terminate()

	def _worker(self):
		while not self._stopping.is_set():
			job = self._jobs.get()
			if job is None:
				self._jobs.task_done()
				return
			try:
				if self._isCurrent(job[0]):
					self._render(*job)
			except Exception:
				if self._isCurrent(job[0]):
					log.error("Samsung Galaxy Voices synthesis failed", exc_info=True)
					try:
						with self._playerLock:
							if self._player is not None and self._isCurrent(job[0]):
								self._player.stop()
					except Exception:
						log.debugWarning("Samsung Galaxy Voices: failed-request audio cleanup failed", exc_info=True)
					finally:
						# A failed request must not leave NVDA waiting forever for completion.
						self._notifyDone(job[0])
				else:
					log.debug("Samsung Galaxy Voices: obsolete request ended during cancellation")
			finally:
				self._jobs.task_done()

	def _discardFailedHost(self, host, voice, stage):
		pid = host.pid
		with self._hostAvailable:
			if self._host is host:
				self._host = self._standbyHost
				self._standbyHost = None
			elif self._standbyHost is host:
				self._standbyHost = None
			self._retiringHosts.discard(host)
			self._hostAvailable.notify_all()
		log.warning(
			f"Samsung Galaxy Voices: failed helper discarded; pid={pid}; voice={voice}; "
			f"engineVersion={host.engineVersion}; stage={stage}; "
			f"diagnostics={'; '.join(host._diagnostics)}"
		)
		host.abort()

	def _setHostParameters(self, host, rate, pitch):
		host.send(b"P", _PARAMETERS.pack(self._nativeRate(rate), self._nativeValue(pitch)))
		kind, payload = host.getMessage(5)
		if kind != b"K":
			raise _HostError(payload.decode("utf-8", "replace") or "Samsung rejected the speech settings.")

	def _synthesize(self, host, token, text, volume, player):
		startedAt = time.monotonic()
		firstAudioAt = None
		bufferTarget = round(host.sampleRate * 2 * self._playbackBufferMs / 1000)
		initialAudio = bytearray()
		log.debug(
			f"Samsung Galaxy Voices: synthesis start; pid={host.pid}; token={token}; chars={len(text)}"
		)
		with self._hostLock:
			if not self._isCurrent(token):
				return False
			self._synthesizingHost = host
		try:
			host.beginRequest()
			host.send(b"S", text.encode("utf-8"))
			lastProgress = time.monotonic()
			while True:
				try:
					kind, payload = host.getMessage(0.25)
				except _HostError:
					if time.monotonic() - lastProgress > 30:
						raise
					continue
				lastProgress = time.monotonic()
				if kind is None:
					if not self._isCurrent(token):
						return False
					raise _HostError(payload.decode("utf-8", "replace"))
				if kind == b"A":
					if firstAudioAt is None:
						firstAudioAt = time.monotonic()
						log.debug(
							f"Samsung Galaxy Voices: first audio; pid={host.pid}; token={token}; "
							f"latencyMs={(firstAudioAt - startedAt) * 1000:.1f}"
						)
					if self._isCurrent(token):
						if bufferTarget:
							initialAudio.extend(payload)
							if len(initialAudio) >= bufferTarget:
								player.feed(self._scaleVolume(initialAudio, volume))
								initialAudio.clear()
								bufferTarget = 0
						else:
							player.feed(self._scaleVolume(payload, volume))
				elif kind == b"D":
					if initialAudio and self._isCurrent(token):
						player.feed(self._scaleVolume(initialAudio, volume))
					log.debug(
						f"Samsung Galaxy Voices: synthesis done; pid={host.pid}; token={token}; "
						f"elapsedMs={(time.monotonic() - startedAt) * 1000:.1f}"
					)
					return self._isCurrent(token)
				elif kind == b"C":
					return False
				elif kind == b"X":
					return False
				elif kind == b"E":
					raise _HostError(payload.decode("utf-8", "replace"))
		finally:
			with self._hostLock:
				if self._synthesizingHost is host:
					self._synthesizingHost = None

	def _render(self, token, events, settings):
		with self._playerLock:
			player = self._player
			if player is None:
				return
			activeDone = threading.Event()
			self._activePlayer = player
			self._activePlayerDone = activeDone
		voice, rate, pitch, volume = settings
		host = None
		completed = False
		failed = False
		stage = "startup"
		try:
			host = self._getHost(voice)
			with self._hostLock:
				self._activeHost = host
			if not self._isCurrent(token):
				return
			if host.sampleRate != 24000:
				raise _HostError(f"Unsupported Samsung sample rate: {host.sampleRate}")
			for eventType, value, pitchOffset, trailingIndexes in events:
				if not self._isCurrent(token):
					return
				if eventType == "index":
					synthIndexReached.notify(synth=self, index=value)
					continue
				blockPitch = max(0, min(100, pitch + pitchOffset))
				stage = "parameters"
				self._setHostParameters(host, rate, blockPitch)
				stage = "synthesis"
				if not self._synthesize(host, token, value, volume, player):
					return
				if not self._isCurrent(token):
					return
				for index in trailingIndexes:
					synthIndexReached.notify(synth=self, index=index)
			if self._isCurrent(token):
				completed = True
				player.feed(b"", onDone=lambda: self._notifyDone(token))
		except Exception:
			failed = True
			if host is not None:
				self._discardFailedHost(host, voice, stage)
			raise
		finally:
			activeDone.set()
			with self._playerLock:
				if self._activePlayerDone is activeDone:
					self._activePlayer = None
					self._activePlayerDone = None
			with self._hostLock:
				if self._activeHost is host:
					self._activeHost = None
				orphaned = host is not None and host not in (
					self._host, self._standbyHost, *self._retiringHosts,
				)
			if orphaned and not failed:
				host.abort()
			if completed or failed:
				self._ensureStandbyHost(voice)

	def _notifyDone(self, token):
		if self._isCurrent(token):
			synthDoneSpeaking.notify(synth=self)
