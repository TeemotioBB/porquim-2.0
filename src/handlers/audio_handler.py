import base64
import re
import httpx
from src.services.ia_service import processar_gasto_audio, transcrever_audio, processar_entrada_texto
from src.services.report_service import verificar_limite_pos_gasto
from src.services.recurring_service import detectar_recorrente, detectar_parcelado
from src.services.reminder_service import _detectar_lembrete_rapido, detectar_lembrete_implicito
from src.core.database import salvar_gasto, salvar_entrada, salvar_memoria
from src.core.config import settings

CARD_AUDIO = """✅ *Gasto Registrado por Áudio!* 🎤

🗣️ _"{transcricao}"_

📍 {descricao}
💰 R$ {valor:.2f}
🏷️ {categoria}
💳 {forma_pagamento}
📅 {data}
🔖 {hashtag}{alerta}

_Salvo com sucesso!_ 🎉
_Para remover este gasto responda: *remover*_
_Para editar responda: *editar*_"""

CARD_AUDIO_ENTRADA = """✅ *Entrada Registrada por Áudio!* 🎤

🗣️ _"{transcricao}"_

📍 {descricao}
💵 R$ {valor:.2f}
🏷️ {categoria}
📅 {data}
🔖 {hashtag}

_Salvo com sucesso!_ 🎉
_Para remover esta entrada responda: *remover entrada*_"""


def _detectar_entrada_texto(texto: str) -> bool:
    """Detecta se o texto transcrito é uma entrada de dinheiro."""
    t = texto.strip().lower()
    return bool(re.search(
        r"\b(recebi|receber|salário|salario|freelance|renda|ganho|ganhei|"
        r"entrou|pagaram|me pagou|me pagaram|reembolso|investimento|dividendo)\b", t
    ))


