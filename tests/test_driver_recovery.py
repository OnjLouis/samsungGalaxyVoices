"""Exercise helper failures without NVDA, downloaded voices, or audio devices."""

import importlib.util
import io
from pathlib import Path
import queue
import struct
import sys
import threading
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_driver():
	class BaseSynth:
		@staticmethod
		def VoiceSetting(**kwargs): pass
		RateSetting = PitchSetting = VolumeSetting = VoiceSetting

	store = types.ModuleType("synthDrivers._samsungGalaxyVoices.voiceStore")
	store.loadVoiceDefinitions = lambda: {}
	class Notification:
		def notify(self, **kwargs): pass
	modules = {
		"addonHandler": types.SimpleNamespace(initTranslation=lambda: None),
		"config": types.SimpleNamespace(conf={"audio": {"outputDevice": "default"}}),
		"logHandler": types.SimpleNamespace(log=types.SimpleNamespace(
			debug=lambda *a, **k: None, debugWarning=lambda *a, **k: None,
			warning=lambda *a, **k: None, error=lambda *a, **k: None,
			info=lambda *a, **k: None,
		)),
		"nvwave": types.SimpleNamespace(),
		"speech.commands": types.SimpleNamespace(IndexCommand=type("IndexCommand", (), {}),
			PitchCommand=type("PitchCommand", (), {})),
		"synthDriverHandler": types.SimpleNamespace(SynthDriver=BaseSynth, VoiceInfo=object,
			synthDoneSpeaking=Notification(), synthIndexReached=Notification()),
		"synthDrivers._samsungGalaxyVoices": types.SimpleNamespace(voiceStore=store),
	}
	with patch.dict(sys.modules, modules):
		spec = importlib.util.spec_from_file_location("galaxyRecoveryUnderTest",
			ROOT / "addon" / "synthDrivers" / "samsungGalaxyVoices.py")
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	return module


class Player:
	def __init__(self):
		self.audio = bytearray()
		self.stopped = False

	def stop(self): self.stopped = True

	def feed(self, data, onDone=None):
		self.audio.extend(data)
		if onDone:
			onDone()


class FakeHost:
	def __init__(self, module, failure=None):
		self.module = module
		self.failure = failure
		self.pid = 123
		self.sampleRate = 24000
		self.engineVersion = 312347000
		self.supportsCooperativeCancel = True
		self.messages = queue.Queue()
		self.aborted = False
		self._diagnostics = []

	def start(self, voice): pass
	def beginRequest(self): pass
	def isRunning(self, voice=None): return not self.aborted
	def abort(self): self.aborted = True

	def send(self, kind, payload=b""):
		if kind == b"P":
			if self.failure == "parameters":
				raise self.module._HostError("parameter timeout")
			self.messages.put((b"K", b""))
		elif kind == b"S":
			if self.failure == "synthesis":
				self.messages.put((None, b"helper disconnected"))
			elif self.failure == "silent":
				self.messages.put((b"D", struct.pack("<Q", 0)))
			else:
				self.messages.put((b"A", b"\x00\x01" * 20))
				self.messages.put((b"D", b""))

	def getMessage(self, timeout=30): return self.messages.get_nowait()


