from src.core.config import settings
import httpx
import uvicorn
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from src.services.reminder_service import iniciar_background_lembretes
import hashlib
import hmac
import os

from src.core.database import (
    get_pool,
    buscar_gastos_mes,
    atualizar_gasto,
    deletar_gasto,
    salvar_limite,
    buscar_limite,
    buscar_entradas_mes,
    total_entrada_mes,
    deletar_entrada,
)

# ── Sistema de Pagamento ──────────────────────────────────────────────────────
from src.core.assinatura_db import (
    create_assinatura_tables,
    salvar_token,
    ativar_assinatura,
    verificar_acesso,
    gerar_token,
)

from src.handlers.text_handler import handle_text_message
from src.handlers.audio_handler import handle_audio_message
from src.handlers.image_handler import handle_image_message

# Variáveis do Mercado Pago (adicione no Railway em Variables)
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")
BOT_WHATSAPP_NUMBER = os.environ.get("BOT_WHATSAPP_NUMBER", "5511999999999")

# Números admin — acesso ilimitado sem assinatura (sem DDI 55, padrão do banco)
# Defina no Railway em Variables: ADMIN_NUMBERS=31999999999,11988887777
_admin_env = os.environ.get("ADMIN_NUMBERS", "")
ADMIN_NUMBERS: set = set(n.strip() for n in _admin_env.split(",") if n.strip())


# ── Emoji por categoria ──────────────────────────────────────────────────────
EMOJI_CAT = {
    "Alimentação": "🍔", "Transporte": "🚗", "Moradia": "🏠",
    "Saúde": "💊", "Lazer": "🎮", "Vestuário": "👕",
    "Educação": "📚", "Outros": "📦",
}

EMOJI_ENTRADA_CAT = {
    "Salário": "💼", "Freelance": "💻", "Investimento": "📈",
    "Presente": "🎁", "Reembolso": "🔄", "Outros": "📦",
}

def emoji_para(cat: str) -> str:
    return EMOJI_CAT.get(cat, "📦")

def emoji_entrada_para(cat: str) -> str:
    return EMOJI_ENTRADA_CAT.get(cat, "📦")


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🐘 Conectando ao PostgreSQL...")
    await get_pool()
    await create_assinatura_tables()  # ← NOVO: cria tabelas de assinatura
    print("✅ Banco conectado e tabelas criadas!")
    app.state.processed_ids = set()
    iniciar_background_lembretes(app, _enviar_resposta)
    yield
    print("👋 Encerrando Porquim...")


app = FastAPI(title="Porquim 2.0 🐷", lifespan=lifespan)

# Estado compartilhado: último gasto por usuário (para remover/editar após áudio/imagem)
_ultimo_gasto_global: dict[str, int] = {}

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Modelos ───────────────────────────────────────────────────────────────────
class EditarGastoBody(BaseModel):
    descricao: str
    valor: float
    categoria: str
    forma_pagamento: str

class LimiteBody(BaseModel):
    valor: float


# ── Helper Evolution ─────────────────────────────────────────────────────────
async def _enviar_resposta(remote_jid: str, texto: str):
    url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json={"number": remote_jid, "text": texto},
                headers={"apikey": settings.EVOLUTION_API_KEY}
            )
            print(f"📤 Evolution: {resp.status_code}")
            return resp.status_code in [200, 201]
    except Exception as e:
        print(f"❌ Erro ao enviar resposta: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
# ROTAS
# ════════════════════════════════════════════════════════════════════

@app.get("/")
async def health():
    return {"status": "Porquim 2.0 🐷 online!"}


@app.get("/api/verificar/{usuario}")
async def verificar_usuario(usuario: str):
    # Gera todas as variações do número (com/sem 55, com/sem 9) para busca no banco
    def _variantes_num(n: str) -> list:
        v = {n}
        sem55 = n[2:] if n.startswith("55") and len(n) > 11 else n
        com55 = n if n.startswith("55") else "55" + n
        v.add(sem55); v.add(com55)
        extras = set()
        for x in v:
            base = x[2:] if x.startswith("55") else x
            if len(base) == 11 and base[2] == "9":
                sem9 = base[:2] + base[3:]
                extras.add(sem9); extras.add("55" + sem9)
            elif len(base) == 10:
                com9 = base[:2] + "9" + base[2:]
                extras.add(com9); extras.add("55" + com9)
        v |= extras
        return list(v)

    variantes = _variantes_num(usuario)
    pool = await get_pool()
    async with pool.acquire() as conn:
        for v in variantes:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM gastos WHERE usuario = $1", v
            )
            if int(count) > 0:
                return {"existe": True, "usuario": v}
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM entradas WHERE usuario = $1", v
            )
            if int(count) > 0:
                return {"existe": True, "usuario": v}
    return {"existe": False}