def _normalizar_comando_audio(transcricao: str) -> str:
    """
    Normaliza uma transcrição de áudio para parecer com um comando textual.

    Pessoas falam por áudio em linguagem natural ("Me dê o resumo do dia 15",
    "Pode mostrar meus limites?"), enquanto o text_handler espera comandos
    mais diretos ("resumo dia 15", "meus limites"). Esta função:
      1. Coloca em minúsculo e tira espaços/pontuação extras.
      2. Remove iterativamente prefixos conversacionais comuns
         ("pode", "me dê", "quero ver", "mostrar", "o", "a", etc.).
    A ideia é só remover ruído conversacional, sem inferir intenção.
    """
    if not transcricao:
        return ""
    t = transcricao.lower().strip()
    # Tira pontuação no fim/início (Whisper sempre coloca "." em frases curtas)
    t = re.sub(r"[.!?,;]+$", "", t).strip()
    t = re.sub(r"^[.!?,;]+", "", t).strip()

    # Lista de prefixos conversacionais a remover.
    # São ordenados automaticamente do maior pro menor antes de aplicar,
    # garantindo que "mostra meus" seja preferido a "mostra " quando ambos
    # encaixam.
    prefixos = [
        # Polidez longa
        "será que você poderia ", "sera que voce poderia ",
        "será que você pode ", "sera que voce pode ",
        "será que dá pra ", "sera que da pra ",
        "será que dá para ", "sera que da para ",
        "por gentileza ", "por gentileza, ",
        "por favor, ", "por favor ",
        # Verbos de pedido (você/tu pode...)
        "você poderia me ", "voce poderia me ",
        "você pode me ", "voce pode me ",
        "tu pode me ", "tu poderia me ",
        "você poderia ", "voce poderia ",
        "você pode ", "voce pode ",
        "tu pode ", "tu poderia ",
        "pode me ", "podia me ", "poderia me ",
        "pode ", "podia ", "poderia ",
        # Verbos de pedido pessoais ("eu quero", "queria")
        "eu queria ", "eu gostaria de ", "gostaria de ",
        "eu preciso ", "preciso ",
        # IMPORTANTE: NÃO removemos "quero " sozinho porque "quero gastar 500
        # com roupa" é a forma de definir limite por categoria — o
        # text_handler precisa do "quero gastar" intacto. Em vez disso,
        # listamos combinações específicas de "quero" + verbo de pedido.
        "eu quero ver os ", "quero ver os ",
        "eu quero ver as ", "quero ver as ",
        "eu quero ver o ", "quero ver o ",
        "eu quero ver a ", "quero ver a ",
        "eu quero ver meus ", "quero ver meus ",
        "eu quero ver minhas ", "quero ver minhas ",
        "eu quero ver meu ", "quero ver meu ",
        "eu quero ver minha ", "quero ver minha ",
        "eu quero ver ", "quero ver ",
        "eu quero saber ", "quero saber ",
        "eu quero o ", "quero o ",
        "eu quero a ", "quero a ",
        "eu quero os ", "quero os ",
        "eu quero as ", "quero as ",
        "eu quero meus ", "quero meus ",
        "eu quero minhas ", "quero minhas ",
        "eu quero meu ", "quero meu ",
        "eu quero minha ", "quero minha ",
        "eu quero um ", "quero um ",
        "eu quero uma ", "quero uma ",
        "vou querer ver os ", "vou querer ver as ", "vou querer ver o ", "vou querer ver a ",
        "vou querer os ", "vou querer as ", "vou querer o ", "vou querer a ",
        "vou querer meus ", "vou querer minhas ", "vou querer meu ", "vou querer minha ",
        "vou querer um ", "vou querer uma ",
        "vou querer ver ", "vou querer ",
        "queria ver ", "queria ",
        # Verbos imperativos: dar/mostrar/etc + artigo
        "me dê o ", "me de o ", "me da o ", "me dá o ",
        "me dê a ", "me de a ", "me da a ", "me dá a ",
        "me dê os ", "me de os ", "me da os ", "me dá os ",
        "me dê as ", "me de as ", "me da as ", "me dá as ",
        "me dê um ", "me de um ", "me da um ", "me dá um ",
        "me dê uma ", "me de uma ", "me da uma ", "me dá uma ",
        "me dê meus ", "me de meus ", "me da meus ", "me dá meus ",
        "me dê minhas ", "me de minhas ", "me da minhas ", "me dá minhas ",
        "me dê meu ", "me de meu ", "me da meu ", "me dá meu ",
        "me dê minha ", "me de minha ", "me da minha ", "me dá minha ",
        "me dê ", "me de ", "me da ", "me dá ",
        "dar o ", "dar a ", "dar os ", "dar as ",
        "dar meus ", "dar minhas ", "dar meu ", "dar minha ",
        "dar um ", "dar uma ", "dar ",
        "me mostre os ", "me mostra os ", "me mostre as ", "me mostra as ",
        "me mostre o ", "me mostra o ", "me mostre a ", "me mostra a ",
        "me mostre meus ", "me mostra meus ", "me mostre minhas ", "me mostra minhas ",
        "me mostre meu ", "me mostra meu ", "me mostre minha ", "me mostra minha ",
        "me mostre ", "me mostra ",
        "mostrar os ", "mostrar as ", "mostrar o ", "mostrar a ",
        "mostrar meus ", "mostrar minhas ", "mostrar meu ", "mostrar minha ",
        "mostrar um ", "mostrar uma ", "mostrar ",
        "mostre os ", "mostra os ", "mostre as ", "mostra as ",
        "mostre o ", "mostra o ", "mostre a ", "mostra a ",
        "mostre meus ", "mostra meus ", "mostre minhas ", "mostra minhas ",
        "mostre meu ", "mostra meu ", "mostre minha ", "mostra minha ",
        "mostre ", "mostra ",
        "me passa o ", "me passa a ", "me passa ",
        "me traz o ", "me traz a ", "me traz ",
        "me manda o ", "me manda a ", "me manda ",
        "me envia o ", "me envia a ", "me envia ",
        "ver os ", "ver as ", "ver o ", "ver a ",
        "ver meus ", "ver minhas ", "ver meu ", "ver minha ",
        "ver um ", "ver uma ", "ver ",
        # Saudação encadeada ("oi, me dá o resumo")
        "oi, ", "olá, ", "ola, ",
        "oi ", "olá ", "ola ", "ei ", "opa ", "hey ",
        "aí, ", "ai, ", "aí ", "ai ", "então, ", "entao, ", "então ", "entao ",
    ]
    # ORDENAÇÃO CRÍTICA: maior primeiro, pra que "mostra meus " seja preferido a "mostra "
    prefixos = sorted(set(prefixos), key=len, reverse=True)

    # Aplica em ciclo até estabilizar (cobre encadeamentos como "oi, pode me dar o")
    for _ in range(12):
        novo = t
        for p in prefixos:
            if novo.startswith(p):
                novo = novo[len(p):].strip()
                break
        if novo == t:
            break
        t = novo

    # Sufixos de cortesia
    t = re.sub(r"[.!?,;]+$", "", t).strip()
    t = re.sub(r"\s+por\s+favor$", "", t).strip()
    t = re.sub(r"\s+pra\s+mim$", "", t).strip()
    t = re.sub(r"\s+para\s+mim$", "", t).strip()
    t = re.sub(r"\s+aí$", "", t).strip()
    t = re.sub(r"\s+ai$", "", t).strip()

    return t


