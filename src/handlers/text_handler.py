import re
from datetime import date, timedelta
from src.services.ia_service import processar_gasto_texto, processar_entrada_texto
from src.services.report_service import (
    gerar_resumo, gerar_resumo_intervalo,
    definir_limite, verificar_limite_pos_gasto,
    definir_limite_categoria_msg, listar_limites_categoria_formatado,
    remover_limite_categoria_msg,
)
from src.services.reminder_service import (
    _detectar_lembrete_rapido,
    detectar_lembrete_implicito,
    processar_lembrete,
    buscar_lembretes_pendentes,
    cancelar_lembrete,
)
from src.services.recurring_service import (
    detectar_recorrente, detectar_parcelado,
    parsear_recorrente, parsear_parcelado,
    criar_recorrente, criar_parcelado,
    listar_recorrentes_formatado, listar_parcelas_formatado,
)
from src.core.database import (
    salvar_gasto, deletar_gasto, atualizar_gasto, buscar_gasto_por_id,
    salvar_entrada, deletar_entrada, buscar_entrada_por_id,
    salvar_memoria, buscar_memoria, limpar_memoria_gasto, limpar_memoria_entrada,
    salvar_intencao_pendente, limpar_intencao_pendente,
    cancelar_recorrente, buscar_recorrente,
    cancelar_parcela, buscar_parcela,
)
from src.services.ia_service import processar_gasto_texto as _extrair
from src.core.config import settings
from openai import AsyncOpenAI

# ─── Cliente Grok ─────────────────────────────────────────────────────────────
_grok = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1",
)

AJUDA = """👋 Oi! Eu sou o Johnny 🐹💚
Seu assistente financeiro aqui no WhatsApp.

💸 *Registrar gastos:*
_"Uber 27"_ ou _"Almoço 35 cartão"_
(também por áudio ou foto 👀)

💰 *Entrou dinheiro?*
_"Salário 3000"_ ou _"Recebi 500"_

💳 *Compras parceladas:*
_"TV 1200 em 6x cartão"_
_"Sapato 300 em 3x"_

🔁 *Gastos recorrentes:*
_"Todo dia 10 faculdade 120"_
_"Aluguel 1500 todo dia 5"_

🎯 *Limites:*
_"Limite 2000"_ (geral)
_"Limite roupas 200"_ (por categoria)

📊 *Resumos:*
*resumo* · *resumo hoje* · *resumo ontem*
*resumo de janeiro* · *resumo mês passado*

🔔 *Lembretes:*
_"Reunião amanhã 14h"_

📋 *Listar:*
*recorrentes* · *parcelas* · *limites* · *meus lembretes*

📲 *Dashboard completo:*
👉 https://dashboard-porquim-theta.vercel.app

Um hábito simples que muda tudo 💚"""

CARD_GASTO = """✅ *Gasto Registrado!*

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉
_Para remover este gasto responda: *remover*_
_Para editar responda: *editar*_"""

CARD_ENTRADA = """✅ *Entrada Registrada!*

📍 {descricao}
💵 R$ {valor:.2f}
🏷️ {categoria}
📅 {data}
🔖 {hashtag}

_Salvo com sucesso!_ 🎉
_Para remover esta entrada responda: *remover entrada*_"""

MESES_NOMES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12
}

# Resumo em RAM (só para a sessão de edição/remoção por número)
_resumo_gastos: dict[str, list] = {}


# ─── Detecção de intenção: GASTO ─────────────────────────────────────────────

