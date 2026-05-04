"""
Serviço de Gastos Recorrentes e Compras Parceladas para o Johnny 🐹
"""

import asyncio
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from src.core.database import (
    get_pool,
    salvar_recorrente,
    listar_recorrentes,
    buscar_recorrente,
    cancelar_recorrente,
    marcar_aviso_recorrente,
    buscar_recorrentes_do_dia,
    salvar_parcela,
    listar_parcelas,
    buscar_parcela,
    cancelar_parcela,
    incrementar_parcela_atual,
    buscar_parcelas_ativas_todas,
    salvar_gasto,
    salvar_intencao_pendente,
)

TZ_BR = ZoneInfo("America/Sao_Paulo")

EMOJI_CAT = {
    "Alimentação": "🍔", "Transporte": "🚗", "Moradia": "🏠",
    "Saúde": "💊", "Lazer": "🎮", "Vestuário": "👕",
    "Educação": "📚", "Outros": "📦",
}


# ─── Detecção de intenção ─────────────────────────────────────────────────────

def detectar_recorrente(texto: str) -> bool:
    """Detecta se a mensagem é para criar um gasto recorrente."""
    t = texto.lower().strip()
    t_norm = _normalizar_numeros_extenso(t)
    padroes = [
        r"\btodo\s+(dia\s+)?\d{1,2}\b",                       # "todo dia 10"
        r"\btodo\s+m[eê]s\s+(no\s+)?dia\s+\d{1,2}",           # "todo mês no dia 5"
        r"\bmensalmente\b",
        r"\bdia\s+\d{1,2}\s+todo\s+m[eê]s",                   # "dia 5 todo mês"
        r"\brecorrente\b",
        r"\btodos?\s+os\s+meses\b",                            # "todos os meses"
        r"\bcada\s+m[eê]s\b",                                  # "cada mês"
    ]
    return any(re.search(p, t_norm) for p in padroes)


def detectar_parcelado(texto: str) -> bool:
    """Detecta se a mensagem indica compra parcelada."""
    t = texto.lower().strip()
    # Normaliza números por extenso comuns
    t_norm = _normalizar_numeros_extenso(t)
    padroes = [
        r"\bem\s+\d+\s*x\b",                     # "em 6x", "em 12 x"
        r"\b\d+\s*x\s+de\s+",                     # "6x de 200"
        r"\bparcelad[oa]s?\s+em\s+\d+",           # "parcelado em 6"
        r"\bparcelei\s+(em\s+|de\s+)?\d+",        # "parcelei em 6", "parcelei de 8"
        r"\b\d+\s+parcelas\b",                    # "12 parcelas"
        r"\b\d+\s+(vezes|veses)\s+de\s+",         # "8 vezes de 175"
        r"\bem\s+\d+\s+(vezes|veses)\b",          # "em 8 vezes"
        r"\bde\s+\d+\s+(vezes|veses)\b",          # "de 8 vezes"
    ]
    return any(re.search(p, t_norm) for p in padroes)


# ─── Normalização de números por extenso ──────────────────────────────────────

_NUMEROS_EXTENSO = {
    "duas": "2", "dois": "2", "três": "3", "tres": "3",
    "quatro": "4", "cinco": "5", "seis": "6", "sete": "7",
    "oito": "8", "nove": "9", "dez": "10", "onze": "11",
    "doze": "12", "treze": "13", "quatorze": "14", "catorze": "14",
    "quinze": "15", "dezesseis": "16", "dezessete": "17",
    "dezoito": "18", "dezenove": "19", "vinte": "20",
    "vinte e quatro": "24", "vinte e quatro": "24",
    "trinta": "30", "trinta e seis": "36", "quarenta e oito": "48",
    "sessenta": "60",
}

def _normalizar_numeros_extenso(texto: str) -> str:
    """Substitui números por extenso pelos seus dígitos (apenas no contexto de parcelas)."""
    t = texto
    # Ordena por tamanho decrescente pra "vinte e quatro" vir antes de "vinte"
    for palavra, num in sorted(_NUMEROS_EXTENSO.items(), key=lambda x: -len(x[0])):
        t = re.sub(rf"\b{palavra}\b", num, t, flags=re.IGNORECASE)
    return t


# ─── Parsers ──────────────────────────────────────────────────────────────────