def _audio_sem_conteudo_util(transcricao: str) -> bool:
    """
    Retorna True se a transcrição estiver vazia, muito curta, sem letras,
    ou for puro ruído que o Whisper "alucina" em áudios mudos
    (ex: '.', '...', 'obrigado.', '[música]', 'thank you').
    """
    if not transcricao:
        return True
    t = transcricao.strip().lower()
    # Tira pontuação/símbolos pra avaliar conteúdo
    so_letras = re.sub(r"[^a-zA-ZáéíóúâêôãõçÁÉÍÓÚÂÊÔÃÕÇ]", "", t)
    if len(so_letras) < 3:
        return True
    # Whisper costuma alucinar essas frases em áudios mudos/silenciosos
    alucinacoes = (
        "thank you", "thanks for watching", "obrigado", "obrigada",
        "[música]", "[musica]", "(música)", "(musica)",
        "legendas pela comunidade", "amara.org",
        "subtítulos pela comunidade", "subtitulos pela comunidade",
        "transcribed by", "tradução", "traducao",
    )
    if t in alucinacoes:
        return True
    # Frases muito curtas e que são apenas alucinações + pontuação
    for a in alucinacoes:
        if t.replace(".", "").replace(",", "").replace("!", "").strip() == a:
            return True
    return False


def _extracao_invalida(dados: dict) -> bool:
    """Retorna True se a extração de gasto/entrada não tem dados úteis."""
    if not dados:
        return True
    try:
        valor = float(dados.get("valor", 0))
    except (TypeError, ValueError):
        return True
    desc = (dados.get("descricao") or "").strip()
    # Sem valor (ou valor 0) ou sem descrição = extração inútil
    if valor <= 0:
        return True
    if not desc or len(desc) < 2:
        return True
    return False