async def _detectar_intencao(texto: str) -> str:
    """Retorna: GASTO | OUTRO"""
    t = texto.strip().lower()

    if not re.search(r"\d", t):
        return "OUTRO"

    if re.fullmatch(r"[kkkhahehe😂🤣👍🙏❤️\s!?.]+", t):
        return "OUTRO"

    padroes_outro = [
        r"^(oi|olá|ola|ei|eai|e aí|opa|hey)\b",
        r"^(tudo bem|tudo bom|como vai|tá bom|ok|okay|certo|entendi|show)\b",
        r"^(obrigad|valeu|vlw|tmj|flw|abraç)\b",
        r"^(sim|não|nao|talvez|claro)\b",
        r"^(bom dia|boa tarde|boa noite)\b",
    ]
    for p in padroes_outro:
        if re.search(p, t):
            return "OUTRO"

    if re.search(r"\b\d+([.,]\d+)?\b", t) and len(t.split()) >= 2:
        pass
    elif re.search(r"\b\d+([.,]\d+)?\b", t) and len(t.split()) == 1:
        return "OUTRO"

    try:
        resp = await _grok.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=5,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você classifica mensagens de WhatsApp de um app de controle financeiro. "
                        "Responda APENAS com uma palavra: GASTO ou OUTRO.\n\n"
                        "GASTO = mensagem que registra uma despesa financeira. "
                        "Exemplos: 'uber 25', 'mc donalds 45 cartão', 'gasolina 90 pix', "
                        "'farmácia 38,50', 'aluguel 1200', 'gastei 50 no mercado'.\n\n"
                        "OUTRO = qualquer outra coisa: saudações, perguntas, piadas, "
                        "elogios, números aleatórios sem contexto de gasto, etc. "
                        "Exemplos: 'oi', 'kkk', 'que legal!', 'valeu', '123', 'tá bom'."
                    ),
                },
                {"role": "user", "content": texto},
            ],
        )
        resultado = resp.choices[0].message.content.strip().upper()
        return resultado if resultado in ("GASTO", "OUTRO") else "OUTRO"
    except Exception as e:
        print(f"⚠️ Erro na detecção de intenção: {e}")
        return "GASTO"


# ─── Detecção de intenção: ENTRADA ───────────────────────────────────────────

async def _detectar_entrada(texto: str) -> bool:
    """Retorna True se a mensagem parece uma entrada de dinheiro."""
    t = texto.strip().lower()

    if not re.search(r"\d", t):
        return False

    if re.search(
        r"\b(recebi|receber|salário|salario|freelance|renda|ganho|ganhei|"
        r"entrou|pagaram|me pagou|me pagaram|reembolso|investimento|dividendo)\b", t
    ):
        return True

    try:
        resp = await _grok.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            max_tokens=5,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você classifica mensagens de WhatsApp de um app financeiro. "
                        "Responda APENAS: ENTRADA ou NAO.\n\n"
                        "ENTRADA = mensagem que registra dinheiro que a pessoa RECEBEU. "
                        "Exemplos: 'salário 3000', 'recebi 500', 'freelance 800', "
                        "'me pagaram 200', 'entrou 1500 na conta', 'reembolso 90', "
                        "'salario 200', 'bonus 400', '13º salário 1500'.\n\n"
                        "NAO = qualquer outra coisa, incluindo gastos e saudações."
                    ),
                },
                {"role": "user", "content": texto},
            ],
        )
        resultado = resp.choices[0].message.content.strip().upper()
        return resultado == "ENTRADA"
    except Exception as e:
        print(f"⚠️ Erro na detecção de entrada: {e}")
        return False


# ─── Parser de mês para resumo ───────────────────────────────────────────────

def _parse_mes_resumo(texto: str):
    hoje = date.today()
    resto = re.sub(r"^resumo\s*", "", texto.lower().strip()).strip()
    resto = re.sub(r"^(do|de|da)\s+", "", resto).strip()

    if not resto:
        return hoje.year, hoje.month
    if re.search(r"m[eê]s\s+passado", resto):
        primeiro = hoje.replace(day=1)
        anterior = primeiro - timedelta(days=1)
        return anterior.year, anterior.month
    if "ano passado" in resto:
        return hoje.year - 1, hoje.month
    for nome, num in MESES_NOMES.items():
        if nome in resto:
            ano_match = re.search(r"\b(20\d{2})\b", resto)
            ano = int(ano_match.group(1)) if ano_match else hoje.year
            return ano, num
    num_match = re.match(r"^(\d{1,2})$", resto)
    if num_match:
        mes = int(num_match.group(1))
        if 1 <= mes <= 12:
            return hoje.year, mes
    return hoje.year, hoje.month


