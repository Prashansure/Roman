"""Lazy Faster-Whisper loading with the source project's CUDA setup."""

import os

from config.settings import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE, WHISPER_MODEL


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self.notice = ""
        self.active_device = None
        self.active_compute_type = None
        self._dll_directories = []

    def _load(self) -> None:
        if self._model is not None:
            return
        device, compute = WHISPER_DEVICE, WHISPER_COMPUTE_TYPE
        if device == "cuda":
            try:
                import nvidia.cublas
                import nvidia.cudnn

                cublas_path = os.path.join(nvidia.cublas.__path__[0], "bin")
                cudnn_path = os.path.join(nvidia.cudnn.__path__[0], "bin")
                self._dll_directories.extend((os.add_dll_directory(cublas_path), os.add_dll_directory(cudnn_path)))
                os.environ["PATH"] = cublas_path + os.pathsep + cudnn_path + os.pathsep + os.environ["PATH"]
            except Exception as exc:
                self.notice = f"CUDA libraries unavailable; using CPU transcription ({exc})."
                device, compute = "cpu", "int8"
        from faster_whisper import WhisperModel

        try:
            self._model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)
            self.active_device, self.active_compute_type = device, compute
        except Exception as exc:
            if device != "cuda":
                raise
            self.notice = f"CUDA transcription unavailable; using CPU ({exc})."
            self._model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            self.active_device, self.active_compute_type = "cpu", "int8"

    def transcribe(self, filename: str) -> str:
        self._load()
        try:
            segments, _ = self._model.transcribe(filename, beam_size=5, vad_filter=True)
            text = " ".join(segment.text for segment in segments).strip()
        except Exception as exc:
            if self.active_device != "cuda":
                raise
            from faster_whisper import WhisperModel

            self.notice = f"GPU transcription failed; retrying on CPU ({exc})."
            self._model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            self.active_device, self.active_compute_type = "cpu", "int8"
            segments, _ = self._model.transcribe(filename, beam_size=5, vad_filter=True)
            text = " ".join(segment.text for segment in segments).strip()
        if not any(character.isalnum() for character in text):
            return ""
        return text