def _precisa_text_handler(transcricao: str) -> bool:
    """
    Decide se a transcrição deve ser roteada pelo text_handler ao invés
    do fluxo direto de gasto. True quando contém comandos, parcelado,
    recorrente, limite por categoria, resumos, lembretes, listagens, etc.

    IMPORTANTE: esta função precisa cobrir TODOS os comandos textuais
    suportados pelo text_handler, pra que qualquer um deles possa ser
    acionado também por áudio — INCLUINDO frases naturais como
    "Me dê o resumo do dia 15" ou "Pode mostrar meus limites?".
    """
    # Trabalha sobre a transcrição já normalizada (sem prefixos polidos)
    t = _normalizar_comando_audio(transcricao)
    if not t:
        return False

    # ── Comandos diretos: palavra única ou prefixo ────────────────────────────
    comandos_simples = (
        # Ajuda / saudações
        "ajuda", "menu", "ola", "olá", "oi", "start", "help",
        "inicio", "início",
        # Resumos / relatórios
        "resumo", "relatorio", "relatório", "gastos", "ver gastos",
        # Limites
        "limite", "limites",
        "meus limites", "ver limites", "listar limites",
        # Recorrentes / parcelas
        "recorrentes", "parcelas",
        "meus recorrentes", "ver recorrentes", "listar recorrentes",
        "minhas parcelas", "ver parcelas", "listar parcelas",
        # Operações em itens registrados
        "remover", "editar",
        "cancelar",
        # Suporte
        "suporte", "atendente", "atendimento", "contato",
        # Lembretes (listagem)
        "lembretes", "meus lembretes", "ver lembretes", "listar lembretes",
        # Reset (todas as variantes)
        "resetar", "reset",
        "apagar tudo", "quero apagar tudo", "pode apagar tudo",
        "zerar tudo", "quero zerar tudo",
        "limpar tudo", "quero limpar tudo",
    )
    if any(t == c or t.startswith(c + " ") for c in comandos_simples):
        return True

    # ── Confirmações / cancelamentos (curtinhos) ─────────────────────────────
    confirmacoes_cancelamentos = (
        "sim", "pode", "confirma", "confirmar", "isso", "quero", "vai", "ok",
        "bora", "yes", "s", "já paguei", "ja paguei", "paguei",
        "não", "nao", "cancela", "esquece", "deixa", "no",
        "ainda não", "ainda nao", "depois",
    )
    if t in confirmacoes_cancelamentos:
        return True

    # ── Parcelado / recorrente ────────────────────────────────────────────────
    if detectar_parcelado(t) or detectar_recorrente(t):
        return True

    # ── Limite (contém "limite", "teto", "máximo", "quero gastar") ────────────
    if re.search(r"\b(limite|teto|m[aá]ximo|quero\s+gastar|gastar\s+at[eé]|posso\s+gastar)\b", t):
        return True

    # ── Lembretes (criação) ───────────────────────────────────────────────────
    if _detectar_lembrete_rapido(t):
        return True

    # ── Frases que indicam comando mesmo sem prefixo direto ──────────────────
    # Suporte / pedido de ajuda humana
    if re.search(
        r"\b(preciso\s+de\s+ajuda|falar\s+com\s+(um\s+)?humano|fale\s+conosco|fala\s+conosco|"
        r"quero\s+falar\s+com\s+(um\s+)?humano|tem\s+algu[eé]m|atendimento\s+humano|"
        r"ajuda\s+humana|suporte\s+humano)\b",
        t
    ):
        return True
    # "apaga", "apagar", "deleta", "deletar" — sinônimos de remover
    if re.search(r"^(apaga(r)?|delet(a|ar)|exclu(i|ir))\b", t):
        return True

    return False


async def _baixar_audio_evolution(msg_data: dict) -> tuple[bytes | None, str]:
    mime_type = msg_data.get("message", {}).get("audioMessage", {}).get("mimetype", "audio/ogg")
    url = f"{settings.EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{settings.EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={"message": {"key": msg_data.get("key", {}), "message": msg_data.get("message", {})}},
                headers={"apikey": settings.EVOLUTION_API_KEY}
            )
            print(f"📥 Evolution getBase64: {resp.status_code}")
            if resp.status_code in [200, 201]:
                data = resp.json()
                b64 = data.get("base64") or data.get("data")
                if b64:
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    return base64.b64decode(b64), mime_type
    except Exception as e:
        print(f"❌ Erro download áudio Evolution: {e}")
    return None, mime_type


