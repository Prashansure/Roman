from config.settings import OLLAMA_MODEL

SYSTEM_PROMPT = """You are ROMAN Ⅰ, a personal AI assistant.
Answer conversationally and clearly, in a natural spoken style.
Give the actual explanation without asking whether the user wants you to continue.
Keep answers reasonably concise because they may be spoken aloud."""


class OllamaClient:
    def ask(self, query: str) -> str:
        import ollama

        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
        )
        return response["message"]["content"].strip()
