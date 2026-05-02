import json
import hashlib
import base64
import httpx
from datetime import date
from openai import AsyncOpenAI
from src.core.config import settings

# Cliente Grok (xAI) para extração de dados
grok = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1"
)

# Cliente OpenAI para Whisper (áudio) e visão (foto)
openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

CATEGORIAS = "Alimentação, Transporte, Vestuário, Moradia, Saúde, Educação, Lazer, Outros"
HOJE = str(date.today())

PROMPT_EXTRACAO = """
Você é o Porquim, assistente financeiro brasileiro.
Analise a mensagem e extraia em JSON:
- valor: número float (ex: 56.00)
- descricao: descrição limpa do gasto
- categoria: uma de ({categorias})
- forma_pagamento: Pix, Cartão, Dinheiro ou Desconhecido
- data: data no formato DD-MM-YYYY. Hoje é {hoje}. Se disser "ontem" use o dia anterior, "semana passada" use 7 dias atrás, etc. Se não mencionar data use {hoje}.

Mensagem: {texto}
Responda APENAS com JSON válido, sem markdown.
""".strip()

def _gerar_hashtag(texto: str) -> str:
    return "#" + hashlib.md5(texto.encode()).hexdigest()[:6]


async def processar_gasto_texto(texto: str) -> dict:
    """Extrai dados de gasto de uma mensagem de texto."""
    prompt = PROMPT_EXTRACAO.format(
        categorias=CATEGORIAS, hoje=HOJE, texto=texto
    )
    resp = await grok.chat.completions.create(
        model="grok-4-1-fast-non-reasoning",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content.strip()
    # Remove possíveis blocos markdown caso o modelo insira
    raw = raw.replace("```json", "").replace("```", "").strip()
    dados = json.loads(raw)
    dados["hashtag"] = _gerar_hashtag(texto)
    return dados


async def transcrever_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Usa Whisper (OpenAI) para transcrever áudio do WhatsApp."""
    if not openai:
        raise RuntimeError("OPENAI_API_KEY não configurada para transcrição de áudio.")

    import tempfile, os
    ext = "ogg" if "ogg" in mime_type else "mp4" if "mp4" in mime_type else "mp3"
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as f:
            transcript = await openai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="pt"
            )
        return transcript.text
    finally:
        os.unlink(tmp_path)


async def processar_gasto_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> dict:
    """Transcreve áudio e extrai dados de gasto."""
    texto = await transcrever_audio(audio_bytes, mime_type)
    dados = await processar_gasto_texto(texto)
    dados["transcricao"] = texto
    return dados


async def processar_comprovante_foto(imagem_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Lê um comprovante/foto e extrai dados de gasto usando visão."""
    if not openai:
        raise RuntimeError("OPENAI_API_KEY não configurada para leitura de fotos.")

    b64 = base64.b64encode(imagem_bytes).decode()
    prompt = f"""
Você é o Porquim, assistente financeiro brasileiro.
Analise esta imagem de comprovante/nota fiscal e extraia em JSON:
- valor: número float total da compra (ex: 56.00)
- descricao: descrição do estabelecimento ou produto principal
- categoria: uma de ({CATEGORIAS})
- forma_pagamento: Pix, Cartão, Dinheiro ou Desconhecido
- data: data no formato DD-MM-YYYY (use {HOJE} se não visível)

Responda APENAS com JSON válido, sem markdown.
"""
    resp = await openai.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
            ]
        }],
        temperature=0.2,
        max_tokens=500
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    dados = json.loads(raw)
    dados["hashtag"] = _gerar_hashtag(dados.get("descricao", "foto"))
    return dados
