# ROMAN Ⅰ

**PERSONAL AI ASSISTANT**

ROMAN Ⅰ is a desktop assistant built from the supplied prototype. It keeps the original Faster-Whisper, Ollama, PyAudio, and Windows SAPI pipeline while separating UI, audio, speech, and orchestration code into modules.

## Run

1. Install Python 3.10 or newer and the dependencies in `requirements.txt`.
2. Make sure Ollama is running and has `llama3.2:3b` available. On Windows, the source prototype expects the microphone input device at index 1.
3. From this directory, run `python main.py`.

The Whisper model loads when the first recording is transcribed. CUDA is used when available, with a CPU `int8` fallback.

## Controls

- **C** or the microphone button: start / stop recording
- **A**: resume speech
- **B**: pause speech
- **E**: cancel the current response
- **D**: close ROMAN Ⅰ
- Type a message and press **Enter** to send

Controls are window-scoped and are ignored while the message field has focus.
