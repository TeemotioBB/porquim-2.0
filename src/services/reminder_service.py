"""
Serviço de Lembretes para o MAYCON 🔔
Permite agendar lembretes via WhatsApp com linguagem natural.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import AsyncOpenAI
from src.core.config import settings
from src.core.database import get_pool

_grok = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1",
)

TZ_BR = ZoneInfo("America/Sao_Paulo")


# ─── Banco ────────────────────────────────────────────────────────────────────

async def salvar_lembrete(usuario: str, mensagem: str, horario: datetime) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO lembretes (usuario, mensagem, horario)
            VALUES ($1, $2, $3)
            RETURNING id
        """, usuario, mensagem, horario)
        return row["id"]


async def buscar_lembretes_pendentes(usuario: str) -> list:
    pool = await get_pool()
    agora = datetime.now(TZ_BR)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, mensagem, horario
            FROM lembretes
            WHERE usuario = $1
              AND enviado = FALSE
              AND horario > $2
            ORDER BY horario ASC
            LIMIT 10
        """, usuario, agora)
        return [dict(r) for r in rows]


async def cancelar_lembrete(lembrete_id: int, usuario: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM lembretes WHERE id=$1 AND usuario=$2 AND enviado=FALSE",
            lembrete_id, usuario
        )
        return result != "DELETE 0"


# ─── Detecção ─────────────────────────────────────────────────────────────────

# Palavras-chave explícitas de lembrete
_PADROES_EXPLICITOS = [
    r"\b(me lembre|me lembra|lembra de|lembra que|me avisa|me avise)\b",
    r"\b(lembrete|agendar lembrete|criar lembrete)\b",
    r"\b(me notifica|me notifique)\b",
]

# Padrões implícitos: assunto + referência de data/hora (sem palavra-chave)
_PADROES_IMPLICITOS = [
    # "reunião amanhã 12:00" / "consulta hoje às 15h"
    r"\b\w+\b.{0,40}\b(amanhã|hoje|segunda|terça|quarta|quinta|sexta|sábado|domingo)\b.{0,20}\d{1,2}[h:]\d{0,2}",
    # "reunião amanhã" / "dentista sexta"
    r"\b\w+\b.{0,30}\b(amanhã|segunda|terça|quarta|quinta|sexta|sábado|domingo)\b",
    # "amanhã 12:00 reunião"
    r"\b(amanhã|hoje)\b.{0,20}\d{1,2}[h:]\d{0,2}",
]


def _detectar_lembrete_rapido(texto: str) -> bool:
    """Detecta intenção de lembrete por palavras-chave explícitas."""
    t = texto.lower()
    return any(re.search(p, t) for p in _PADROES_EXPLICITOS)


async def detectar_lembrete_implicito(texto: str) -> bool:
    """
    Para mensagens sem palavra-chave explícita mas com padrão de data/hora,
    usa Grok para decidir se é um lembrete ou outra coisa.
    """
    t = texto.lower()

    # Primeiro filtra por padrão implícito para não chamar API à toa
    if not any(re.search(p, t) for p in _PADROES_IMPLICITOS):
        return False

    # Também ignora se parece claramente um gasto
    if re.search(r"\b(cartão|pix|dinheiro|débito|crédito|reais|r\$)\b", t):
        return False

    try:
        resp = await _grok.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=5,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você classifica mensagens de WhatsApp. Responda APENAS: LEMBRETE ou NAO.\n\n"
                        "LEMBRETE = mensagem que agenda um evento futuro para ser lembrado. "
                        "Exemplos: 'reunião amanhã 12:00', 'consulta sexta 15h', 'dentista amanhã', "
                        "'academia hoje 18h', 'ligação amanhã de manhã'.\n\n"
                        "NAO = qualquer outra coisa, especialmente gastos financeiros."
                    ),
                },
                {"role": "user", "content": texto},
            ],
        )
        return resp.choices[0].message.content.strip().upper() == "LEMBRETE"
    except Exception as e:
        print(f"⚠️ Erro na detecção implícita de lembrete: {e}")
        return False


# ─── Extração com Grok ────────────────────────────────────────────────────────

async def _parsear_lembrete(texto: str) -> dict | None:
    agora_br = datetime.now(TZ_BR)
    agora_str = agora_br.strftime("%Y-%m-%d %H:%M")
    dia_semana = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][agora_br.weekday()]

    system_prompt = f"""Você extrai informações de lembretes de mensagens em português.

Data e hora atual: {agora_str} ({dia_semana}-feira, fuso de Brasília)

Responda APENAS com JSON válido, sem markdown, sem explicação:
{{
  "mensagem": "descrição clara do que deve ser lembrado",
  "horario_iso": "YYYY-MM-DDTHH:MM:00-03:00"
}}

Regras para "mensagem":
- Extraia o ASSUNTO do lembrete, ignorando as partes de data/hora e agendamento
- Remova preposições e artigos soltos do início: "da", "do", "de", "para", "a", "o", "as", "os"
- Exemplos: "me lembre da reunião" → "reunião"; "lembra de tomar remédio" → "tomar remédio"
- Se a mensagem for só "reunião amanhã 12:00" → "reunião"
- Se não houver assunto claro, use uma descrição genérica como "compromisso"

