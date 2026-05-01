from dotenv import load_dotenv
import os
from pydantic_settings import BaseSettings

load_dotenv()

class Settings(BaseSettings):
    EVOLUTION_API_URL: str = os.getenv("EVOLUTION_API_URL")
    EVOLUTION_API_KEY: str = os.getenv("EVOLUTION_API_KEY")
    EVOLUTION_INSTANCE: str = os.getenv("EVOLUTION_INSTANCE")
    GROK_API_KEY: str = os.getenv("GROK_API_KEY")
    REDIS_URL: str = os.getenv("REDIS_URL")
    PORT: int = int(os.getenv("PORT", 3000))

settings = Settings()