async def parsear_recorrente(texto: str, grok_client) -> dict | None:
    """Usa o Grok para extrair: descricao, valor, categoria, dia_mes, forma_pagamento."""
    import json
    prompt = f"""Você extrai informações de gastos RECORRENTES (mensais) em português.

Responda APENAS com JSON válido, sem markdown:
{{
  "descricao": "descrição limpa do gasto",
  "valor": 120.00,
  "categoria": "uma de (Alimentação, Transporte, Vestuário, Moradia, Saúde, Educação, Lazer, Outros)",
  "dia_mes": 10,
  "forma_pagamento": "Pix, Cartão, Dinheiro ou Desconhecido"
}}

Regras:
- "dia_mes" é o dia do mês (1-31) que o gasto se repete
- Se não houver dia explícito, retorne {{"erro": "sem_dia"}}
- Se não houver valor, retorne {{"erro": "sem_valor"}}
- Categoria deve refletir o tipo do gasto (faculdade=Educação, aluguel=Moradia, plano de saúde=Saúde, etc.)

Exemplos:
"todo dia 10 pagar faculdade 120" → {{"descricao": "Faculdade", "valor": 120, "categoria": "Educação", "dia_mes": 10, "forma_pagamento": "Desconhecido"}}
"aluguel 1500 todo dia 5" → {{"descricao": "Aluguel", "valor": 1500, "categoria": "Moradia", "dia_mes": 5, "forma_pagamento": "Desconhecido"}}
"academia 80 todo mês dia 15 cartão" → {{"descricao": "Academia", "valor": 80, "categoria": "Saúde", "dia_mes": 15, "forma_pagamento": "Cartão"}}

Mensagem: {texto}"""
    try:
        resp = await grok_client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
        data = json.loads(raw)
        if "erro" in data:
            return None
        # Validação básica
        if not all(k in data for k in ("descricao", "valor", "categoria", "dia_mes")):
            return None
        dia = int(data["dia_mes"])
        if dia < 1 or dia > 31:
            return None
        return data
    except Exception as e:
        print(f"⚠️ Erro ao parsear recorrente: {e}")
        return None


async def parsear_parcelado(texto: str, grok_client) -> dict | None:
    """Extrai: descricao, valor_total, num_parcelas, categoria, forma_pagamento."""
    import json
    prompt = f"""Você extrai informações de COMPRAS PARCELADAS em português brasileiro.

Responda APENAS com JSON válido, sem markdown:
{{
  "descricao": "descrição do produto/compra",
  "valor_total": 1200.00,
  "num_parcelas": 6,
  "categoria": "uma de (Alimentação, Transporte, Vestuário, Moradia, Saúde, Educação, Lazer, Outros)",
  "forma_pagamento": "Cartão"
}}

ATENÇÃO — interpretação dos valores:
- "valor_total" é o valor TOTAL da compra (não o da parcela)
- "TV de 6x de 200" = 6 parcelas × R$200 = TOTAL R$1200
- "TV 1200 em 6x" = total já é R$1200
- "TV de 8 vezes de 175" = 8 × R$175 = TOTAL R$1400
- "TV de oito vezes de 1400" = AMBÍGUO. Por padrão, interprete o número MAIOR como TOTAL e divida pelas parcelas (8 parcelas, total R$1400, parcela R$175)
- "parcelei a TV de 8 vezes 1400" = mesmo caso acima: 8 parcelas, total R$1400
- num_parcelas mínimo: 2

Aceite números por extenso: oito=8, doze=12, vinte e quatro=24, etc.

Exemplos:
"comprei TV 1200 em 6x cartão" → {{"descricao": "TV", "valor_total": 1200, "num_parcelas": 6, "categoria": "Lazer", "forma_pagamento": "Cartão"}}
"sapato 300 parcelado em 3x" → {{"descricao": "Sapato", "valor_total": 300, "num_parcelas": 3, "categoria": "Vestuário", "forma_pagamento": "Cartão"}}
"geladeira 12x de 250 cartão" → {{"descricao": "Geladeira", "valor_total": 3000, "num_parcelas": 12, "categoria": "Moradia", "forma_pagamento": "Cartão"}}
"mouse 90 em 2x" → {{"descricao": "Mouse", "valor_total": 90, "num_parcelas": 2, "categoria": "Outros", "forma_pagamento": "Cartão"}}
"parcelei a TV em 8 vezes de 1400" → {{"descricao": "TV", "valor_total": 1400, "num_parcelas": 8, "categoria": "Lazer", "forma_pagamento": "Cartão"}}
"comprei um sofá e parcelei em 10 vezes 250" → {{"descricao": "Sofá", "valor_total": 2500, "num_parcelas": 10, "categoria": "Moradia", "forma_pagamento": "Cartão"}}
"fiz uma compra parcelada da geladeira de 12 vezes de 200" → {{"descricao": "Geladeira", "valor_total": 2400, "num_parcelas": 12, "categoria": "Moradia", "forma_pagamento": "Cartão"}}

Mensagem: {texto}"""
    try:
        resp = await grok_client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
        data = json.loads(raw)
        if "erro" in data:
            return None
        if not all(k in data for k in ("descricao", "valor_total", "num_parcelas", "categoria")):
            return None
        n = int(data["num_parcelas"])
        if n < 2 or n > 60:
            return None
        return data
    except Exception as e:
        print(f"⚠️ Erro ao parsear parcelado: {e}")
        return None


