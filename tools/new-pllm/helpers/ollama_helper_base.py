# Base class for LLM interaction via Ollama (or OpenAI).
# Handles model initialisation and basic file reading.

from langchain_community.chat_models import ChatOllama
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
import os


class OllamaHelperBase:
    """Thin wrapper that selects the right LangChain chat model
    (Ollama or OpenAI) based on the model name."""

    def __init__(self, base_url="http://localhost:11434", model="llama3",
                 temp=0.7, logging=False) -> None:
        self.logging = logging
        if "gpt" in model:
            load_dotenv()
            api_key = os.getenv("OPENAI_KEY")
            self.model = ChatOpenAI(model=model, api_key=api_key, temperature=temp)
        else:
            self.model = ChatOllama(
                base_url=base_url, model=model, format="json", temperature=temp
            )

    def read_python_file(self, filepath):
        """Return the contents of *filepath* as a single string (newlines removed)."""
        with open(filepath, "r") as fh:
            return fh.read().replace("\n", "")