@app.get("/api/resumo/{usuario}")
async def resumo(usuario: str, ano: int, mes: int):
    gastos = await buscar_gastos_mes(usuario, ano, mes)
    total_gastos = sum(float(g["valor"]) for g in gastos)
    total_entradas = await total_entrada_mes(usuario, ano, mes)
    saldo = total_entradas - total_gastos

    cat_map: dict[str, float] = {}
    for g in gastos:
        cat_map[g["categoria"]] = cat_map.get(g["categoria"], 0.0) + float(g["valor"])

    categorias = sorted(
        [
            {
                "nome": cat,
                "valor": round(val, 2),
                "pct": round(val / total_gastos * 100, 1) if total_gastos > 0 else 0,
                "emoji": emoji_para(cat),
            }
            for cat, val in cat_map.items()
        ],
        key=lambda x: x["valor"],
        reverse=True,
    )

    limite = await buscar_limite(usuario)
    pct_limite = round(total_gastos / limite * 100, 1) if limite else 0

    return {
        "total": round(total_gastos, 2),
        "total_entradas": round(total_entradas, 2),
        "saldo": round(saldo, 2),
        "num_gastos": len(gastos),
        "categorias": categorias,
        "limite": limite,
        "pct_limite": pct_limite,
    }


@app.get("/api/gastos/{usuario}")
async def listar_gastos(usuario: str, ano: int, mes: int, categoria: Optional[str] = None):
    gastos = await buscar_gastos_mes(usuario, ano, mes)

    if categoria:
        gastos = [g for g in gastos if g["categoria"] == categoria]

    return {
        "gastos": [
            {
                "id": g["id"],
                "descricao": g["descricao"],
                "valor": float(g["valor"]),
                "categoria": g["categoria"],
                "forma_pagamento": g["forma_pagamento"],
                "data": g["data"].isoformat(),
                "fonte": g.get("fonte", "texto"),
                "emoji": emoji_para(g["categoria"]),
            }
            for g in gastos
        ]
    }


@app.get("/api/entradas/{usuario}")
async def listar_entradas(usuario: str, ano: int, mes: int, categoria: Optional[str] = None):
    entradas = await buscar_entradas_mes(usuario, ano, mes)

    if categoria:
        entradas = [e for e in entradas if e["categoria"] == categoria]

    return {
        "entradas": [
            {
                "id": e["id"],
                "descricao": e["descricao"],
                "valor": float(e["valor"]),
                "categoria": e["categoria"],
                "data": e["data"].isoformat(),
                "fonte": e.get("fonte", "texto"),
                "emoji": emoji_entrada_para(e["categoria"]),
            }
            for e in entradas
        ]
    }


@app.delete("/api/entradas/{usuario}/{entrada_id}")
async def excluir_entrada(usuario: str, entrada_id: int):
    ok = await deletar_entrada(entrada_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Entrada não encontrada")
    return {"ok": True}


@app.put("/api/gastos/{usuario}/{gasto_id}")
async def editar_gasto(usuario: str, gasto_id: int, body: EditarGastoBody):
    ok = await atualizar_gasto(gasto_id, usuario, body.model_dump())
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"ok": True}


