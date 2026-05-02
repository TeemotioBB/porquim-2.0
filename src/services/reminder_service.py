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

# ─── Cliente Grok ─────────────────────────────────────────────────────────────
_grok = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1",
)

# Fuso horário padrão (Brasil / São Paulo)
TZ_BR = ZoneInfo("America/Sao_Paulo")


# ─── Salvar lembrete ──────────────────────────────────────────────────────────

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


# ─── Parser de data/hora com Grok ─────────────────────────────────────────────

async def _parsear_lembrete(texto: str) -> dict | None:
    agora_br = datetime.now(TZ_BR)
    agora_str = agora_br.strftime("%Y-%m-%d %H:%M")
    dia_semana = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][agora_br.weekday()]

    system_prompt = f"""Você extrai informações de lembretes de mensagens em português.

Data e hora atual: {agora_str} ({dia_semana}-feira, fuso de Brasília)

Responda APENAS com JSON válido, sem markdown, sem explicação:
{{
  "mensagem": "texto do lembrete (o que deve ser lembrado, sem a parte de agendamento)",
  "horario_iso": "YYYY-MM-DDTHH:MM:00-03:00"
}}

Regras:
- "hoje" = {agora_br.strftime("%Y-%m-%d")}
- "amanhã" = {(agora_br + timedelta(days=1)).strftime("%Y-%m-%d")}
- Se não especificar data, assuma hoje
- Horários como "9h", "9:00", "9 da manhã" → "09:00"
- Horários como "14h", "14:00", "2 da tarde" → "14:00"
- Prefira PM para ambiguidades (ex: "7h" sem contexto → 19:00 se já passou das 7h da manhã)
- Se não conseguir extrair horário válido, retorne {{"erro": "horario_invalido"}}

Exemplos:
- "me lembre da reunião hoje às 14:00" → {{"mensagem": "reunião", "horario_iso": "{agora_br.strftime('%Y-%m-%d')}T14:00:00-03:00"}}
- "lembra de tomar o remédio amanhã às 8h" → {{"mensagem": "tomar o remédio", "horario_iso": "{(agora_br + timedelta(days=1)).strftime('%Y-%m-%d')}T08:00:00-03:00"}}
- "me avisa da consulta sexta às 15:30" → horario calculado para próxima sexta"""

    try:
        resp = await _grok.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=100,
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


# ─── Detectar intenção de lembrete ───────────────────────────────────────────

def _detectar_lembrete_rapido(texto: str) -> bool:
    t = texto.lower()
    padroes = [
        r"\b(me lembre|me lembra|lembra de|lembra que|me avisa|me avise)\b",
        r"\b(lembrete|agendar lembrete|criar lembrete)\b",
        r"\b(me notifica|me notifique)\b",
    ]
    return any(re.search(p, t) for p in padroes)


async def processar_lembrete(texto: str, usuario: str) -> str:
    dados = await _parsear_lembrete(texto)

    if not dados or "horario_iso" not in dados:
        return (
            "❌ Não consegui entender o horário do lembrete.\n\n"
            "Tente assim:\n"
            "_'Me lembre da reunião hoje às 14:00'_\n"
            "_'Lembra de tomar o remédio amanhã às 8h'_\n"
            "_'Me avisa da consulta sexta às 15:30'_"
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

        mensagem = dados.get("mensagem", texto)
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