# ─── Criação ──────────────────────────────────────────────────────────────────

async def criar_recorrente(usuario: str, dados: dict) -> str:
    """Cria um gasto recorrente e retorna a mensagem de confirmação."""
    rec_id = await salvar_recorrente(
        usuario=usuario,
        descricao=dados["descricao"],
        valor=float(dados["valor"]),
        categoria=dados["categoria"],
        dia_mes=int(dados["dia_mes"]),
        forma_pagamento=dados.get("forma_pagamento", "Desconhecido"),
    )
    emoji = EMOJI_CAT.get(dados["categoria"], "📦")
    return (
        f"🔁 *Gasto recorrente criado!*\n\n"
        f"{emoji} {dados['descricao']}\n"
        f"💰 R$ {float(dados['valor']):.2f}\n"
        f"🏷️ {dados['categoria']}\n"
        f"📆 Todo dia {dados['dia_mes']} do mês\n\n"
        f"_ID #{rec_id}_\n"
        f"_Vou te lembrar todo dia {dados['dia_mes']} pra você confirmar o pagamento._\n"
        f"_Para cancelar: *cancelar recorrente {rec_id}*_"
    )


async def criar_parcelado(usuario: str, dados: dict) -> str:
    """
    Cria uma compra parcelada, lança a 1ª parcela imediatamente
    e retorna a mensagem de confirmação.
    """
    hoje = date.today()
    valor_total = float(dados["valor_total"])
    num_parcelas = int(dados["num_parcelas"])
    valor_parcela = round(valor_total / num_parcelas, 2)
    categoria = dados["categoria"]
    forma = dados.get("forma_pagamento", "Cartão")
    descricao = dados["descricao"]

    # Cria registro da parcela
    parcela = await salvar_parcela(
        usuario=usuario,
        descricao=descricao,
        valor_total=valor_total,
        num_parcelas=num_parcelas,
        categoria=categoria,
        forma_pagamento=forma,
        data_compra=hoje,
    )

    # Lança a 1ª parcela como gasto
    desc_gasto = f"{descricao} (1/{num_parcelas})"
    gasto_dados = {
        "descricao": desc_gasto,
        "valor": valor_parcela,
        "categoria": categoria,
        "forma_pagamento": forma,
        "data": hoje,
        "hashtag": f"#parc{parcela['id']}",
    }
    await salvar_gasto(usuario, gasto_dados, fonte="parcelado", parcela_id=parcela["id"])
    await incrementar_parcela_atual(parcela["id"])

    emoji = EMOJI_CAT.get(categoria, "📦")
    return (
        f"💳 *Compra parcelada registrada!*\n\n"
        f"{emoji} {descricao}\n"
        f"💰 Total: R$ {valor_total:.2f}\n"
        f"📊 {num_parcelas}x de R$ {valor_parcela:.2f}\n"
        f"🏷️ {categoria}\n"
        f"📅 1ª parcela lançada hoje\n\n"
        f"_ID #{parcela['id']}_\n"
        f"_As próximas parcelas serão lançadas no dia {hoje.day} de cada mês._\n"
        f"_Para cancelar parcelas futuras: *cancelar parcela {parcela['id']}*_"
    )


# ─── Listagem ─────────────────────────────────────────────────────────────────