@app.delete("/api/gastos/{usuario}/{gasto_id}")
async def excluir_gasto(usuario: str, gasto_id: int):
    ok = await deletar_gasto(gasto_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Gasto não encontrado")
    return {"ok": True}


@app.get("/api/evolucao/{usuario}")
async def evolucao(usuario: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows_gastos = await conn.fetch(
            """
            SELECT
                EXTRACT(YEAR  FROM data)::int AS ano,
                EXTRACT(MONTH FROM data)::int AS mes,
                COALESCE(SUM(valor), 0)::float AS total
            FROM gastos
            WHERE usuario = $1
              AND data >= (CURRENT_DATE - INTERVAL '5 months')::date
            GROUP BY ano, mes
            ORDER BY ano, mes
            """,
            usuario,
        )
        rows_entradas = await conn.fetch(
            """
            SELECT
                EXTRACT(YEAR  FROM data)::int AS ano,
                EXTRACT(MONTH FROM data)::int AS mes,
                COALESCE(SUM(valor), 0)::float AS total
            FROM entradas
            WHERE usuario = $1
              AND data >= (CURRENT_DATE - INTERVAL '5 months')::date
            GROUP BY ano, mes
            ORDER BY ano, mes
            """,
            usuario,
        )

    MESES_SHORT = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
                   "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

    gastos_map = {(r["ano"], r["mes"]): round(r["total"], 2) for r in rows_gastos}
    entradas_map = {(r["ano"], r["mes"]): round(r["total"], 2) for r in rows_entradas}
    all_keys = sorted(set(list(gastos_map.keys()) + list(entradas_map.keys())))

    return {
        "meses": [
            {
                "nome": MESES_SHORT[mes],
                "ano": ano,
                "mes": mes,
                "total": gastos_map.get((ano, mes), 0.0),
                "entradas": entradas_map.get((ano, mes), 0.0),
            }
            for ano, mes in all_keys
        ]
    }


@app.post("/api/limite/{usuario}")
async def definir_limite(usuario: str, body: LimiteBody):
    if body.valor <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido")
    await salvar_limite(usuario, body.valor)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# RESET
# ════════════════════════════════════════════════════════════════════

@app.get("/api/usuarios")
async def listar_usuarios(senha: str = ""):
    if senha != settings.RESET_SECRET:
        raise HTTPException(status_code=403, detail="Senha incorreta.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        usuarios = await conn.fetch("SELECT DISTINCT usuario FROM gastos UNION SELECT DISTINCT usuario FROM entradas")
    return {"usuarios": [r["usuario"] for r in usuarios]}


@app.get("/api/reset/{usuario}")
async def reset_usuario(usuario: str, senha: str = ""):
    if senha != settings.RESET_SECRET:
        raise HTTPException(status_code=403, detail="Senha incorreta.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        gastos_del = await conn.execute("DELETE FROM gastos WHERE usuario = $1", usuario)
        entradas_del = await conn.execute("DELETE FROM entradas WHERE usuario = $1", usuario)
    n_gastos = int(gastos_del.split()[-1])
    n_entradas = int(entradas_del.split()[-1])
    return {
        "ok": True,
        "usuario": usuario,
        "gastos_removidos": n_gastos,
        "entradas_removidas": n_entradas,
        "mensagem": f"✅ Zerado! {n_gastos} gasto(s) e {n_entradas} entrada(s) removidos."
    }


# ════════════════════════════════════════════════════════════════════
# CRIAR PREFERÊNCIA — gera link de pagamento personalizado com o
# WhatsApp do comprador embutido no external_reference
# ════════════════════════════════════════════════════════════════════

class PreferenciaBody(BaseModel):
    plano: str          # "mensal" ou "anual"
    whatsapp: str       # ex: "5531999999999" ou "31999999999"
    email: str = ""     # email do comprador para fallback

@app.post("/criar-preferencia")
async def criar_preferencia(body: PreferenciaBody):
    """
    Chamado pela landing page antes de redirecionar ao Mercado Pago.
    Cria uma preferência de pagamento com o número de WhatsApp do
    comprador no campo external_reference — assim o webhook consegue
    identificar para quem ativar a assinatura.
    """
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN não configurado")

    precos = {"mensal": 19.90, "anual": 67.00}
    plano = body.plano if body.plano in precos else "mensal"
    valor = precos[plano]

    # Normaliza o número: apenas dígitos, sem DDI 55
    numero_limpo = ''.join(filter(str.isdigit, body.whatsapp))
    if numero_limpo.startswith("55") and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]  # remove o 55 se vier com DDI

    print(f"🔗 Criando preferência | plano={plano} | whatsapp={numero_limpo}")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                json={
                    "items": [{
                        "title": f"Porquim – Plano {plano.capitalize()}",
                        "quantity": 1,
                        "unit_price": valor,
                        "currency_id": "BRL"
                    }],
                    "external_reference": numero_limpo,   # ← número WA do comprador (sem DDI 55)
                    "metadata": {"plano": plano, "whatsapp": numero_limpo, "email": body.email},
                    "back_urls": {
                        "success": "https://wa.me/" + BOT_WHATSAPP_NUMBER + "?text=Oi%2C+acabei+de+pagar!",
                        "failure": "",
                        "pending": ""
                    },
                    "auto_return": "approved",
                    "statement_descriptor": "PORQUIM"
                }
            )
            dados = resp.json()
    except Exception as e:
        print(f"❌ Erro ao criar preferência no MP: {e}")
        raise HTTPException(status_code=500, detail="Erro ao gerar link de pagamento")

    init_point = dados.get("init_point")
    if not init_point:
        print(f"❌ Resposta inesperada do MP: {dados}")
        raise HTTPException(status_code=500, detail="Link de pagamento não retornado pelo MP")

    print(f"✅ Preferência criada | link={init_point}")
    return {"link": init_point, "plano": plano, "whatsapp": numero_limpo, "email": body.email}


# ════════════════════════════════════════════════════════════════════
# WEBHOOK MERCADO PAGO — geração de token após pagamento aprovado
# ════════════════════════════════════════════════════════════════════

async def _enviar_email_ativacao(email: str, token: str, plano_label: str, link_acesso: str):
    """
    Envia email com o link de ativação do WhatsApp.
    Usa a API do Resend (resend.com) — grátis até 3000 emails/mês.
    Configure RESEND_API_KEY no Railway.
    """
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@seudominio.com.br")

    if not RESEND_API_KEY:
        print(f"⚠️ RESEND_API_KEY não configurada — email não enviado para {email}")
        print(f"   Link de ativação: {link_acesso}")
        return

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0f1a10;color:#e2ebe4;border-radius:16px">
      <h1 style="color:#00e676;font-size:1.8rem;margin-bottom:4px">Porquim 🐹</h1>
      <p style="color:#aaa;font-size:0.85rem;margin-bottom:28px">Seu assistente financeiro no WhatsApp</p>

      <h2 style="font-size:1.1rem;margin-bottom:12px">✅ Pagamento confirmado!</h2>
      <p style="color:#bbb;line-height:1.6;margin-bottom:24px">
        Obrigado pela sua assinatura do plano <strong style="color:#00e676">{plano_label}</strong>.
        Clique no botão abaixo para ativar seu acesso no WhatsApp:
      </p>

      <a href="{link_acesso}" style="display:inline-block;background:#00e676;color:#000;padding:14px 28px;border-radius:100px;text-decoration:none;font-weight:700;font-size:1rem;margin-bottom:24px">
        📱 Ativar acesso no WhatsApp
      </a>

      <p style="color:#888;font-size:0.8rem;line-height:1.6;margin-bottom:8px">
        Ou abra o WhatsApp e envie esta mensagem para o bot:
      </p>
      <div style="background:#1a2e1c;border:1px solid rgba(0,230,118,0.2);border-radius:10px;padding:12px 16px;font-family:monospace;font-size:1rem;color:#00e676;letter-spacing:0.05em;margin-bottom:24px">
        {token}
      </div>

      <p style="color:#666;font-size:0.72rem">
        Se você não realizou esta compra, ignore este email.
      </p>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": f"Porquim <{EMAIL_FROM}>",
                    "to": [email],
                    "subject": f"✅ Seu acesso ao Porquim está pronto!",
                    "html": html_body
                }
            )
            if resp.status_code in [200, 201]:
                print(f"📧 Email enviado para {email}")
            else:
                print(f"⚠️ Erro ao enviar email: {resp.status_code} — {resp.text}")
    except Exception as e:
        print(f"❌ Erro ao enviar email: {e}")


