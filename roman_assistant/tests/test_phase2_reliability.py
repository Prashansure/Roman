import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from audio.microphone import Microphone
from audio.whisper import Transcriber
from core.assistant import Assistant
from core.instance_guard import SingleInstanceGuard
from core.json_io import write_json_atomic
from llm.ollama_client import OllamaClient
from memory.memory_manager import MemoryManager
from speech.tts import Speech


class JsonAndMemoryTests(unittest.TestCase):
    def test_atomic_json_write_and_concurrent_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            write_json_atomic(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

            memory = MemoryManager(Path(tmp) / "memory")
            session = memory.start_new_conversation()
            threads = [
                threading.Thread(target=memory.add_user_message, args=(f"message {i}", session))
                for i in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            records = memory.list_conversations()
            self.assertEqual(len(records[0]["messages"]), 12)
            self.assertEqual(len(memory.recent_history), 12)


class OllamaTests(unittest.TestCase):
    def test_cancelled_stream_closes_response(self):
        cancel = threading.Event()

        class Response:
            def __init__(self):
                self.index = 0
                self.closed = False

            def __iter__(self):
                return self

            def __next__(self):
                self.index += 1
                if self.index == 2:
                    cancel.set()
                return {"message": {"content": str(self.index)}}

            def close(self):
                self.closed = True

        response = Response()
        client = OllamaClient(timeout=12)
        client._client = SimpleNamespace(chat=lambda **_kwargs: response)
        self.assertEqual(list(client.stream("hello", cancel_event=cancel)), ["1"])
        self.assertTrue(response.closed)

    def test_client_uses_configured_request_timeout(self):
        client = OllamaClient(timeout=17)
        fake_module = SimpleNamespace(Client=lambda **kwargs: kwargs)
        with patch.dict("sys.modules", {"ollama": fake_module}):
            self.assertEqual(client._get_client(), {"timeout": 17})


class InstanceGuardTests(unittest.TestCase):
    def test_only_one_guard_with_the_same_name_acquires(self):
        import uuid

        name = f"Local\\RomanAssistant.Test.{uuid.uuid4()}"
        first, second = SingleInstanceGuard(name), SingleInstanceGuard(name)
        third = None
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            third = SingleInstanceGuard(name)
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            if third:
                third.release()


class AssistantTests(unittest.TestCase):
    def test_user_is_saved_and_empty_assistant_reply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryManager(Path(tmp) / "memory")

            class EmptyLLM:
                def stream(self, *_args, **_kwargs):
                    return iter(())

                def extract_memories(self, *_args):
                    self.fail("memory extraction must not run for an empty response")

                def cancel(self):
                    pass

                def close(self):
                    pass

            assistant = Assistant(memory=memory, llm=EmptyLLM(), speech=Speech())
            self.assertFalse(assistant.send_message("   "))
            self.assertTrue(assistant.send_message("Keep this message"))
            # Run response directly to keep this test deterministic and hardware-free.
            for worker in tuple(assistant._workers):
                worker.join(timeout=2)
            conversations = memory.list_conversations()
            saved = conversations[0]["messages"]
            self.assertEqual(saved[0]["role"], "user")
            self.assertEqual(saved[0]["content"], "Keep this message")
            self.assertEqual(len(saved), 1)
            assistant.shutdown()


class AudioCleanupTests(unittest.TestCase):
    def test_microphone_closes_stream_and_audio_after_capture_error(self):
        class Stream:
            stopped = False
            closed = False

            def read(self, *_args, **_kwargs):
                raise RuntimeError("mock capture failure")

            def stop_stream(self):
                self.stopped = True

            def close(self):
                self.closed = True

        class Audio:
            terminated = False

            def open(self, **_kwargs):
                return stream

            def get_sample_size(self, _format):
                return 2

            def terminate(self):
                self.terminated = True

        microphone = Microphone.__new__(Microphone)
        microphone._pyaudio = SimpleNamespace(paInt16=8)
        stream = Stream()
        microphone._audio = Audio()
        microphone._stream = None
        microphone._stop = threading.Event()
        microphone._thread = None
        microphone._lock = threading.Lock()
        result = []
        microphone._record(lambda path, error: result.append((path, error)))
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertTrue(microphone._audio.terminated)
        self.assertIsNone(result[0][0])
        self.assertIn("mock capture failure", result[0][1])

    def test_cuda_inference_failure_retries_on_cpu(self):
        calls = []

        class Model:
            def __init__(self, _name, device, compute_type):
                calls.append((device, compute_type))
                self.device = device

            def transcribe(self, *_args, **_kwargs):
                if self.device == "cuda":
                    raise RuntimeError("mock CUDA failure")
                return ([SimpleNamespace(text="recovered")], None)

        transcriber = Transcriber()
        transcriber._model = Model("base", "cuda", "float16")
        transcriber.active_device = "cuda"
        transcriber.active_compute_type = "float16"
        with patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=Model)}):
            self.assertEqual(transcriber.transcribe("fake.wav"), "recovered")
        self.assertEqual(calls, [("cuda", "float16"), ("cpu", "int8")])
        self.assertEqual(transcriber.active_device, "cpu")
        self.assertIn("retrying on CPU", transcriber.notice)


class SpeechLoggingTests(unittest.TestCase):
    def test_tts_start_failure_is_logged(self):
        speech = Speech()
        speech.prepare()
        with patch("speech.tts.os.name", "nt"), patch(
            "speech.tts.subprocess.Popen", side_effect=OSError("mock SAPI failure")
        ), self.assertLogs("speech.tts", level=logging.ERROR):
            speech.speak("A test sentence.")


if __name__ == "__main__":
    unittest.main()