async def handle_audio_message(msg_data: dict, remote_jid: str, ultimo_gasto: dict) -> dict:
    numero = remote_jid.split("@")[0]
    audio_bytes, mime_type = await _baixar_audio_evolution(msg_data)

    if not audio_bytes:
        return {"type": "text", "content": "❌ Não consegui baixar o áudio. Tente enviar novamente ou descreva o gasto em texto."}

    try:
        # Transcreve o áudio primeiro
        transcricao = await transcrever_audio(audio_bytes, mime_type)
        print(f"🎤 Transcrição: {transcricao!r}")

        # ── Áudio mudo / sem conteúdo útil ──────────────────────────────────────
        if _audio_sem_conteudo_util(transcricao):
            return {
                "type": "text",
                "content": (
                    "🤔 Não consegui entender o que você falou no áudio.\n\n"
                    "Pode ter ficado mudo ou com pouco volume. Tenta de novo "
                    "falando algo como:\n"
                    "🎤 _\"gastei 45 reais no iFood com cartão\"_\n"
                    "🎤 _\"recebi 1000 de freelance\"_\n\n"
                    "Ou manda em texto mesmo 😉"
                )
            }

        # ── Roteamento inteligente: se for comando/parcelado/recorrente/limite/lembrete,
        # passa pelo text_handler que tem toda a lógica completa ──────────────────────
        if _precisa_text_handler(transcricao):
            print(f"🎤➡️📝 Roteando áudio pelo text_handler")
            # Import aqui pra evitar import circular
            from src.handlers.text_handler import handle_text_message
            # Normaliza a transcrição: tira pontuação final E prefixos
            # conversacionais ("me dê o", "pode mostrar", etc.) pra que o
            # text_handler reconheça o comando da mesma forma que reconhece
            # quando o usuário digita.
            texto_normalizado = _normalizar_comando_audio(transcricao)
            print(f"🎤 Texto normalizado: {texto_normalizado!r}")
            resposta = await handle_text_message({
                "text": {"body": texto_normalizado},
                "key": {"remoteJid": remote_jid}
            })
            # Adiciona o áudio transcrito no topo da resposta pra confirmar o que foi entendido
            if resposta and resposta.get("content"):
                resposta["content"] = f"🗣️ _\"{transcricao}\"_\n\n{resposta['content']}"
            return resposta

        # ── Verifica se é entrada de dinheiro ──
        if _detectar_entrada_texto(transcricao):
            try:
                dados = await processar_entrada_texto(transcricao)
            except Exception as e:
                print(f"⚠️ Falha ao extrair entrada do áudio: {e}")
                dados = None

            if _extracao_invalida(dados):
                return {
                    "type": "text",
                    "content": (
                        f"🗣️ _\"{transcricao}\"_\n\n"
                        "🤔 Entendi que parece uma entrada de dinheiro, mas "
                        "não consegui identificar o valor.\n\n"
                        "Tenta de novo falando o valor, ex:\n"
                        "🎤 _\"recebi 1000 de freelance\"_"
                    )
                }

            entrada_id = await salvar_entrada(numero, dados, fonte="audio")
            await salvar_memoria(numero, ultima_entrada_id=entrada_id)

            card = CARD_AUDIO_ENTRADA.format(
                transcricao=transcricao,
                descricao=dados["descricao"],
                valor=float(dados["valor"]),
                categoria=dados.get("categoria", "Outros"),
                data=dados["data"],
                hashtag=dados["hashtag"],
            )
            return {"type": "text", "content": card}

        # ── Caso contrário, processa como gasto comum ──
        try:
            dados = await processar_gasto_audio(audio_bytes, mime_type)
        except Exception as e:
            print(f"⚠️ Falha ao extrair gasto do áudio: {e}")
            dados = None

        # Se a extração resultar em valor 0 ou descrição vazia, NÃO registra
        # (era exatamente o bug: áudio mudo virava gasto de R$0,00).
        if _extracao_invalida(dados):
            return {
                "type": "text",
                "content": (
                    f"🗣️ _\"{transcricao}\"_\n\n"
                    "🤔 Não consegui identificar um gasto válido nesse áudio.\n\n"
                    "Tenta falar algo como:\n"
                    "🎤 _\"gastei 45 reais no iFood com cartão\"_\n"
                    "🎤 _\"uber 27 reais pix\"_\n\n"
                    "Ou se for outro comando, digita *ajuda* pra ver as opções."
                )
            }

        dados["transcricao"] = transcricao  # reusa a transcrição já feita
        gasto_id = await salvar_gasto(numero, dados, fonte="audio")
        ultimo_gasto[numero] = gasto_id
        await salvar_memoria(numero, ultimo_gasto_id=gasto_id)

        # Passa categoria pra checar limite por categoria também
        alerta = await verificar_limite_pos_gasto(numero, dados.get("categoria")) or ""

        card = CARD_AUDIO.format(
            transcricao=transcricao,
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
        print(f"❌ Erro ao processar áudio: {e}")
        return {"type": "text", "content": "😅 Não entendi o áudio. Tente falar mais claramente ou envie em texto.\nEx: _'gastei 45 reais no iFood com cartão'_"}
