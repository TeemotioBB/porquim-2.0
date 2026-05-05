import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    EVOLUTION_API_URL: str = os.environ["EVOLUTION_API_URL"]
    EVOLUTION_API_KEY: str = os.environ["EVOLUTION_API_KEY"]
    EVOLUTION_INSTANCE: str = os.environ["EVOLUTION_INSTANCE"]

    GROK_API_KEY: str = os.environ["GROK_API_KEY"]

    # PostgreSQL (Railway fornece DATABASE_URL automaticamente)
    DATABASE_URL: str = os.environ["DATABASE_URL"]

    # OpenAI Whisper para transcrição de áudio
    OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]

    PORT: int = int(os.environ.get("PORT", 8080))
    RESET_SECRET: str = os.environ.get("RESET_SECRET", "johnny-reset-2026")

    # WhatsApp de suporte (apenas dígitos, com DDI). Default: 31991316890 (BR).
    SUPPORT_WHATSAPP: str = os.environ.get("SUPPORT_WHATSAPP", "5531991316890")

settings = Settings()