Regras para "horario_iso":
- "hoje" = {agora_br.strftime("%Y-%m-%d")}
- "amanhã" = {(agora_br + timedelta(days=1)).strftime("%Y-%m-%d")}
- Se não especificar data, assuma hoje (ou amanhã se o horário já passou)
- Calcule a próxima ocorrência dos dias da semana (segunda, terça, etc.)
- Horários: "9h"/"9:00"/"9 da manhã" → "09:00"; "14h"/"2 da tarde" → "14:00"
- Se o horário for ambíguo (ex: só "7h"), prefira PM se já passou das 7h AM
- Se não houver horário especificado, retorne {{"erro": "sem_horario"}}
- Se não conseguir extrair, retorne {{"erro": "invalido"}}"""

    try:
        resp = await _grok.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=120,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": texto},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
        data = json.loads(raw)
        if "erro" in data:
            return None
        return data
    except Exception as e:
        print(f"⚠️ Erro ao parsear lembrete: {e}")
        return None


# ─── Processar e salvar ───────────────────────────────────────────────────────

async def processar_lembrete(texto: str, usuario: str) -> str:
    dados = await _parsear_lembrete(texto)

    if not dados or "horario_iso" not in dados:
        return (
            "❌ Não consegui entender o horário do lembrete.\n\n"
            "Tente assim:\n"
            "_'Me lembre da reunião hoje às 14:00'_\n"
            "_'Lembra de tomar o remédio amanhã às 8h'_\n"
            "_'Reunião amanhã 12:00'_"
        )

    try:
        horario = datetime.fromisoformat(dados["horario_iso"])
        agora = datetime.now(TZ_BR)

        if horario.tzinfo is None:
            horario = horario.replace(tzinfo=TZ_BR)

        if horario <= agora:
            return (
                "⚠️ Esse horário já passou! Escolha um horário futuro.\n\n"
                "Exemplo: _'Me lembre da reunião amanhã às 14:00'_"
            )

        # Limpa a mensagem: remove preposições soltas do início
        mensagem = dados.get("mensagem", texto).strip()
        mensagem = re.sub(r"^(da|do|de|para|a|o|as|os)\s+", "", mensagem, flags=re.IGNORECASE).strip()
        if not mensagem:
            mensagem = "compromisso"

        lembrete_id = await salvar_lembrete(usuario, mensagem, horario)

        horario_br = horario.astimezone(TZ_BR)
        hoje = agora.date()
        data_lembrete = horario_br.date()

        if data_lembrete == hoje:
            data_str = f"Hoje às {horario_br.strftime('%H:%M')}"
        elif data_lembrete == hoje + timedelta(days=1):
            data_str = f"Amanhã às {horario_br.strftime('%H:%M')}"
        else:
            DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
            dia_nome = DIAS[horario_br.weekday()]
            data_str = f"{dia_nome}, {horario_br.strftime('%d/%m')} às {horario_br.strftime('%H:%M')}"

        return (
            f"🔔 *Lembrete agendado!* ✅\n\n"
            f"📌 {mensagem.capitalize()}\n"
            f"⏰ {data_str}\n\n"
            f"_ID #{lembrete_id} · Para cancelar: *cancelar lembrete {lembrete_id}*_"
        )

    except Exception as e:
        print(f"❌ Erro ao salvar lembrete: {e}")
        return "❌ Erro ao salvar lembrete. Tente novamente."


# ─── Background task ──────────────────────────────────────────────────────────

async def _disparar_lembretes(enviar_func):
    pool = await get_pool()
    while True:
        try:
            agora = datetime.now(TZ_BR)
            ate = agora + timedelta(seconds=45)

            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, usuario, mensagem, horario
                    FROM lembretes
                    WHERE enviado = FALSE
                      AND horario <= $1
                    ORDER BY horario ASC
                    LIMIT 50
                """, ate)

                for row in rows:
                    lembrete_id = row["id"]
                    usuario = row["usuario"]
                    mensagem = row["mensagem"]
                    horario = row["horario"]

                    agora_check = datetime.now(TZ_BR)
                    if horario > agora_check:
                        await asyncio.sleep((horario - agora_check).total_seconds())

                    texto_envio = f"🔔 *Lembrete!*\n\n📌 {mensagem.capitalize()}"
                    remote_jid = usuario if "@" in usuario else f"{usuario}@s.whatsapp.net"

                    try:
                        await enviar_func(remote_jid, texto_envio)
                        print(f"✅ Lembrete #{lembrete_id} enviado para {usuario}")
                    except Exception as e:
                        print(f"❌ Erro ao enviar lembrete #{lembrete_id}: {e}")

                    await conn.execute(
                        "UPDATE lembretes SET enviado=TRUE WHERE id=$1",
                        lembrete_id
                    )

        except Exception as e:
            print(f"❌ Erro no loop de lembretes: {e}")

        await asyncio.sleep(30)


def iniciar_background_lembretes(app, enviar_func):
    loop = asyncio.get_event_loop()
    task = loop.create_task(_disparar_lembretes(enviar_func))
    app.state.lembrete_task = task
    print("🔔 Background task de lembretes iniciada!")
    return task
