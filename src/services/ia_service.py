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
# HOJE é calculado dinamicamente em cada chamada para não ficar desatualizado

PROMPT_EXTRACAO = """
Você é o Johnny, assistente financeiro brasileiro.
Analise a mensagem e extraia em JSON:
- valor: número float (ex: 56.00)
- descricao: descrição limpa do gasto
- categoria: uma de ({categorias})
- forma_pagamento: Pix, Cartão, Dinheiro ou Desconhecido
- data: data no formato DD-MM-YYYY. Hoje é {hoje} (use SEMPRE este ano {ano} como referência). Se mencionar um mês (ex: "maio", "janeiro"), use o dia 1 desse mês no ano {ano}. Se disser "ontem" use o dia anterior a hoje. Se não mencionar data use {hoje}.

Mensagem: {texto}
Responda APENAS com JSON válido, sem markdown.
""".strip()

PROMPT_EXTRACAO_ENTRADA = """
Você é o Johnny, assistente financeiro brasileiro.
Analise a mensagem e extraia em JSON uma ENTRADA de dinheiro:
- valor: número float (ex: 1500.00)
- descricao: descrição limpa da entrada
- categoria: uma de (Salário, Freelance, Investimento, Presente, Reembolso, Outros)
- data: data no formato DD-MM-YYYY. Hoje é {hoje}. Se disser "ontem" use o dia anterior. Se não mencionar data use {hoje}.

Mensagem: {texto}
Responda APENAS com JSON válido, sem markdown.
""".strip()

def _gerar_hashtag(texto: str) -> str:
    return "#" + hashlib.md5(texto.encode()).hexdigest()[:6]


