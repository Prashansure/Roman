# Roman

**PERSONAL AI ASSISTANT**

Roman is a desktop assistant built from the supplied prototype. It keeps the original Faster-Whisper, Ollama, PyAudio, and Windows SAPI pipeline while separating UI, audio, speech, and orchestration code into modules.

## Run

1. Install Python 3.10 or 3.11 and the dependencies in `requirements.txt`.
2. Make sure Ollama is running and has `llama3.2:3b` available. On Windows, the source prototype expects the microphone input device at index 1.
3. From this directory, run `python main.py`.

The Whisper model loads when the first recording is transcribed. CUDA is used when available, with a CPU `int8` fallback.

## Controls

- **C**, **Ctrl+Shift+M**, or the Voice button: start / stop recording
- **A**: resume speech
- **B**: pause speech
- **E** or **Esc**: stop generation or speech
- **Ctrl+N**: start a new conversation
- Use the window close control to exit Roman
- Press **Enter** to send, or **Shift+Enter** for a new line

The collapsible sidebar starts chats, restores, renames, and deletes local
conversation history, opens appearance and microphone settings, and shows model
runtime information. Replies stream with formatted Markdown and syntax-colored
copyable code blocks. The interface does not offer unconnected tools or attachments.

## Local memory

Roman stores its long-term memory in `memory/memory.json` and dated conversation
logs in `memory/conversations/`. These files stay on this computer. Recent messages
are limited to the last 12 entries in the Ollama prompt; the full conversation is
kept in the dated files and is not copied into `memory.json`. After each answer,
the existing Ollama model makes a separate structured extraction call. Only stable
facts, preferences, and projects are saved, and extraction failures do not stop chat.

## Memory test procedure

1. Start Roman and say or type: `My name is Prashan.` Close and reopen it, then
   ask: `What is my name?` It should answer that your name is Prashan.
2. Tell it: `I am building an AI assistant called Roman.` Restart Roman and
   ask: `What project am I building?` It should retrieve the project from memory.
3. Have a temporary conversation, such as asking about today's weather. Check
   `memory/memory.json` and confirm that the temporary exchange was not saved as
   a permanent memory. Full turns should instead appear in that day's conversation
   file.

The memory files are created automatically the first time Roman starts. An
invalid `memory.json` is preserved as a `memory.corrupt.*.json` recovery copy and
reinitialized with the default structure.