@app.post("/webhook/pagamento")
async def webhook_pagamento(request: Request):
    """
    Recebe notificações do Mercado Pago quando um pagamento é aprovado.
    Configure em: https://www.mercadopago.com.br/developers/panel/webhooks
    URL: https://SEU_APP.railway.app/webhook/pagamento
    Evento: payment
    """
    body_bytes = await request.body()

    # Verificação de assinatura (segurança)
    if MP_WEBHOOK_SECRET:
        signature = request.headers.get("x-signature", "")
        request_id = request.headers.get("x-request-id", "")
        try:
            data_tmp = json.loads(body_bytes)
            parts = dict(p.split("=", 1) for p in signature.split(","))
            ts = parts.get("ts", "")
            v1 = parts.get("v1", "")
            data_id = data_tmp.get("data", {}).get("id", "")
            manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
            expected = hmac.new(
                MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, v1):
                print("⚠️ Webhook MP: assinatura inválida")
                return {"ok": False}
        except Exception as e:
            print(f"⚠️ Erro na verificação MP: {e}")

    data = json.loads(body_bytes)

    if data.get("type") != "payment":
        return {"ok": True}

    payment_id = str(data.get("data", {}).get("id", ""))
    if not payment_id:
        return {"ok": True}

    # Busca detalhes do pagamento
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
            )
            pagamento = resp.json()
    except Exception as e:
        print(f"❌ Erro ao buscar pagamento no MP: {e}")
        return {"ok": False}

    print(f"💳 Pagamento {payment_id}: status={pagamento.get('status')}")

    if pagamento.get("status") != "approved":
        return {"ok": True}

    valor = float(pagamento.get("transaction_amount", 0))

    # Identifica o plano: 1º pelo metadata (mais confiável), depois pelo valor
    metadata = pagamento.get("metadata", {})
    if metadata.get("plano"):
        plano = metadata["plano"]
    elif valor >= 60:
        plano = "anual"
    else:
        plano = "mensal"

    # ─── NOVO: pega o WhatsApp do external_reference (definido pela landing page) ───
    # Fallback 1: metadata.whatsapp
    # Fallback 2: campo phone do payer (pouco confiável)
    external_ref = pagamento.get("external_reference", "")
    whatsapp_meta = metadata.get("whatsapp", "")
    telefone_payer = pagamento.get("payer", {}).get("phone", {}).get("number", "")
    # Prioridade de email: metadata (digitado pelo usuário) > payer do MP
    email_meta = metadata.get("email", "")
    email_payer = email_meta or pagamento.get("payer", {}).get("email", "")

    telefone_raw = external_ref or whatsapp_meta or telefone_payer
    print(f"📱 WhatsApp identificado: '{telefone_raw}' "
          f"(external_ref='{external_ref}', meta='{whatsapp_meta}', payer='{telefone_payer}')")

    # Gera token e salva
    token = gerar_token()
    await salvar_token(token=token, plano=plano, valor_pago=valor, payment_id=payment_id)

    link_acesso = f"https://wa.me/{BOT_WHATSAPP_NUMBER}?text={token}"
    plano_label = "Anual 🎉" if plano == "anual" else "Mensal"

    print(f"✅ Token gerado: {token} | Plano: {plano_label} | Link: {link_acesso}")

    # Envia email com link de ativação (funciona mesmo se o cliente nunca interagiu com o bot)
    if email_payer and "@" in email_payer and "testuser" not in email_payer.lower():
        await _enviar_email_ativacao(
            email=email_payer,
            token=token,
            plano_label=plano_label,
            link_acesso=link_acesso
        )

    # Se encontrou o WhatsApp, ativa automaticamente e manda mensagem
    if telefone_raw:
        # Normaliza: apenas dígitos, sem DDI 55 (padrão de armazenamento do bot)
        numero_limpo = ''.join(filter(str.isdigit, telefone_raw))
        if numero_limpo.startswith("55") and len(numero_limpo) > 11:
            numero_limpo = numero_limpo[2:]  # remove 55 se vier com DDI

        jid = f"{numero_limpo}@s.whatsapp.net"

        resultado = await ativar_assinatura(jid, token)
        if resultado["ok"]:
            dias = resultado.get("expira") and (resultado["expira"] - datetime.now(timezone.utc)).days
            await _enviar_resposta(jid,
                f"✅ Pagamento confirmado! Bem-vindo ao Porquim 🐷\n\n"
                f"📋 Plano: {plano_label}\n"
                f"📅 Válido por {dias} dias\n\n"
                f"Pode começar agora! Me manda um gasto:\n"
                f"Ex: _gastei 25 reais no ifood_"
            )
        else:
            # Token gerado mas ativação automática falhou — manda o token para ativar manualmente
            await _enviar_resposta(jid,
                f"✅ Pagamento confirmado!\n\n"
                f"Seu token de acesso: *{token}*\n\n"
                f"Me envie o token acima para ativar seu acesso ao Porquim 🐷"
            )

    # Se não encontrou WhatsApp de jeito nenhum, loga para o admin
    if not telefone_raw:
        print(f"⚠️ Sem WhatsApp identificado no pagamento. Token gerado mas não enviado.")
        print(f"   Token: {token} | Plano: {plano_label} | Valor: R$ {valor:.2f}")
        print(f"   E-mail do comprador: {email_payer or 'não informado'}")
        print(f"   ➡ Envie manualmente: {link_acesso}")

    return {"ok": True, "token": token}