async def listar_recorrentes_formatado(usuario: str) -> str:
    recs = await listar_recorrentes(usuario, apenas_ativos=True)
    if not recs:
        return (
            "📭 Você não tem gastos recorrentes cadastrados.\n\n"
            "Para criar um:\n"
            "_'Todo dia 10 faculdade 120'_\n"
            "_'Aluguel 1500 todo dia 5'_"
        )
    linhas = ["🔁 *Seus gastos recorrentes:*\n"]
    for r in recs:
        emoji = EMOJI_CAT.get(r["categoria"], "📦")
        linhas.append(
            f"#{r['id']} · {emoji} {r['descricao']}\n"
            f"   R$ {float(r['valor']):.2f} · todo dia {r['dia_mes']}"
        )
    linhas.append("\n_Para cancelar: *cancelar recorrente [ID]*_")
    return "\n".join(linhas)


async def listar_parcelas_formatado(usuario: str) -> str:
    parcs = await listar_parcelas(usuario, apenas_ativas=True)
    if not parcs:
        return (
            "📭 Você não tem compras parceladas ativas.\n\n"
            "Para registrar uma:\n"
            "_'TV 1200 em 6x cartão'_\n"
            "_'Sapato 300 parcelado em 3x'_"
        )
    linhas = ["💳 *Suas compras parceladas:*\n"]
    for p in parcs:
        emoji = EMOJI_CAT.get(p["categoria"], "📦")
        atual = p["parcela_atual"]
        total = p["num_parcelas"]
        restantes = total - atual
        valor_restante = float(p["valor_parcela"]) * restantes
        linhas.append(
            f"#{p['id']} · {emoji} {p['descricao']}\n"
            f"   {atual}/{total} pagas · R$ {float(p['valor_parcela']):.2f}/mês\n"
            f"   Restam {restantes}x (R$ {valor_restante:.2f})"
        )
    linhas.append("\n_Para cancelar parcelas futuras: *cancelar parcela [ID]*_")
    return "\n".join(linhas)


# ─── Background task ──────────────────────────────────────────────────────────

async def _processar_recorrentes_e_parcelas_do_dia(enviar_func):
    """
    Roda 1x por dia (ao bater 09:00 BR ou no startup se ainda não rodou hoje).
    - Avisa recorrentes do dia (e marca ultimo_aviso)
    - Lança parcelas vencidas (compras feitas em dias anteriores cujo aniversário cai hoje)
    """
    hoje = datetime.now(TZ_BR).date()
    pool = await get_pool()

    # ── Avisos de recorrentes ──
    recs = await buscar_recorrentes_do_dia(hoje.day)
    # Edge case: meses que não têm o dia 30/31 — avisa no último dia do mês
    if hoje.day == _ultimo_dia_mes(hoje):
        async with pool.acquire() as conn:
            extras = await conn.fetch(
                "SELECT * FROM gastos_recorrentes WHERE dia_mes > $1 AND ativo=TRUE",
                hoje.day
            )
        recs.extend([dict(r) for r in extras])

    for r in recs:
        ult = r.get("ultimo_aviso")
        # Não duplica aviso no mesmo mês
        if ult and ult.year == hoje.year and ult.month == hoje.month:
            continue
        usuario = r["usuario"]
        rec_id = r["id"]
        emoji = EMOJI_CAT.get(r["categoria"], "📦")
        msg = (
            f"🔁 *Lembrete de gasto recorrente*\n\n"
            f"{emoji} *{r['descricao']}*\n"
            f"💰 R$ {float(r['valor']):.2f}\n"
            f"📅 Vence hoje (dia {r['dia_mes']})\n\n"
            f"Você já pagou? Responda *sim* pra eu lançar como gasto, ou *não* pra eu não fazer nada.\n\n"
            f"_(também posso ignorar e você lança depois)_"
        )
        remote_jid = usuario if "@" in usuario else f"{usuario}@s.whatsapp.net"
        try:
            await enviar_func(remote_jid, msg)
            await marcar_aviso_recorrente(rec_id, hoje)
            # Salva intenção pendente para que "sim" lance o gasto
            await salvar_intencao_pendente(usuario, f"recorrente:{rec_id}")
            print(f"🔁 Aviso recorrente #{rec_id} enviado para {usuario}")
        except Exception as e:
            print(f"❌ Erro aviso recorrente #{rec_id}: {e}")

    # ── Lançamento automático de parcelas ──
    parcs = await buscar_parcelas_ativas_todas()
    for p in parcs:
        usuario = p["usuario"]
        data_compra = p["data_compra"]
        atual = p["parcela_atual"]
        total = p["num_parcelas"]

        if atual >= total:
            continue

        # Próximo lançamento: data_compra + 'atual' meses
        proxima_data = _add_months(data_compra, atual)
        if proxima_data > hoje:
            continue
        # Já lançada hoje? checa se existe gasto dessa parcela com data == proxima_data
        async with pool.acquire() as conn:
            ja_lancada = await conn.fetchval(
                "SELECT COUNT(*) FROM gastos WHERE parcela_id=$1 AND data=$2",
                p["id"], proxima_data
            )
        if int(ja_lancada) > 0:
            continue

        # Lança o gasto da parcela
        nova_parcela = atual + 1
        desc_gasto = f"{p['descricao']} ({nova_parcela}/{total})"
        gasto_dados = {
            "descricao": desc_gasto,
            "valor": float(p["valor_parcela"]),
            "categoria": p["categoria"],
            "forma_pagamento": p["forma_pagamento"],
            "data": proxima_data,
            "hashtag": f"#parc{p['id']}",
        }
        try:
            await salvar_gasto(usuario, gasto_dados, fonte="parcelado", parcela_id=p["id"])
            await incrementar_parcela_atual(p["id"])
            emoji = EMOJI_CAT.get(p["categoria"], "📦")
            msg = (
                f"💳 *Parcela lançada automaticamente!*\n\n"
                f"{emoji} {p['descricao']} ({nova_parcela}/{total})\n"
                f"💰 R$ {float(p['valor_parcela']):.2f}\n"
                f"📅 {proxima_data.strftime('%d/%m/%Y')}\n\n"
                f"_Restam {total - nova_parcela} parcela(s)._"
            )
            remote_jid = usuario if "@" in usuario else f"{usuario}@s.whatsapp.net"
            await enviar_func(remote_jid, msg)
            print(f"💳 Parcela {nova_parcela}/{total} de #{p['id']} lançada para {usuario}")
        except Exception as e:
            print(f"❌ Erro ao lançar parcela #{p['id']}: {e}")