class DriverRecoveryTests(unittest.TestCase):
	def setUp(self):
		self.module = load_driver()

	def test_cancel_racing_natural_completion_keeps_helper_reusable(self):
		host = self.module._SamsungHost()
		host._terminalKind = b"D"
		host._terminalEvent.set()
		self.assertTrue(host.waitForTerminal(b"C", 0), "completed helper is mistaken for a failed cancellation")

	def test_cancellation_does_not_accept_failure_as_completion(self):
		for terminal in (b"E", None, b"K"):
			with self.subTest(terminal=terminal):
				host = self.module._SamsungHost()
				host._terminalKind = terminal
				host._terminalEvent.set()
				self.assertFalse(host.waitForTerminal(b"C", 0))

	def driver(self, host, standby=None):
		driver = self.module.SynthDriver.__new__(self.module.SynthDriver)
		driver._voice = "test"
		driver._token = 1
		driver._stopping = threading.Event()
		driver._stateLock = threading.Lock()
		driver._hostLock = threading.RLock()
		driver._hostAvailable = threading.Condition(driver._hostLock)
		driver._host = host
		driver._standbyHost = standby
		driver._retiringHosts = set()
		driver._activeHost = driver._synthesizingHost = None
		driver._playerLock = threading.Lock()
		driver._player = Player()
		driver._playbackBufferMs = 0
		driver._ensureStandbyHost = lambda voice: None
		return driver

	def test_failed_helper_is_not_reused_on_next_speech(self):
		for failure in ("parameters", "synthesis"):
			with self.subTest(failure=failure):
				failed = FakeHost(self.module, failure)
				standby = FakeHost(self.module)
				driver = self.driver(failed, standby)
				with self.assertRaises(self.module._HostError):
					driver._render(1, [("text", "First", 0, ())], ("test", 50, 50, 100))
				self.assertTrue(failed.aborted, "failed helper remains available")
				self.assertIs(driver._host, standby)
				driver._render(1, [("text", "Next", 0, ())], ("test", 50, 50, 100))
				self.assertTrue(driver._player.audio)

	def test_silent_success_finishes_without_discarding_helper(self):
		host = FakeHost(self.module, "silent")
		driver = self.driver(host)
		with patch.object(self.module.synthDoneSpeaking, "notify") as notify:
			driver._render(1, [("text", "?", 0, ())], ("test", 50, 50, 100))
			notify.assert_called_once_with(synth=driver)
		self.assertFalse(host.aborted)
		self.assertIs(driver._host, host)

	def test_terminal_publication_finishes_before_next_request(self):
		host = self.module._SamsungHost()
		queued = threading.Event()
		release = threading.Event()
		finish = threading.Event()

		class DelayedQueue(queue.Queue):
			def put(self, item, *args, **kwargs):
				super().put(item, *args, **kwargs)
				if item[0] == b"D":
					queued.set()
					release.wait(2)

		class Stream(io.BytesIO):
			def read(self, size):
				if self.tell() == len(self.getvalue()):
					finish.wait(2)
				return super().read(size)

		process = types.SimpleNamespace(stdout=Stream(b"D" + struct.pack("<I", 0)))
		host._process = process
		host._messages = DelayedQueue()
		reader = threading.Thread(target=host._reader, args=(process,))
		reader.start()
		self.assertTrue(queued.wait(1))
		beganNext = threading.Event()

		def consume():
			host.getMessage(1)
			host.beginRequest()
			beganNext.set()

		consumer = threading.Thread(target=consume)
		consumer.start()
		early = beganNext.wait(0.05)
		release.set()
		consumer.join(1)
		stale = host._terminalEvent.is_set()
		host._process = None
		finish.set()
		reader.join(1)
		self.assertFalse(early, "next request overtook terminal publication")
		self.assertFalse(stale, "old completion leaked into the next request")

	def test_slow_cancellations_do_not_grow_helper_pool(self):
		retiring = {FakeHost(self.module), FakeHost(self.module)}
		driver = self.driver(None)
		driver._retiringHosts = retiring.copy()
		fresh = FakeHost(self.module)
		with patch.object(self.module, "_SamsungHost", return_value=fresh), \
			patch.object(self.module, "_LEGACY_HOST_RECOVERY_WAIT_SECONDS", 0):
			self.assertIs(driver._getHost("test"), fresh)
		self.assertEqual(1, sum(host.aborted for host in retiring))
		self.assertEqual(1, len(driver._retiringHosts))

	def test_warmup_cannot_leave_an_obsolete_helper_running(self):
		host = FakeHost(self.module)
		driver = self.driver(None)
		driver._warmHost(host, "test")
		self.assertTrue(host.aborted)

	def test_finished_old_voice_helper_is_closed(self):
		host = FakeHost(self.module)
		driver = self.driver(FakeHost(self.module))
		driver._getHost = lambda voice: host
		driver._render(1, [("text", "Old voice", 0, ())], ("previous", 50, 50, 100))
		self.assertTrue(host.aborted)

	def test_failure_releases_nvda_speech_queue(self):
		driver = self.driver(FakeHost(self.module))
		driver._jobs = queue.Queue()
		driver._jobs.put((1, [], ("test", 50, 50, 100)))
		driver._jobs.put(None)
		driver._render = lambda *args: (_ for _ in ()).throw(self.module._HostError("failed"))
		with patch.object(self.module.synthDoneSpeaking, "notify") as notify:
			driver._worker()
			notify.assert_called_once_with(synth=driver)
		self.assertTrue(driver._player.stopped)

	def test_stopping_a_full_queue_releases_its_reader(self):
		host = self.module._SamsungHost()
		entered = threading.Event()
		class FullQueue(queue.Queue):
			def put(self, item, *args, **kwargs):
				if item[0] == b"D":
					entered.set()
				return super().put(item, *args, **kwargs)
		host._messages = FullQueue(maxsize=2)
		host._messages.put((b"A", b"audio"))
		host._messages.put((b"A", b"audio"))
		process = types.SimpleNamespace(stdout=io.BytesIO(b"D" + struct.pack("<I", 0)),
			stdin=None, stderr=None, poll=lambda: 0)
		host._process = process
		reader = threading.Thread(target=host._reader, args=(process,))
		reader.start()
		self.assertTrue(entered.wait(1))
		host.stop()
		reader.join(0.2)
		stuck = reader.is_alive()
		if stuck:
			host._messages.get_nowait()
			reader.join(1)
		self.assertFalse(stuck, "stopped reader is still blocked on its abandoned queue")

	def test_cancelled_failure_does_not_complete_new_speech(self):
		driver = self.driver(FakeHost(self.module))
		driver._jobs = queue.Queue()
		driver._jobs.put((1, [], ("test", 50, 50, 100)))
		driver._jobs.put(None)
		def failAfterCancel(*args):
			driver._token = 2
			raise self.module._HostError("cancelled")
		driver._render = failAfterCancel
		with patch.object(self.module.synthDoneSpeaking, "notify") as notify:
			driver._worker()
			notify.assert_not_called()
		self.assertFalse(driver._player.stopped)

	def test_audio_cleanup_failure_does_not_kill_worker(self):
		driver = self.driver(FakeHost(self.module))
		driver._jobs = queue.Queue()
		for _ in range(2):
			driver._jobs.put((1, [], ("test", 50, 50, 100)))
		driver._jobs.put(None)
		driver._render = lambda *args: (_ for _ in ()).throw(self.module._HostError("failed"))
		driver._player.stop = lambda: (_ for _ in ()).throw(RuntimeError("audio unavailable"))
		with patch.object(self.module.synthDoneSpeaking, "notify") as notify:
			driver._worker()
			self.assertEqual(2, notify.call_count)
		self.assertEqual(0, driver._jobs.unfinished_tasks)

	def test_timeout_reports_bounded_helper_state(self):
		host = self.module._SamsungHost()
		host._process = types.SimpleNamespace(pid=456, poll=lambda: None)
		host._voice = "test_voice"
		with self.assertRaises(self.module._HostError) as raised:
			host.getMessage(0)
		for detail in ("pid=456", "voice=test_voice", "queuedFrames=0", "lastFrame=None"):
			self.assertIn(detail, str(raised.exception))


if __name__ == "__main__":
	unittest.main()