# ════════════════════════════════════════════════════════════════════
# ENDPOINT DE TESTE — força processar um payment_id real
# Remova em produção ou proteja com senha
# ════════════════════════════════════════════════════════════════════

@app.get("/testar-pagamento/{payment_id}")
async def testar_pagamento(payment_id: str):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
            )
            pagamento = resp.json()
    except Exception as e:
        return {"erro": str(e)}
    print(f"🧪 [TESTE] Pagamento {payment_id}: status={pagamento.get('status')} | external_reference={pagamento.get('external_reference')}")
    return {
        "status": pagamento.get("status"),
        "external_reference": pagamento.get("external_reference"),
        "valor": pagamento.get("transaction_amount"),
        "email": pagamento.get("payer", {}).get("email"),
        "metadata": pagamento.get("metadata"),
    }


# ════════════════════════════════════════════════════════════════════
# WEBHOOK WHATSAPP
# ════════════════════════════════════════════════════════════════════

@app.post("/webhook")
@app.post("/webhook/{any:path}")
async def evolution_webhook(request: Request, any: str = None):
    data = await request.json()
    event = any or "webhook"
    print(f"\n📥 [WEBHOOK] Evento: {event}")

    if not isinstance(data, dict) or "data" not in data:
        return {"status": "ok"}

    msg_data = data["data"]

    if isinstance(msg_data, list):
        return {"status": "ok"}

    if msg_data.get("key", {}).get("fromMe", False):
        return {"status": "ok"}

    msg_id = msg_data.get("key", {}).get("id", "")
    if msg_id and msg_id in app.state.processed_ids:
        print(f"⚠️ Duplicata ignorada: {msg_id}")
        return {"status": "ok"}
    if msg_id:
        app.state.processed_ids.add(msg_id)
        if len(app.state.processed_ids) > 200:
            app.state.processed_ids.pop()

    remote_jid = msg_data.get("key", {}).get("remoteJid")
    msg = msg_data.get("message", {})

    if not remote_jid or not msg:
        return {"status": "ok"}

    text_body = msg.get("conversation") or msg.get("extendedTextMessage", {}).get("text")

    # ── Guard de acesso ──────────────────────────────────────────────────────
    # Se o usuário mandou um token de ativação
    if text_body and text_body.strip().upper().startswith("PORQUIM-"):
        token_enviado = text_body.strip().upper()
        resultado = await ativar_assinatura(remote_jid, token_enviado)

        if resultado["ok"]:
            plano = resultado["plano"]
            expira = resultado["expira"]
            dias = (expira - datetime.now(timezone.utc)).days
            plano_label = "Anual 🎉" if plano == "anual" else "Mensal"
            await _enviar_resposta(remote_jid,
                f"✅ Acesso ativado com sucesso!\n\n"
                f"📋 Plano: {plano_label}\n"
                f"📅 Válido por {dias} dias\n\n"
                f"Seja bem-vindo ao Porquim 🐷\n"
                f"Me manda um gasto pra começar! Ex: _gastei 15 reais no mercado_"
            )
        elif resultado["motivo"] == "token_invalido":
            await _enviar_resposta(remote_jid,
                "❌ Token inválido. Verifique se digitou corretamente.\n"
                "O token tem o formato *PORQUIM-XXXXXXXX*"
            )
        elif resultado["motivo"] == "token_ja_usado":
            await _enviar_resposta(remote_jid,
                "❌ Este token já foi utilizado por outro número.\n"
                "Se você acredita que houve um erro, entre em contato."
            )
        elif resultado["motivo"] == "ja_tem_assinatura_ativa":
            await _enviar_resposta(remote_jid,
                "✅ Você já tem uma assinatura ativa! Pode usar o bot normalmente 🐷"
            )
        return {"status": "ok"}

    # Verifica acesso antes de processar qualquer mensagem
    # Números admin pulam a verificação de assinatura
    # Gera todas as variações possíveis do número para comparar com ADMIN_NUMBERS:
    # com/sem DDI 55, com/sem o nono dígito (MG e outros estados)
    _jid_num = remote_jid.replace("@s.whatsapp.net", "")
    _jid_sem55 = _jid_num[2:] if _jid_num.startswith("55") and len(_jid_num) > 11 else _jid_num

    def _variantes(n: str) -> set:
        v = {n}
        # adiciona/remove 55
        if n.startswith("55"):
            v.add(n[2:])
        else:
            v.add("55" + n)
        # para cada variante, adiciona/remove o 9 após o DDD (2 dígitos)
        extras = set()
        for x in v:
            digits = x.lstrip("55") if x.startswith("55") else x
            # remove 55 prefix para trabalhar com DDD+número
            base = x[2:] if x.startswith("55") else x
            if len(base) == 11 and base[2] == "9":      # tem o 9 → remove
                extras.add(x[:len(x)-9] + base[3:] if x.startswith("55") else base[:2] + base[3:])
                sem9 = base[:2] + base[3:]
                extras.add(sem9)
                extras.add("55" + sem9)
            elif len(base) == 10:                         # sem o 9 → adiciona
                com9 = base[:2] + "9" + base[2:]
                extras.add(com9)
                extras.add("55" + com9)
        v |= extras
        return v

    _variacoes = _variantes(_jid_num)
    _is_admin = bool(_variacoes & ADMIN_NUMBERS)
    print(f"🔍 [ADMIN] jid={_jid_num} | variações={_variacoes} | is_admin={_is_admin}")
    if not _is_admin:
        acesso = await verificar_acesso(remote_jid)
        if not acesso["tem_acesso"]:
            if acesso["motivo"] == "sem_assinatura":
                await _enviar_resposta(remote_jid,
                    "👋 Olá! O *Porquim* é um assistente financeiro via WhatsApp.\n\n"
                    "Para usar, você precisa de uma assinatura:\n"
                    "• 💰 Mensal: R$ 19,90\n"
                    "• 🎉 Anual: R$ 67,00 _(economize 72%!)_\n\n"
                    "Acesse nossa página para assinar:\n"
                    "👉 https://SUA_LANDING_PAGE.com\n\n"
                    "_Após o pagamento, você receberá um token para ativar seu acesso aqui._"
                )
            elif acesso["motivo"] == "expirado":
                await _enviar_resposta(remote_jid,
                    f"⚠️ Sua assinatura expirou.\n\n"
                    f"Renove agora para continuar usando o Porquim 🐷:\n"
                    f"• 💰 Mensal: R$ 19,90\n"
                    f"• 🎉 Anual: R$ 67,00\n\n"
                    f"👉 https://maycon.app"
                )
            return {"status": "ok"}
    # ── Fim do guard ──────────────────────────────────────────────────────────

    response = None

    if text_body:
        print(f"✅ Texto: '{text_body}'")
        response = await handle_text_message({
            "text": {"body": text_body},
            "key": {"remoteJid": remote_jid}
        })

    elif "audioMessage" in msg:
        print("🎤 Áudio recebido")
        response = await handle_audio_message(
            msg_data=msg_data,
            remote_jid=remote_jid,
            ultimo_gasto=_ultimo_gasto_global
        )

    elif "imageMessage" in msg:
        caption = msg["imageMessage"].get("caption", "").strip()
        print(f"📷 Imagem recebida (caption: '{caption}')")
        response = await handle_image_message(
            msg_data=msg_data,
            remote_jid=remote_jid,
            ultimo_gasto=_ultimo_gasto_global
        )

    else:
        print(f"⚠️ Tipo não suportado: {list(msg.keys())}")
        return {"status": "ok"}

    if response:
        await _enviar_resposta(remote_jid, response["content"])

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
