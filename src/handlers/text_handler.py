import re
from datetime import date, timedelta
from src.services.ia_service import processar_gasto_texto, processar_entrada_texto
from src.services.report_service import gerar_resumo, definir_limite, verificar_limite_pos_gasto
from src.core.database import (
    salvar_gasto, deletar_gasto, atualizar_gasto, buscar_gasto_por_id,
    salvar_entrada, deletar_entrada, buscar_entrada_por_id
)
from src.services.ia_service import processar_gasto_texto as _extrair
from src.core.config import settings
from openai import AsyncOpenAI

# ─── Cliente Grok ─────────────────────────────────────────────────────────────
_grok = AsyncOpenAI(
    api_key=settings.GROK_API_KEY,
    base_url="https://api.x.ai/v1",
)

AJUDA = """👋 *Olá! Sou o MAYCON* 🤖
_Seu assistente financeiro no WhatsApp!_

━━━━━━━━━━━━━━━━━━
📝 *REGISTRAR GASTO*
━━━━━━━━━━━━━━━━━━
Manda qualquer gasto no texto:
- _"iFood 45 cartão"_
- _"Uber 23,50 pix"_
- _"Farmácia 89 dinheiro"_
- _"Aluguel 1200"_

🎤 *Áudio:* Fala o gasto!
_"Gastei 50 reais no mercado com cartão"_

📷 *Foto:* Manda foto do comprovante!
_O MAYCON lê e registra automático_

━━━━━━━━━━━━━━━━━━
💵 *REGISTRAR ENTRADA*
━━━━━━━━━━━━━━━━━━
Manda qualquer entrada de dinheiro:
- _"salário 3000"_
- _"recebi freelance 500"_
- _"me pagaram 800"_
- _"reembolso 150"_

━━━━━━━━━━━━━━━━━━
📊 *VER RELATÓRIOS*
━━━━━━━━━━━━━━━━━━
- *resumo* → mês atual
- *resumo mês passado* → mês anterior
- *resumo janeiro* → mês específico
- *resumo janeiro 2025* → mês e ano

━━━━━━━━━━━━━━━━━━
✏️ *EDITAR / REMOVER*
━━━━━━━━━━━━━━━━━━
Após registrar um gasto:
- *remover* → remove o último gasto
- *editar* → edita o último gasto

No resumo, pelo número:
- *remover 2* → remove o gasto 2️⃣
- *editar 2 Uber 50 cartão* → edita o gasto 2️⃣

Para entradas:
- *remover entrada* → remove a última entrada

━━━━━━━━━━━━━━━━━━
💳 *LIMITE MENSAL*
━━━━━━━━━━━━━━━━━━
- *limite 2000* → define seu limite
_Te aviso quando passar de 80% e 100%!_

━━━━━━━━━━━━━━━━━━
- *ajuda* ou *menu* → mostra este guia
━━━━━━━━━━━━━━━━━━
Bora controlar as finanças? 🚀"""

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

# Memória em RAM: último gasto/entrada por usuário e lista do resumo
_ultimo_gasto: dict[str, int] = {}
_ultima_entrada: dict[str, int] = {}
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

    # Sem número → não é entrada
    if not re.search(r"\d", t):
        return False

    # Palavras-chave explícitas com número → entrada direta, sem chamar API
    if re.search(
        r"\b(recebi|receber|salário|salario|freelance|renda|ganho|ganhei|"
        r"entrou|pagaram|me pagou|me pagaram|reembolso|investimento|dividendo)\b", t
    ):
        return True

    # Casos ambíguos → Grok decide
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
    if texto_lower in ["oi", "olá", "ola", "start", "ajuda", "help", "menu", "inicio", "início"]:
        return {"type": "text", "content": AJUDA}

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
        entrada_id = _ultima_entrada.get(numero)
        if not entrada_id:
            return {"type": "text", "content": "❌ Nenhuma entrada recente para remover."}
        e = await buscar_entrada_por_id(entrada_id, numero)
        if not e:
            return {"type": "text", "content": "❌ Entrada não encontrada ou já foi removida."}
        ok = await deletar_entrada(entrada_id, numero)
        if ok:
            _ultima_entrada.pop(numero, None)
            return {"type": "text", "content": f"🗑️ *Entrada removida!*\n\n_{e['descricao']} · R$ {float(e['valor']):.2f}_"}
        return {"type": "text", "content": "❌ Não consegui remover. Tente novamente."}

    # ── Remover último gasto: "remover" ───────────────────────────────────────
    if texto_lower == "remover":
        gasto_id = _ultimo_gasto.get(numero)
        if not gasto_id:
            return {"type": "text", "content": "❌ Nenhum gasto recente para remover.\nUse *resumo* para ver seus gastos e remover pelo número."}
        g = await buscar_gasto_por_id(gasto_id, numero)
        if not g:
            return {"type": "text", "content": "❌ Gasto não encontrado ou já foi removido."}
        ok = await deletar_gasto(gasto_id, numero)
        if ok:
            _ultimo_gasto.pop(numero, None)
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
        gasto_id = _ultimo_gasto.get(numero)
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
    match_lim = re.match(r"^limite\s+([\d.,]+)", texto_lower)
    if match_lim:
        try:
            valor = float(match_lim.group(1).replace(",", "."))
            return {"type": "text", "content": await definir_limite(numero, valor)}
        except ValueError:
            return {"type": "text", "content": "❌ Valor inválido. Ex: _limite 2000_"}

    # ── Registrar entrada ─────────────────────────────────────────────────────
    if await _detectar_entrada(texto):
        try:
            dados = await processar_entrada_texto(texto)
            entrada_id = await salvar_entrada(numero, dados, fonte="texto")
            _ultima_entrada[numero] = entrada_id
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

    # ── Detecção de intenção → Registrar gasto ────────────────────────────────
    intencao = await _detectar_intencao(texto)

    if intencao != "GASTO":
        return {"type": "text", "content": AJUDA}

    try:
        dados = await processar_gasto_texto(texto)
        gasto_id = await salvar_gasto(numero, dados, fonte="texto")
        _ultimo_gasto[numero] = gasto_id

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