def _parse_resumo_intervalo(texto: str) -> tuple[date, date, str] | None:
    """
    Detecta resumos de intervalo: 'resumo hoje', 'resumo ontem', 'resumo semana',
    'resumo dia 15', 'resumo de 01/05 a 15/05'.
    Retorna (data_inicio, data_fim, titulo) ou None se não bater.
    """
    t = texto.lower().strip()
    t = re.sub(r"^resumo\s*", "", t).strip()
    t = re.sub(r"^(do|de|da)\s+", "", t).strip()
    hoje = date.today()

    if t == "hoje":
        return hoje, hoje, "Hoje"
    if t == "ontem":
        ontem = hoje - timedelta(days=1)
        return ontem, ontem, "Ontem"
    if t in ("semana", "esta semana", "essa semana"):
        # Início da semana = segunda-feira
        ini = hoje - timedelta(days=hoje.weekday())
        return ini, hoje, "Esta semana"
    if t == "semana passada":
        fim_passada = hoje - timedelta(days=hoje.weekday() + 1)
        ini_passada = fim_passada - timedelta(days=6)
        return ini_passada, fim_passada, "Semana passada"

    # "resumo dia 15" ou "resumo dia 15/05"
    m = re.match(r"^dia\s+(\d{1,2})(?:[/-](\d{1,2}))?(?:[/-](\d{2,4}))?$", t)
    if m:
        dia = int(m.group(1))
        mes = int(m.group(2)) if m.group(2) else hoje.month
        ano = int(m.group(3)) if m.group(3) else hoje.year
        if ano < 100:
            ano += 2000
        try:
            d = date(ano, mes, dia)
            return d, d, d.strftime("%d/%m/%Y")
        except ValueError:
            return None

    # "resumo 01/05 a 15/05" ou "resumo de 01/05 até 15/05"
    m = re.match(
        r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s+(?:a|até|ate)\s+(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$",
        t
    )
    if m:
        try:
            d1, m1, a1 = int(m.group(1)), int(m.group(2)), int(m.group(3) or hoje.year)
            d2, m2, a2 = int(m.group(4)), int(m.group(5)), int(m.group(6) or hoje.year)
            if a1 < 100: a1 += 2000
            if a2 < 100: a2 += 2000
            ini = date(a1, m1, d1)
            fim = date(a2, m2, d2)
            if ini > fim:
                ini, fim = fim, ini
            return ini, fim, f"{ini.strftime('%d/%m')} a {fim.strftime('%d/%m/%Y')}"
        except ValueError:
            return None

    return None


# ─── Handler principal ────────────────────────────────────────────────────────

async def handle_text_message(message: dict) -> dict:
    texto = message["text"]["body"].strip()
    numero = message["key"]["remoteJid"].split("@")[0]
    texto_lower = texto.lower()

    # ── Ajuda ─────────────────────────────────────────────────────────────────
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

    # ── Confirmação de intenção pendente ──────────────────────────────────────
    _confirmacoes = ["sim", "pode", "confirma", "isso", "quero", "vai", "ok", "bora", "yes", "s", "já paguei", "ja paguei", "paguei"]
    _cancelamentos = ["não", "nao", "cancela", "esquece", "deixa", "no", "ainda não", "ainda nao", "depois"]

    if texto_lower in _confirmacoes or texto_lower in _cancelamentos:
        mem = await buscar_memoria(numero)
        intencao = mem.get("intencao_pendente")
        if intencao:
            await limpar_intencao_pendente(numero)
            if texto_lower in _cancelamentos:
                return {"type": "text", "content": "Ok, sem problema! 😊 Pode mandar outro comando quando quiser."}
            if intencao.startswith("limite:"):
                valor = float(intencao.split(":")[1])
                return {"type": "text", "content": await definir_limite(numero, valor)}
            if intencao.startswith("recorrente:"):
                # Lança o recorrente como gasto
                rec_id = int(intencao.split(":")[1])
                rec = await buscar_recorrente(rec_id, numero)
                if not rec:
                    return {"type": "text", "content": "❌ Recorrente não encontrado."}
                hoje = date.today()
                gasto_dados = {
                    "descricao": rec["descricao"],
                    "valor": float(rec["valor"]),
                    "categoria": rec["categoria"],
                    "forma_pagamento": rec["forma_pagamento"] or "Desconhecido",
                    "data": hoje,
                    "hashtag": f"#rec{rec_id}",
                }
                gasto_id = await salvar_gasto(numero, gasto_dados, fonte="recorrente")
                await salvar_memoria(numero, ultimo_gasto_id=gasto_id)
                alerta = await verificar_limite_pos_gasto(numero, rec["categoria"]) or ""
                from src.services.report_service import EMOJI_CATEGORIA as _EMOJI
                emoji = _EMOJI.get(rec["categoria"], "📦")
                return {
                    "type": "text",
                    "content": (
                        f"✅ *Pagamento registrado!*\n\n"
                        f"{emoji} {rec['descricao']}\n"
                        f"💰 R$ {float(rec['valor']):.2f}\n"
                        f"🏷️ {rec['categoria']}\n"
                        f"📅 {hoje.strftime('%d/%m/%Y')}{alerta}\n\n"
                        f"_Te aviso de novo no mês que vem 🐹_"
                    )
                }

    # ── Resumo por intervalo (hoje/ontem/dia X/etc) ──────────────────────────
    if texto_lower.startswith("resumo"):
        intervalo = _parse_resumo_intervalo(texto)
        if intervalo:
            ini, fim, titulo = intervalo
            conteudo, gastos = await gerar_resumo_intervalo(numero, ini, fim, titulo)
            _resumo_gastos[numero] = gastos
            return {"type": "text", "content": conteudo}

    # ── Resumo (mês) ──────────────────────────────────────────────────────────
    if texto_lower.startswith("resumo") or texto_lower in ["relatorio", "relatório", "gastos", "ver gastos"]:
        ano, mes = _parse_mes_resumo(texto_lower)
        conteudo, gastos = await gerar_resumo(numero, ano=ano, mes=mes)
        _resumo_gastos[numero] = gastos
        return {"type": "text", "content": conteudo}

    # ── Remover pelo número do resumo: "remover 2" ────────────────────────────
    match_rem_num = re.match(r"^remover\s+(\d+)$", texto_lower)
    if match_rem_num:
        idx = int(match_rem_num.group(1)) - 1
        gastos = _resumo_gastos.get(numero, [])
        if not gastos:
            return {"type": "text", "content": "❌ Faça um *resumo* primeiro para ver os gastos numerados."}
        if idx < 0 or idx >= len(gastos):
            return {"type": "text", "content": f"❌ Número inválido. Escolha entre 1 e {len(gastos)}."}
        g = gastos[idx]
        ok = await deletar_gasto(g["id"], numero)
        if ok:
            _resumo_gastos[numero] = [x for x in gastos if x["id"] != g["id"]]
            return {"type": "text", "content": f"🗑️ *Gasto removido!*\n\n_{g['descricao']} · R$ {float(g['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Remover última entrada: "remover entrada" ─────────────────────────────
    if texto_lower == "remover entrada":
        mem = await buscar_memoria(numero)
        entrada_id = mem["ultima_entrada_id"]
        if not entrada_id:
            return {"type": "text", "content": "❌ Nenhuma entrada recente para remover."}
        e = await buscar_entrada_por_id(entrada_id, numero)
        if not e:
            return {"type": "text", "content": "❌ Entrada não encontrada ou já foi removida."}
        ok = await deletar_entrada(entrada_id, numero)
        if ok:
            await limpar_memoria_entrada(numero)
            return {"type": "text", "content": f"🗑️ *Entrada removida!*\n\n_{e['descricao']} · R$ {float(e['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Remover último gasto: "remover" ───────────────────────────────────────
    if texto_lower == "remover":
        mem = await buscar_memoria(numero)
        lote = mem["lote_gastos_ids"]
        if lote:
            removidos = []
            for gid in lote:
                g = await buscar_gasto_por_id(gid, numero)
                if g:
                    ok = await deletar_gasto(gid, numero)
                    if ok:
                        removidos.append(f"_{g['descricao']} · R$ {float(g['valor']):.2f}_")
            await limpar_memoria_gasto(numero)
            if removidos:
                return {"type": "text", "content": "🗑️ *LOTE REMOVIDO!*"}
            return {"type": "text", "content": "❌ Não consegui remover os gastos. Tente novamente."}

        gasto_id = mem["ultimo_gasto_id"]
        if not gasto_id:
            return {"type": "text", "content": "❌ Nenhum gasto recente para remover.\nUse *resumo* para ver seus gastos e remover pelo número."}
        g = await buscar_gasto_por_id(gasto_id, numero)
        if not g:
            return {"type": "text", "content": "❌ Gasto não encontrado ou já foi removido."}
        ok = await deletar_gasto(gasto_id, numero)
        if ok:
            await limpar_memoria_gasto(numero)
            return {"type": "text", "content": f"🗑️ *Gasto removido!*\n\n_{g['descricao']} · R$ {float(g['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Editar pelo número do resumo: "editar 2 Uber 50 cartão" ──────────────
    match_edit_num = re.match(r"^editar\s+(\d+)\s+(.+)$", texto_lower)
    if match_edit_num:
        idx = int(match_edit_num.group(1)) - 1
        novo_texto = match_edit_num.group(2)
        gastos = _resumo_gastos.get(numero, [])
        if not gastos:
            return {"type": "text", "content": "❌ Faça um *resumo* primeiro para ver os gastos numerados."}
        if idx < 0 or idx >= len(gastos):
            return {"type": "text", "content": f"❌ Número inválido. Escolha entre 1 e {len(gastos)}."}
        try:
            novos_dados = await _extrair(novo_texto)
            ok = await atualizar_gasto(gastos[idx]["id"], numero, novos_dados)
            if ok:
                return {"type": "text", "content": f"✏️ *Gasto atualizado!*\n\n📍 {novos_dados['descricao']}\n💰 R$ {float(novos_dados['valor']):.2f}\n🏷️ {novos_dados['categoria']}\n💳 {novos_dados['forma_pagamento']}"}
        except Exception as e:
            print(f"❌ Erro ao editar: {e}")
        return {"type": "text", "content": "❌ Não consegui editar. Tente: *editar 2 Uber 50 cartão*"}

    # ── Editar último gasto: "editar" ou "editar Uber 50 cartão" ─────────────
    match_edit = re.match(r"^editar\s+(.+)$", texto_lower)
    if match_edit or texto_lower == "editar":
        mem = await buscar_memoria(numero)
        gasto_id = mem["ultimo_gasto_id"]
        if not gasto_id:
            return {"type": "text", "content": "❌ Nenhum gasto recente para editar.\nUse *resumo* e depois *editar 2 Uber 50 cartão*."}
        if texto_lower == "editar":
            g = await buscar_gasto_por_id(gasto_id, numero)
            return {"type": "text", "content": f"✏️ Para editar o último gasto, responda:\n*editar {g['descricao']} [novo valor] [forma pagamento]*\n\nEx: *editar Uber 55 pix*"}
        try:
            novo_texto = match_edit.group(1)
            novos_dados = await _extrair(novo_texto)
            ok = await atualizar_gasto(gasto_id, numero, novos_dados)
            if ok:
                return {"type": "text", "content": f"✏️ *Gasto atualizado!*\n\n📍 {novos_dados['descricao']}\n💰 R$ {float(novos_dados['valor']):.2f}\n🏷️ {novos_dados['categoria']}\n💳 {novos_dados['forma_pagamento']}"}
        except Exception as e:
            print(f"❌ Erro ao editar: {e}")
        return {"type": "text", "content": "❌ Não consegui editar. Tente: *editar Uber 55 pix*"}

    # ── Limite por categoria: "limite roupas 200", "limite alimentação 800" ──
    # IMPORTANTE: precisa vir ANTES de "limite NUMERO" pra não capturar errado
    CATS = ["alimentação", "alimentacao", "transporte", "moradia", "saúde", "saude",
            "lazer", "vestuário", "vestuario", "roupas", "educação", "educacao", "outros"]
    cat_alias = {
        "alimentacao": "Alimentação", "alimentação": "Alimentação",
        "transporte": "Transporte", "moradia": "Moradia",
        "saude": "Saúde", "saúde": "Saúde",
        "lazer": "Lazer",
        "vestuario": "Vestuário", "vestuário": "Vestuário", "roupas": "Vestuário",
        "educacao": "Educação", "educação": "Educação",
        "outros": "Outros"
    }
    match_lim_cat = re.match(
        r"^limite\s+(" + "|".join(CATS) + r")\s+([\d.,]+)",
        texto_lower
    )
    if match_lim_cat:
        cat_user = match_lim_cat.group(1)
        try:
            valor = float(match_lim_cat.group(2).replace(",", "."))
            if valor <= 0:
                return {"type": "text", "content": "❌ Valor inválido. Ex: *limite roupas 200*"}
            categoria = cat_alias.get(cat_user, "Outros")
            return {"type": "text", "content": await definir_limite_categoria_msg(numero, categoria, valor)}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: *limite roupas 200*"}

    # ── Listar limites por categoria ──────────────────────────────────────────
    if texto_lower in ("limites", "meus limites", "ver limites", "listar limites"):
        return {"type": "text", "content": await listar_limites_categoria_formatado(numero)}

    # ── Remover limite de categoria ──────────────────────────────────────────
    match_rem_lim = re.match(
        r"^remover\s+limite\s+(" + "|".join(CATS) + r")",
        texto_lower
    )
    if match_rem_lim:
        cat_user = match_rem_lim.group(1)
        categoria = cat_alias.get(cat_user, "Outros")
        return {"type": "text", "content": await remover_limite_categoria_msg(numero, categoria)}

    # ── Limite mensal geral: "limite 2000" ────────────────────────────────────
    match_lim = re.match(r"^limite\s+([\d.,]+)\s*$", texto_lower)
    if match_lim:
        try:
            valor = float(match_lim.group(1).replace(",", "."))
            return {"type": "text", "content": await definir_limite(numero, valor)}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: *limite 2000*"}

    # ── Recorrentes: listar ───────────────────────────────────────────────────
    if texto_lower in ("recorrentes", "meus recorrentes", "ver recorrentes", "listar recorrentes"):
        return {"type": "text", "content": await listar_recorrentes_formatado(numero)}

    # ── Recorrentes: cancelar ─────────────────────────────────────────────────
    match_cancel_rec = re.match(r"^cancelar\s+recorrente\s+(\d+)$", texto_lower)
    if match_cancel_rec:
        rec_id = int(match_cancel_rec.group(1))
        rec = await buscar_recorrente(rec_id, numero)
        if not rec:
            return {"type": "text", "content": f"❌ Recorrente #{rec_id} não encontrado."}
        ok = await cancelar_recorrente(rec_id, numero)
        if ok:
            return {"type": "text", "content": f"🗑️ *Recorrente cancelado!*\n\n_{rec['descricao']} · R$ {float(rec['valor']):.2f}_\n\n_Não vou mais te avisar todo mês._"}
        return {"type": "text", "content": "❌ Não consegui cancelar."}

    # ── Parcelas: listar ──────────────────────────────────────────────────────
    if texto_lower in ("parcelas", "minhas parcelas", "ver parcelas", "listar parcelas"):
        return {"type": "text", "content": await listar_parcelas_formatado(numero)}

    # ── Parcelas: cancelar ────────────────────────────────────────────────────
    match_cancel_parc = re.match(r"^cancelar\s+parcela\s+(\d+)$", texto_lower)
    if match_cancel_parc:
        parc_id = int(match_cancel_parc.group(1))
        parc = await buscar_parcela(parc_id, numero)
        if not parc:
            return {"type": "text", "content": f"❌ Parcela #{parc_id} não encontrada."}
        ok = await cancelar_parcela(parc_id, numero)
        if ok:
            atual = parc["parcela_atual"]
            total = parc["num_parcelas"]
            return {
                "type": "text",
                "content": (
                    f"🗑️ *Parcelas futuras canceladas!*\n\n"
                    f"_{parc['descricao']}_\n"
                    f"📊 {atual}/{total} já pagas\n\n"
                    f"_Não vou mais lançar parcelas dessa compra._\n"
                    f"_Os gastos já lançados continuam no histórico._"
                )
            }
        return {"type": "text", "content": "❌ Não consegui cancelar."}

    # ── Lembrete: "meus lembretes" ou "ver lembretes" ────────────────────────
    if re.search(r"\b(meus lembretes|ver lembretes|listar lembretes)\b", texto_lower):
        lembretes = await buscar_lembretes_pendentes(numero)
        if not lembretes:
            return {"type": "text", "content": "📭 Você não tem lembretes agendados no momento.\n\nPara criar um:\n_'Me lembre da reunião hoje às 14:00'_"}
        from zoneinfo import ZoneInfo
        TZ_BR = ZoneInfo("America/Sao_Paulo")
        linhas = ["🔔 *Seus lembretes pendentes:*\n"]
        for i, l in enumerate(lembretes, 1):
            h = l["horario"].astimezone(TZ_BR)
            linhas.append(f"{i}. 📌 {l['mensagem'].capitalize()}\n   ⏰ {h.strftime('%d/%m às %H:%M')} · ID #{l['id']}")
        linhas.append("\n_Para cancelar: *cancelar lembrete [ID]*_")
        return {"type": "text", "content": "\n".join(linhas)}

    # ── Lembrete: cancelar "cancelar lembrete 3" ─────────────────────────────
    match_cancel = re.match(r"^cancelar lembrete\s+(\d+)$", texto_lower)
    if match_cancel:
        lembrete_id = int(match_cancel.group(1))
        ok = await cancelar_lembrete(lembrete_id, numero)
        if ok:
            return {"type": "text", "content": f"🗑️ *Lembrete #{lembrete_id} cancelado!*"}
        return {"type": "text", "content": f"❌ Lembrete #{lembrete_id} não encontrado ou já foi enviado."}

    # ── Detecção de RECORRENTE (antes de gasto comum) ────────────────────────
    if detectar_recorrente(texto):
        dados = await parsear_recorrente(texto, _grok)
        if dados:
            return {"type": "text", "content": await criar_recorrente(numero, dados)}
        return {
            "type": "text",
            "content": (
                "🔁 Não consegui entender o recorrente. Tenta assim:\n"
                "_'Todo dia 10 faculdade 120'_\n"
                "_'Aluguel 1500 todo dia 5'_\n"
                "_'Academia 80 todo mês dia 15 cartão'_"
            )
        }

    # ── Detecção de PARCELADO (antes de gasto comum) ─────────────────────────
    if detectar_parcelado(texto):
        dados = await parsear_parcelado(texto, _grok)
        if dados:
            return {"type": "text", "content": await criar_parcelado(numero, dados)}
        return {
            "type": "text",
            "content": (
                "💳 Não consegui entender a compra parcelada. Tenta assim:\n"
                "_'TV 1200 em 6x cartão'_\n"
                "_'Sapato 300 parcelado em 3x'_\n"
                "_'Geladeira 12x de 250'_"
            )
        }

    # ── Lembrete: criar com palavra-chave ("me lembre...", "lembra de...") ───
    if _detectar_lembrete_rapido(texto):
        resposta = await processar_lembrete(texto, numero)
        return {"type": "text", "content": resposta}

    # ── Lembrete: criar sem palavra-chave ("reunião amanhã 12:00") ───────────
    if await detectar_lembrete_implicito(texto):
        resposta = await processar_lembrete(texto, numero)
        return {"type": "text", "content": resposta}

    # ── Limpa intenção pendente se usuário mandou outra coisa ────────────────
    mem_check = await buscar_memoria(numero)
    if mem_check.get("intencao_pendente"):
        await limpar_intencao_pendente(numero)

    # ── Registrar entrada ─────────────────────────────────────────────────────
    if await _detectar_entrada(texto):
        try:
            dados = await processar_entrada_texto(texto)
            entrada_id = await salvar_entrada(numero, dados, fonte="texto")
            await salvar_memoria(numero, ultima_entrada_id=entrada_id)
            card = CARD_ENTRADA.format(
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                categoria=dados.get("categoria", "Outros"),
                data=dados["data"],
                hashtag=dados["hashtag"],
            )
            return {"type": "text", "content": card}
        except Exception as e:
            print(f"❌ Erro ao processar entrada: {e}")
            return {
                "type": "text",
                "content": (
                    "😅 Não entendi essa entrada. Tente algo como:\n"
                    "_'salário 3000'_ ou _'recebi freelance 500'_"
                )
            }

    # ── Múltiplos gastos agrupados (uma linha por gasto) ──────────────────────
    linhas = [l.strip() for l in texto.strip().splitlines() if l.strip()]
    if len(linhas) > 1:
        linhas_com_numero = [l for l in linhas if re.search(r"\d", l)]
        contexto_lote = " ".join(l for l in linhas if not re.search(r"\d", l))
        if len(linhas_com_numero) >= 2:
            cards = []
            falhas = []
            ids_registrados = []
            for linha in linhas_com_numero:
                try:
                    dados = await processar_gasto_texto(linha, contexto=contexto_lote)
                    gasto_id = await salvar_gasto(numero, dados, fonte="texto")
                    ids_registrados.append(gasto_id)
                    alerta = await verificar_limite_pos_gasto(numero, dados.get("categoria")) or ""
                    card = CARD_GASTO.format(
                        descricao=dados["descricao"],
                        valor=float(dados["valor"]),
                        categoria=dados["categoria"],
                        forma_pagamento=dados["forma_pagamento"],
                        data=dados["data"],
                        hashtag=dados["hashtag"],
                        alerta=alerta,
                    )
                    cards.append(card)
                except Exception as e:
                    print(f"⚠️ Falha ao processar linha '{linha}': {e}")
                    falhas.append(f"❌ Não entendi: _{linha}_")

            if ids_registrados:
                await salvar_memoria(numero, ultimo_gasto_id=ids_registrados[-1], lote_ids=ids_registrados)

            partes = cards[:]
            if falhas:
                partes.extend(falhas)
            return {"type": "text", "content": "\n\n─────────────\n\n".join(partes)}

    # ── Detecção de intenção → Registrar gasto ────────────────────────────────
    intencao = await _detectar_intencao(texto)

    if intencao != "GASTO":
        try:
            import re as _re
            import json as _json
            resp = await _grok.chat.completions.create(
                model="grok-4-1-fast-non-reasoning",
                max_tokens=150,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você classifica mensagens de WhatsApp de um app financeiro. "
                            "Responda APENAS com JSON válido, sem markdown:\n"
                            "{\"acao\": \"TIPO\", \"valor\": \"...\"}\n\n"
                            "Tipos possíveis:\n"
                            "- LEMBRETE: ação futura com data/hora. valor = texto original completo\n"
                            "- LIMITE: definir teto de gastos mensais. valor = número extraído\n"
                            "- RESUMO: ver gastos do mês. valor = \"\"\n"
                            "- ENTRADA: dinheiro recebido. valor = texto original completo\n"
                            "- OUTRO: qualquer outra coisa. valor = resposta curta e simpática em 1 linha\n\n"
                            "Exemplos:\n"
                            "'pagar boleto amanhã 8h' → {\"acao\": \"LEMBRETE\", \"valor\": \"pagar boleto amanhã 8h\"}\n"
                            "'consulta sexta 15h' → {\"acao\": \"LEMBRETE\", \"valor\": \"consulta sexta 15h\"}\n"
                            "'quero gastar 500 esse mês' → {\"acao\": \"LIMITE\", \"valor\": \"500\"}\n"
                            "'quanto gastei?' → {\"acao\": \"RESUMO\", \"valor\": \"\"}\n"
                            "'recebi 1000 de freelance' → {\"acao\": \"ENTRADA\", \"valor\": \"recebi 1000 de freelance\"}\n"
                            "'boa noite' → {\"acao\": \"OUTRO\", \"valor\": \"Boa noite! 🐹 Digite *ajuda* para ver os meus comandos válidos.\"}"
                        ),
                    },
                    {"role": "user", "content": texto},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            raw = _re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
            classificacao = _json.loads(raw)
            acao = classificacao.get("acao", "OUTRO")
            valor = classificacao.get("valor", "")

            if acao == "LEMBRETE":
                resposta = await processar_lembrete(valor, numero)
                return {"type": "text", "content": resposta}

            elif acao == "LIMITE":
                try:
                    v = float(str(valor).replace(",", "."))
                    return {"type": "text", "content": await definir_limite(numero, v)}
                except:
                    return {"type": "text", "content": "😅 Não entendi o valor. Ex: _limite 2000_"}

            elif acao == "RESUMO":
                hoje = date.today()
                conteudo, gastos = await gerar_resumo(numero, ano=hoje.year, mes=hoje.month)
                _resumo_gastos[numero] = gastos
                return {"type": "text", "content": conteudo}

            elif acao == "ENTRADA":
                try:
                    dados = await processar_entrada_texto(valor)
                    entrada_id = await salvar_entrada(numero, dados, fonte="texto")
                    await salvar_memoria(numero, ultima_entrada_id=entrada_id)
                    card = CARD_ENTRADA.format(
                        descricao=dados["descricao"],
                        valor=float(dados["valor"]),
                        categoria=dados.get("categoria", "Outros"),
                        data=dados["data"],
                        hashtag=dados["hashtag"],
                    )
                    return {"type": "text", "content": card}
                except:
                    return {"type": "text", "content": "😅 Não entendi essa entrada. Ex: _salário 3000_"}

            else:
                return {"type": "text", "content": valor or "😅 Não entendi. Digite *ajuda* para ver os comandos."}

        except Exception as e:
            print(f"⚠️ Erro na classificação inteligente: {e}")
            return {"type": "text", "content": "😅 Não entendi. Tenta assim: _'iFood 45 cartão'_ ou _'Uber 22 pix'_\n\nDigite *ajuda* para ver todos os comandos."}

    try:
        dados = await processar_gasto_texto(texto)
        gasto_id = await salvar_gasto(numero, dados, fonte="texto")
        await salvar_memoria(numero, ultimo_gasto_id=gasto_id)

        alerta = await verificar_limite_pos_gasto(numero, dados.get("categoria")) or ""

        card = CARD_GASTO.format(
            descricao=dados["descricao"],
            valor=float(dados["valor"]),
            categoria=dados["categoria"],
            forma_pagamento=dados["forma_pagamento"],
            data=dados["data"],
            hashtag=dados["hashtag"],
            alerta=alerta
        )
        return {"type": "text", "content": card}
    except Exception as e:
        print(f"❌ Erro ao processar texto: {e}")
        return {
            "type": "text",
            "content": (
                "😅 Não entendi esse gasto. Tente algo como:\n"
                "_'iFood 45 cartão'_ ou _'Gasolina 120 pix'_\n\n"
                "Digite *ajuda* para ver todos os comandos."
            )
        }
