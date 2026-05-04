import re
from datetime import date, timedelta
from src.services.ia_service import processar_gasto_texto, processar_entrada_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.services.reminder_service import (
    _detectar_lembrete_rapido,
    detectar_lembrete_implicito,
    processar_lembrete,
    buscar_lembretes_pendentes,
    cancelar_lembrete,
)
from src.core.database import (
    salvar_gasto, deletar_gasto, atualizar_gasto, buscar_gasto_por_id,
    salvar_entrada, deletar_entrada, buscar_entrada_por_id,
    salvar_memoria, buscar_memoria, limpar_memoria_gasto, limpar_memoria_entrada
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
Vou cuidar da sua grana com você, combinado? 😄

💸 *Pra registrar um gasto:*
É só mandar algo como:
_"Uber 27"_ ou _"Almoço 35 cartão"_
(pode ser áudio ou foto também, eu entendo tudinho 👀)

💰 *Entrou dinheiro?*
_"Salário 3000"_ ou _"Recebi 500"_

📊 *Quer ver como tá indo?*
Digite: *resumo*
ou acompanhe tudo no seu dashboard:
👉 https://dashboard-porquim-theta.vercel.app

🔔 *Também te ajudo com lembretes!*
Ex: _"Tenho reunião hoje 14h"_
e eu te aviso na hora certa ⏰

❓ *Precisou de ajuda?*
Digite: *suporte*

Um hábito simples que muda tudo 💚"""

SUPORTE = """🙋 *Precisa de ajuda?*

Fala comigo diretamente! Respondo o mais rápido possível 😊

👉 wa.me/5531991316890

_Horário de atendimento: seg a sex, 9h às 18h_"""

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
# último gasto/entrada e lote agora persistidos no banco (memoria_usuario)


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


# ─── Parser de mês ───────────────────────────────────────────────────────────

def _parse_mes_resumo(texto: str):
    hoje = date.today()
    resto = re.sub(r"^resumo\s*", "", texto.lower().strip()).strip()

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


# ─── Handler principal ────────────────────────────────────────────────────────

async def handle_text_message(message: dict) -> dict:
    texto = message["text"]["body"].strip()
    numero = message["key"]["remoteJid"].split("@")[0]
    texto_lower = texto.lower()

    # ── Ajuda ─────────────────────────────────────────────────────────────────
    if texto_lower in ["oi", "oie", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

    # ── Suporte ───────────────────────────────────────────────────────────────
    if texto_lower in ["suporte", "ajuda suporte", "falar com suporte", "atendimento"]:
        return {"type": "text", "content": SUPORTE}

    # ── Resumo ────────────────────────────────────────────────────────────────
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

    # ── Limite ────────────────────────────────────────────────────────────────
    if texto_lower == "limite":
        return {"type": "text", "content": (
            "💳 *Limite mensal*\n\n"
            "Para definir seu limite de gastos do mês, manda assim:\n"
            "_limite_ seguido do valor. Exemplo:\n\n"
            "*limite 2000*\n\n"
            "Assim que você atingir 80% e 100% do limite, te aviso automaticamente aqui no WhatsApp 🔔"
        )}
        
    match_lim = re.match(r"^limite\s+([\d.,]+)", texto_lower)
    if match_lim:
        try:
            valor = float(match_lim.group(1).replace(",", "."))
            return {"type": "text", "content": await definir_limite(numero, valor)}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: _limite 2000_"}

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

    # ── Lembrete: criar com palavra-chave ("me lembre...", "lembra de...") ───
    if _detectar_lembrete_rapido(texto):
        resposta = await processar_lembrete(texto, numero)
        return {"type": "text", "content": resposta}

    # ── Lembrete: criar sem palavra-chave ("reunião amanhã 12:00") ───────────
    if await detectar_lembrete_implicito(texto):
        resposta = await processar_lembrete(texto, numero)
        return {"type": "text", "content": resposta}

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
        # Verifica se parece uma lista de gastos: pelo menos 2 linhas com número
        linhas_com_numero = [l for l in linhas if re.search(r"\d", l)]
        # Linhas sem número servem de contexto (ex: "Meus gastos no mês de maio")
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
                    alerta = await verificar_limite_pos_gasto(numero) or ""
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
        return {"type": "text", "content": "😅 Não entendi. Tenta assim: _'iFood 45 cartão'_ ou _'Uber 22 pix'_\n\nDigite *ajuda* para ver todos os comandos."}

    try:
        dados = await processar_gasto_texto(texto)
        gasto_id = await salvar_gasto(numero, dados, fonte="texto")
        await salvar_memoria(numero, ultimo_gasto_id=gasto_id)

        alerta = await verificar_limite_pos_gasto(numero) or ""

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