async def processar_gasto_texto(texto: str, contexto: str = "") -> dict:
    """Extrai dados de gasto de uma mensagem de texto."""
    hoje_dt = date.today()
    hoje = hoje_dt.strftime("%d-%m-%Y")
    ano = str(hoje_dt.year)
    texto_completo = f"{contexto}\n{texto}".strip() if contexto else texto
    prompt = PROMPT_EXTRACAO.format(
        categorias=CATEGORIAS, hoje=hoje, ano=ano, texto=texto_completo
    )
    resp = await grok.chat.completions.create(
        model="grok-4-1-fast-non-reasoning",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    dados = json.loads(raw)
    dados["hashtag"] = _gerar_hashtag(texto)
    print(f"📦 Gasto extraído: {dados}")
    return dados


async def processar_entrada_texto(texto: str) -> dict:
    """Extrai dados de uma entrada de dinheiro a partir de texto."""
    hoje = str(date.today())
    prompt = PROMPT_EXTRACAO_ENTRADA.format(hoje=hoje, texto=texto)
    resp = await grok.chat.completions.create(
        model="grok-4-1-fast-non-reasoning",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    raw = resp.choices[0].message.content.strip()
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
    """Lê um comprovante/foto e extrai dados de gasto usando visão.

    Retorna um dict com:
      - "modo": "unico" ou "multiplos"
      - se "unico": chaves no nível raiz (descricao, valor, categoria, ...)
      - se "multiplos": chave "itens" com lista de dicts (cada um um gasto),
        e também "total_geral" e "forma_pagamento_geral".

    Para retrocompatibilidade, quando "modo" == "unico" o dict no nível raiz
    contém todos os campos esperados antes (valor, descricao, ...).
    """
    if not openai:
        raise RuntimeError("OPENAI_API_KEY não configurada para leitura de fotos.")

    hoje = str(date.today())
    b64 = base64.b64encode(imagem_bytes).decode()
    prompt = f"""Você é o Johnny, assistente financeiro brasileiro.
Analise esta imagem de comprovante / nota fiscal / cupom fiscal / recibo / extrato e identifique os itens comprados.

REGRA IMPORTANTE:
- Se a imagem listar VÁRIOS ITENS / PRODUTOS distintos (cupom fiscal de mercado, padaria, farmácia, etc), retorne CADA item separadamente.
- Se for um comprovante de um pagamento único (ex: Pix enviado, boleto, fatura, recibo de Uber), retorne UM ÚNICO item.

Responda APENAS com JSON válido, sem markdown, no formato:

{{
  "tipo": "MULTIPLOS" ou "UNICO",
  "forma_pagamento": "Pix" | "Cartão" | "Dinheiro" | "Desconhecido",
  "data": "DD-MM-YYYY (use {hoje} se não visível)",
  "itens": [
    {{
      "descricao": "nome do produto / estabelecimento",
      "valor": número float (ex: 5.90),
      "categoria": uma de ({CATEGORIAS})
    }}
    // ... uma entrada por item se MULTIPLOS, apenas uma se UNICO
  ]
}}

Exemplos:
- Cupom de mercado com Banana 5,90, Vinho 24,90, Sabonete 3,50 →
  tipo MULTIPLOS, itens=[banana 5.90 Alimentação, vinho 24.90 Alimentação, sabonete 3.50 Outros].
- Comprovante de Pix de R$ 100 para um restaurante →
  tipo UNICO, itens=[{{"descricao": "Restaurante X", "valor": 100, "categoria": "Alimentação"}}]

NÃO some os itens. NÃO crie um item "Total" — o total é apenas a soma natural dos itens.
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
        max_tokens=1500,
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)

    tipo = (parsed.get("tipo") or "UNICO").upper()
    forma_pagamento = parsed.get("forma_pagamento") or "Desconhecido"
    data_comprovante = parsed.get("data") or hoje
    itens_raw = parsed.get("itens") or []

    # Sanitização: remove itens sem valor ou descrição, e itens chamados "Total"
    itens_limpos: list[dict] = []
    for it in itens_raw:
        try:
            v = float(it.get("valor", 0))
        except (TypeError, ValueError):
            v = 0
        desc = (it.get("descricao") or "").strip()
        if v <= 0 or not desc:
            continue
        if desc.lower() in ("total", "subtotal", "total geral", "valor total"):
            continue
        cat = it.get("categoria") or "Outros"
        itens_limpos.append({
            "descricao": desc,
            "valor": round(v, 2),
            "categoria": cat,
            "forma_pagamento": forma_pagamento,
            "data": data_comprovante,
            "hashtag": _gerar_hashtag(f"{desc}|{v}|{data_comprovante}"),
        })

    if not itens_limpos:
        # Fallback: nenhum item válido encontrado
        raise ValueError("Não foi possível extrair itens do comprovante.")

    if tipo == "MULTIPLOS" and len(itens_limpos) > 1:
        return {
            "modo": "multiplos",
            "itens": itens_limpos,
            "forma_pagamento_geral": forma_pagamento,
            "data": data_comprovante,
            "total_geral": round(sum(i["valor"] for i in itens_limpos), 2),
        }

    # Caminho de item único — mantém o formato antigo (chaves no nível raiz)
    item = itens_limpos[0]
    return {
        "modo": "unico",
        "descricao": item["descricao"],
        "valor": item["valor"],
        "categoria": item["categoria"],
        "forma_pagamento": forma_pagamento,
        "data": data_comprovante,
        "hashtag": item["hashtag"],
    }


async def classificar_foto_comprovante(imagem_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """
    Analisa a foto e retorna "GASTO" ou "ENTRADA".
    GASTO  = comprovante de pagamento, nota fiscal, boleto pago, Pix enviado.
    ENTRADA = comprovante de recebimento, Pix recebido, transferência recebida, extrato positivo.
    """
    b64 = base64.b64encode(imagem_bytes).decode()
    prompt = (
        "Olhe esta imagem. É um comprovante de PAGAMENTO (dinheiro saindo) "
        "ou de RECEBIMENTO (dinheiro entrando)?\n\n"
        "Responda APENAS com uma palavra: GASTO ou ENTRADA."
    )
    try:
        resp = await openai.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
                ]
            }],
            temperature=0,
            max_tokens=5
        )
        resultado = resp.choices[0].message.content.strip().upper()
        return resultado if resultado in ("GASTO", "ENTRADA") else "GASTO"
    except Exception as e:
        print(f"⚠️ Erro ao classificar foto: {e}")
        return "GASTO"


async def processar_recebimento_foto(imagem_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Lê comprovante de recebimento e extrai dados como entrada de dinheiro."""
    hoje = str(date.today())
    b64 = base64.b64encode(imagem_bytes).decode()
    prompt = f"""Você é o Johnny, assistente financeiro brasileiro.
Analise esta imagem de comprovante de RECEBIMENTO e extraia em JSON:
- valor: número float total recebido (ex: 500.00)
- descricao: descrição da origem (ex: "Pix recebido", "Transferência recebida", nome do pagador se visível)
- categoria: uma de (Salário, Freelance, Investimento, Presente, Reembolso, Outros)
- data: data no formato DD-MM-YYYY (use {hoje} se não visível)

Responda APENAS com JSON válido, sem markdown."""
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
    dados["hashtag"] = _gerar_hashtag(dados.get("descricao", "foto-entrada"))
    return dados