def _ultimo_dia_mes(d: date) -> int:
    if d.month == 12:
        prox = date(d.year + 1, 1, 1)
    else:
        prox = date(d.year, d.month + 1, 1)
    return (prox - timedelta(days=1)).day


def _add_months(d: date, meses: int) -> date:
    """Adiciona meses respeitando o último dia do mês."""
    ano = d.year + (d.month - 1 + meses) // 12
    mes = (d.month - 1 + meses) % 12 + 1
    if mes == 12:
        prox = date(ano + 1, 1, 1)
    else:
        prox = date(ano, mes + 1, 1)
    ultimo = (prox - timedelta(days=1)).day
    return date(ano, mes, min(d.day, ultimo))


async def _loop_diario(enviar_func):
    """Roda às 9h da manhã (BR) todo dia. No startup verifica se já rodou hoje."""
    pool = await get_pool()
    # Cria tabela de controle se não existir
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _job_runs (
                job_name VARCHAR(50) PRIMARY KEY,
                ultima_execucao DATE NOT NULL
            );
        """)

    while True:
        try:
            agora = datetime.now(TZ_BR)
            hoje = agora.date()

            async with pool.acquire() as conn:
                ult = await conn.fetchval(
                    "SELECT ultima_execucao FROM _job_runs WHERE job_name='recorrentes_parcelas'"
                )

            ja_rodou_hoje = ult == hoje
            hora_certa = agora.hour >= 9  # roda a partir de 9h da manhã

            if hora_certa and not ja_rodou_hoje:
                print(f"🔁 Processando recorrentes e parcelas — {hoje}")
                await _processar_recorrentes_e_parcelas_do_dia(enviar_func)
                async with pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO _job_runs (job_name, ultima_execucao)
                        VALUES ('recorrentes_parcelas', $1)
                        ON CONFLICT (job_name) DO UPDATE SET ultima_execucao=$1
                    """, hoje)
                print(f"✅ Job recorrentes_parcelas concluído")
        except Exception as e:
            print(f"❌ Erro no loop diário: {e}")

        # Verifica a cada 10 minutos
        await asyncio.sleep(600)


def iniciar_background_recorrentes(app, enviar_func):
    loop = asyncio.get_event_loop()
    task = loop.create_task(_loop_diario(enviar_func))
    app.state.recorrentes_task = task
    print("🔁 Background task de recorrentes/parcelas iniciada!")
    return task
