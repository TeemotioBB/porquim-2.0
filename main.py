from src.core.config import settings
import httpx
import uvicorn
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, date as _date
from src.services.reminder_service import iniciar_background_lembretes
from src.services.recurring_service import iniciar_background_recorrentes
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
    # novas
    buscar_gastos_intervalo,
    buscar_entradas_intervalo,
    salvar_recorrente,
    listar_recorrentes,
    cancelar_recorrente,
    salvar_parcela,
    listar_parcelas,
    cancelar_parcela,
    salvar_limite_categoria,
    listar_limites_categoria,
    deletar_limite_categoria,
    total_gasto_categoria_mes,
)

# ── Sistema de Pagamento ──────────────────────────────────────────────────────
from src.core.assinatura_db import (
    create_assinatura_tables,
    salvar_token,
    ativar_assinatura,
    verificar_acesso,
    gerar_token,
    buscar_token_por_payment_id,
)

from src.handlers.text_handler import handle_text_message
from src.handlers.audio_handler import handle_audio_message
from src.handlers.image_handler import handle_image_message

# Variáveis do Mercado Pago (adicione no Railway em Variables)
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")
BOT_WHATSAPP_NUMBER = os.environ.get("BOT_WHATSAPP_NUMBER", "5511999999999")

# Números admin — acesso ilimitado sem assinatura (sem DDI 55, padrão do banco)
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
    await create_assinatura_tables()
    print("✅ Banco conectado e tabelas criadas!")
    app.state.processed_ids = set()
    iniciar_background_lembretes(app, _enviar_resposta)
    iniciar_background_recorrentes(app, _enviar_resposta)
    yield
    print("👋 Encerrando Johnny...")


app = FastAPI(title="Johnny 🐹", lifespan=lifespan)

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

class LimiteCategoriaBody(BaseModel):
    categoria: str
    valor: float

class RecorrenteBody(BaseModel):
    descricao: str
    valor: float
    categoria: str
    dia_mes: int
    forma_pagamento: str = "Desconhecido"

class ParcelaBody(BaseModel):
    descricao: str
    valor_total: float
    num_parcelas: int
    categoria: str
    forma_pagamento: str = "Cartão"


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
    return {"status": "Johnny 🐹 online!"}


@app.get("/api/verificar/{usuario}")
async def verificar_usuario(usuario: str):
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

    # Limites por categoria com progresso
    limites_cat = await listar_limites_categoria(usuario)
    limites_cat_progresso = []
    for l in limites_cat:
        gasto_cat = await total_gasto_categoria_mes(usuario, l["categoria"], ano, mes)
        pct = round(gasto_cat / l["valor"] * 100, 1) if l["valor"] > 0 else 0
        limites_cat_progresso.append({
            "categoria": l["categoria"],
            "limite": l["valor"],
            "gasto": round(gasto_cat, 2),
            "pct": pct,
            "emoji": emoji_para(l["categoria"]),
        })

    return {
        "total": round(total_gastos, 2),
        "total_entradas": round(total_entradas, 2),
        "saldo": round(saldo, 2),
        "num_gastos": len(gastos),
        "categorias": categorias,
        "limite": limite,
        "pct_limite": pct_limite,
        "limites_categoria": limites_cat_progresso,
    }


@app.get("/api/resumo-intervalo/{usuario}")
async def resumo_intervalo(usuario: str, data_inicio: str, data_fim: str):
    """Resumo por intervalo de datas (formato YYYY-MM-DD)."""
    try:
        di = _date.fromisoformat(data_inicio)
        df = _date.fromisoformat(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datas inválidas (use YYYY-MM-DD)")
    if di > df:
        raise HTTPException(status_code=400, detail="data_inicio maior que data_fim")

    gastos = await buscar_gastos_intervalo(usuario, di, df)
    entradas = await buscar_entradas_intervalo(usuario, di, df)

    total_gastos = sum(float(g["valor"]) for g in gastos)
    total_entradas = sum(float(e["valor"]) for e in entradas)

    cat_map: dict[str, float] = {}
    for g in gastos:
        cat_map[g["categoria"]] = cat_map.get(g["categoria"], 0.0) + float(g["valor"])
    categorias = sorted(
        [{"nome": c, "valor": round(v, 2), "emoji": emoji_para(c)} for c, v in cat_map.items()],
        key=lambda x: x["valor"], reverse=True
    )

    return {
        "total_gastos": round(total_gastos, 2),
        "total_entradas": round(total_entradas, 2),
        "saldo": round(total_entradas - total_gastos, 2),
        "num_gastos": len(gastos),
        "categorias": categorias,
        "gastos": [
            {
                "id": g["id"], "descricao": g["descricao"], "valor": float(g["valor"]),
                "categoria": g["categoria"], "forma_pagamento": g["forma_pagamento"],
                "data": g["data"].isoformat(), "fonte": g.get("fonte", "texto"),
                "emoji": emoji_para(g["categoria"]),
            } for g in gastos
        ],
        "entradas": [
            {
                "id": e["id"], "descricao": e["descricao"], "valor": float(e["valor"]),
                "categoria": e["categoria"], "data": e["data"].isoformat(),
                "fonte": e.get("fonte", "texto"), "emoji": emoji_entrada_para(e["categoria"]),
            } for e in entradas
        ],
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
                "parcela_id": g.get("parcela_id"),
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
async def definir_limite_route(usuario: str, body: LimiteBody):
    if body.valor <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido")
    await salvar_limite(usuario, body.valor)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# RECORRENTES — CRUD
# ════════════════════════════════════════════════════════════════════

@app.get("/api/recorrentes/{usuario}")
async def listar_recorrentes_route(usuario: str):
    recs = await listar_recorrentes(usuario, apenas_ativos=True)
    return {
        "recorrentes": [
            {
                "id": r["id"],
                "descricao": r["descricao"],
                "valor": float(r["valor"]),
                "categoria": r["categoria"],
                "forma_pagamento": r["forma_pagamento"],
                "dia_mes": r["dia_mes"],
                "ativo": r["ativo"],
                "ultimo_aviso": r["ultimo_aviso"].isoformat() if r["ultimo_aviso"] else None,
                "emoji": emoji_para(r["categoria"]),
            }
            for r in recs
        ]
    }


@app.post("/api/recorrentes/{usuario}")
async def criar_recorrente_route(usuario: str, body: RecorrenteBody):
    if body.valor <= 0 or body.dia_mes < 1 or body.dia_mes > 31:
        raise HTTPException(status_code=400, detail="Dados inválidos")
    rec_id = await salvar_recorrente(
        usuario=usuario,
        descricao=body.descricao,
        valor=body.valor,
        categoria=body.categoria,
        dia_mes=body.dia_mes,
        forma_pagamento=body.forma_pagamento,
    )
    return {"ok": True, "id": rec_id}


@app.delete("/api/recorrentes/{usuario}/{rec_id}")
async def cancelar_recorrente_route(usuario: str, rec_id: int):
    ok = await cancelar_recorrente(rec_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Recorrente não encontrado")
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# PARCELAS — CRUD
# ════════════════════════════════════════════════════════════════════

@app.get("/api/parcelas/{usuario}")
async def listar_parcelas_route(usuario: str):
    parcs = await listar_parcelas(usuario, apenas_ativas=False)
    return {
        "parcelas": [
            {
                "id": p["id"],
                "descricao": p["descricao"],
                "valor_total": float(p["valor_total"]),
                "valor_parcela": float(p["valor_parcela"]),
                "num_parcelas": p["num_parcelas"],
                "parcela_atual": p["parcela_atual"],
                "categoria": p["categoria"],
                "forma_pagamento": p["forma_pagamento"],
                "data_compra": p["data_compra"].isoformat(),
                "ativo": p["ativo"],
                "emoji": emoji_para(p["categoria"]),
            }
            for p in parcs
        ]
    }


@app.post("/api/parcelas/{usuario}")
async def criar_parcela_route(usuario: str, body: ParcelaBody):
    """
    Cria uma compra parcelada e lança a 1ª parcela imediatamente.
    """
    if body.valor_total <= 0 or body.num_parcelas < 2:
        raise HTTPException(status_code=400, detail="Dados inválidos")
    from src.services.recurring_service import criar_parcelado as _criar
    msg = await _criar(usuario, body.model_dump())
    return {"ok": True, "mensagem": msg}


@app.delete("/api/parcelas/{usuario}/{parcela_id}")
async def cancelar_parcela_route(usuario: str, parcela_id: int):
    ok = await cancelar_parcela(parcela_id, usuario)
    if not ok:
        raise HTTPException(status_code=404, detail="Parcela não encontrada")
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════
# LIMITES POR CATEGORIA — CRUD
# ════════════════════════════════════════════════════════════════════

@app.get("/api/limites-categoria/{usuario}")
async def listar_limites_cat_route(usuario: str):
    hoje = _date.today()
    limites = await listar_limites_categoria(usuario)
    out = []
    for l in limites:
        gasto = await total_gasto_categoria_mes(usuario, l["categoria"], hoje.year, hoje.month)
        pct = round(gasto / l["valor"] * 100, 1) if l["valor"] > 0 else 0
        out.append({
            "categoria": l["categoria"],
            "valor": l["valor"],
            "gasto": round(gasto, 2),
            "pct": pct,
            "emoji": emoji_para(l["categoria"]),
        })
    return {"limites": out}


@app.post("/api/limites-categoria/{usuario}")
async def salvar_limite_cat_route(usuario: str, body: LimiteCategoriaBody):
    if body.valor <= 0:
        raise HTTPException(status_code=400, detail="Valor inválido")
    await salvar_limite_categoria(usuario, body.categoria, body.valor)
    return {"ok": True}


@app.delete("/api/limites-categoria/{usuario}/{categoria}")
async def deletar_limite_cat_route(usuario: str, categoria: str):
    ok = await deletar_limite_categoria(usuario, categoria)
    if not ok:
        raise HTTPException(status_code=404, detail="Limite não encontrado")
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
# CRIAR PREFERÊNCIA — Mercado Pago
# ════════════════════════════════════════════════════════════════════

class PreferenciaBody(BaseModel):
    plano: str
    whatsapp: str
    email: str = ""

@app.post("/criar-preferencia")
async def criar_preferencia(body: PreferenciaBody):
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN não configurado")

    precos = {"mensal": 1.90, "anual": 67.00}
    plano = body.plano if body.plano in precos else "mensal"
    valor = precos[plano]

    numero_limpo = ''.join(filter(str.isdigit, body.whatsapp))
    if numero_limpo.startswith("55") and len(numero_limpo) > 11:
        numero_limpo = numero_limpo[2:]

    print(f"🔗 Criando preferência | plano={plano} | whatsapp={numero_limpo}")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                json={
                    "items": [{
                        "title": f"Johnny – Plano {plano.capitalize()}",
                        "quantity": 1,
                        "unit_price": valor,
                        "currency_id": "BRL"
                    }],
                    "external_reference": numero_limpo,
                    "metadata": {"plano": plano, "whatsapp": numero_limpo, "email": body.email},
                    "back_urls": {
                        "success": "https://wa.me/" + BOT_WHATSAPP_NUMBER + "?text=Oi%2C+acabei+de+pagar!",
                        "failure": "",
                        "pending": ""
                    },
                    "auto_return": "approved",
                    "statement_descriptor": "JOHNNY"
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
# WEBHOOK MERCADO PAGO
# ════════════════════════════════════════════════════════════════════

async def _enviar_email_ativacao(email: str, token: str, plano_label: str, link_acesso: str):
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@seudominio.com.br")

    if not RESEND_API_KEY:
        print(f"⚠️ RESEND_API_KEY não configurada — email não enviado para {email}")
        print(f"   Link de ativação: {link_acesso}")
        return

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#060809;font-family:'DM Sans',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#060809;min-height:100vh;">
    <tr>
      <td align="center" style="padding:40px 20px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;">
          <tr>
            <td style="padding-bottom:32px;">
              <span style="font-size:1.6rem;font-weight:900;color:#1DB954;letter-spacing:-0.03em;">Johnny</span>
              <span style="font-size:1.4rem;">🐹</span>
              <p style="margin:4px 0 0;font-size:0.8rem;color:rgba(240,245,241,0.4);">Seu assistente financeiro no WhatsApp</p>
            </td>
          </tr>
          <tr>
            <td style="background:linear-gradient(135deg,rgba(29,185,84,0.08),rgba(29,185,84,0.02));border:1px solid rgba(29,185,84,0.2);border-radius:20px;padding:32px 28px;">
              <div style="display:inline-block;background:rgba(29,185,84,0.15);border:1px solid rgba(29,185,84,0.3);border-radius:100px;padding:6px 14px;margin-bottom:20px;">
                <span style="font-size:0.72rem;font-weight:500;color:#1DB954;letter-spacing:0.06em;text-transform:uppercase;">✅ Pagamento confirmado</span>
              </div>
              <h1 style="margin:0 0 8px;font-size:1.6rem;font-weight:700;color:#f0f5f1;line-height:1.2;letter-spacing:-0.03em;">Bem-vindo ao Johnny!</h1>
              <p style="margin:0 0 24px;font-size:0.9rem;color:rgba(240,245,241,0.5);line-height:1.6;">
                Sua assinatura do plano <strong style="color:#1DB954;">{plano_label}</strong> está ativa.<br>
                Clique no botão abaixo para ativar seu acesso no WhatsApp:
              </p>
              <a href="{link_acesso}" style="display:block;background:#1DB954;color:#000;text-align:center;padding:16px 28px;border-radius:100px;text-decoration:none;font-weight:700;font-size:1rem;margin-bottom:28px;">📱 Ativar acesso no WhatsApp</a>

              <div style="background:rgba(0,0,0,0.2);border-radius:12px;padding:16px 18px;margin-bottom:24px;">
                <p style="margin:0 0 10px;font-size:0.78rem;font-weight:500;color:#f0f5f1;">O que você pode fazer com o Johnny:</p>
                <p style="margin:0 0 6px;font-size:0.75rem;color:rgba(240,245,241,0.6);">💸 Registrar gastos por texto, áudio ou foto</p>
                <p style="margin:0 0 6px;font-size:0.75rem;color:rgba(240,245,241,0.6);">💰 Registrar entradas de dinheiro</p>
                <p style="margin:0 0 6px;font-size:0.75rem;color:rgba(240,245,241,0.6);">📊 Ver resumo dos seus gastos a qualquer hora</p>
                <p style="margin:0 0 6px;font-size:0.75rem;color:rgba(240,245,241,0.6);">🎯 Definir limite mensal e receber alertas</p>
                <p style="margin:0;font-size:0.75rem;color:rgba(240,245,241,0.6);">🔔 Criar lembretes com linguagem natural</p>
              </div>

              <div style="height:1px;background:rgba(255,255,255,0.06);margin-bottom:24px;"></div>
              <p style="margin:0 0 10px;font-size:0.75rem;color:rgba(240,245,241,0.4);">Ou abra o WhatsApp e envie este código para o bot:</p>
              <div style="background:rgba(0,0,0,0.3);border:1px solid rgba(29,185,84,0.25);border-radius:12px;padding:14px 18px;text-align:center;">
                <span style="font-family:monospace;font-size:1.2rem;font-weight:700;color:#1DB954;letter-spacing:0.1em;">{token}</span>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding-top:24px;text-align:center;">
              <p style="margin:0;font-size:0.7rem;color:rgba(240,245,241,0.25);line-height:1.6;">
                Se você não realizou esta compra, ignore este email.<br>
                © 2026 Johnny · <a href="https://meujohnny.com.br" style="color:rgba(29,185,84,0.5);text-decoration:none;">meujohnny.com.br</a>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": f"Johnny <{EMAIL_FROM}>",
                    "to": [email],
                    "subject": f"✅ Seu acesso ao Johnny está pronto!",
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
    body_bytes = await request.body()

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
            return {"ok": False}

    data = json.loads(body_bytes)

    if data.get("type") != "payment":
        return {"ok": True}

    payment_id = str(data.get("data", {}).get("id", ""))
    if not payment_id:
        return {"ok": True}

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

    metadata = pagamento.get("metadata", {})
    if metadata.get("plano"):
        plano = metadata["plano"]
    elif valor >= 60:
        plano = "anual"
    else:
        plano = "mensal"

    external_ref = pagamento.get("external_reference", "")
    whatsapp_meta = metadata.get("whatsapp", "")
    telefone_payer = pagamento.get("payer", {}).get("phone", {}).get("number", "")
    email_meta = metadata.get("email", "")
    email_payer = email_meta or pagamento.get("payer", {}).get("email", "")

    telefone_raw = external_ref or whatsapp_meta or telefone_payer
    print(f"📱 WhatsApp identificado: '{telefone_raw}' "
          f"(external_ref='{external_ref}', meta='{whatsapp_meta}', payer='{telefone_payer}')")

    # ── BUG FIX 1: Idempotência — reutiliza token se webhook disparar múltiplas vezes ──
    token_existente = await buscar_token_por_payment_id(payment_id)
    if token_existente:
        token = token_existente
        print(f"♻️ Token já existia para payment_id={payment_id}: {token} (webhook duplicado ignorado)")
    else:
        token = gerar_token()
        await salvar_token(token=token, plano=plano, valor_pago=valor, payment_id=payment_id)

    link_acesso = f"https://wa.me/{BOT_WHATSAPP_NUMBER}?text={token}"
    plano_label = "Anual 🎉" if plano == "anual" else "Mensal"

    print(f"✅ Token gerado: {token} | Plano: {plano_label} | Link: {link_acesso}")

    if email_payer and "@" in email_payer and "testuser" not in email_payer.lower():
        await _enviar_email_ativacao(
            email=email_payer,
            token=token,
            plano_label=plano_label,
            link_acesso=link_acesso
        )

    if telefone_raw:
        numero_limpo = ''.join(filter(str.isdigit, telefone_raw))
        # Normaliza para sempre ter o DDI 55
        if numero_limpo.startswith("55") and len(numero_limpo) > 11:
            numero_sem55 = numero_limpo[2:]
        else:
            numero_sem55 = numero_limpo

        # Gera variações sem DDI (para tentar ativação)
        variacoes_numero = [numero_sem55]
        if len(numero_sem55) == 10:
            com9 = numero_sem55[:2] + "9" + numero_sem55[2:]
            variacoes_numero.append(com9)
        elif len(numero_sem55) == 11:
            sem9 = numero_sem55[:2] + numero_sem55[3:]
            variacoes_numero.append(sem9)

        resultado = None
        jid = None
        for num in variacoes_numero:
            # ── BUG FIX 2: Salva assinatura SEMPRE com DDI 55 ──
            # A Evolution entrega mensagens com 55XXXXXXXXXX, então precisamos
            # que a assinatura fique salva nesse formato para verificar_acesso funcionar.
            jid = f"55{num}@s.whatsapp.net"
            resultado = await ativar_assinatura(jid, token)
            if resultado["ok"]:
                print(f"✅ Ativação com número: 55{num}")
                break
            print(f"⚠️ Ativação falhou para 55{num}, tentando variação...")

        if resultado["ok"]:
            dias = resultado.get("expira") and (resultado["expira"] - datetime.now(timezone.utc)).days
            dias_extras = resultado.get("dias_extras", 0)
            extra_msg = f"\n⏭ _+{dias_extras} dias do ciclo anterior foram somados!_" if dias_extras > 0 else ""
            await _enviar_resposta(jid,
                f"✅ Pagamento confirmado! Bem-vindo ao Johnny 🐹\n\n"
                f"📋 Plano: {plano_label}\n"
                f"📅 Válido por {dias} dias{extra_msg}\n\n"
                f"👋 Oi! Eu sou o Johnny 🐹💚\n"
                f"Seu assistente financeiro aqui no WhatsApp.\n"
                f"Vou cuidar da sua grana com você!\n\n"
                f"Agora para começar digite *ajuda* e entenda todas as funcionalidades!"
            )
        else:
            await _enviar_resposta(jid,
                f"✅ Pagamento confirmado!\n\n"
                f"Seu token de acesso: *{token}*\n\n"
                f"Me envie o token acima para ativar seu acesso ao Johnny 🐹"
            )

    if not telefone_raw:
        print(f"⚠️ Sem WhatsApp identificado no pagamento. Token gerado mas não enviado.")
        print(f"   Token: {token} | Plano: {plano_label} | Valor: R$ {valor:.2f}")
        print(f"   E-mail do comprador: {email_payer or 'não informado'}")
        print(f"   ➡ Envie manualmente: {link_acesso}")

    return {"ok": True, "token": token}


@app.get("/testar-pagamento/{payment_id}")
async def testar_pagamento(payment_id: str, senha: str = ""):
    if not senha or senha != os.environ.get("ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="Acesso negado")
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


@app.get("/admin/assinantes")
async def admin_assinantes(senha: str = ""):
    if not senha or senha != os.environ.get("ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="Acesso negado")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                a.usuario,
                a.plano,
                a.data_inicio,
                a.data_expiracao,
                a.status,
                t.valor_pago,
                t.payment_id
            FROM assinaturas a
            JOIN tokens t ON a.token = t.token
            ORDER BY a.data_expiracao ASC
        """)
    agora = datetime.now(timezone.utc)
    resultado = []
    for r in rows:
        expira = r["data_expiracao"]
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)
        dias = (expira - agora).days
        num = r["usuario"].replace("@s.whatsapp.net", "")
        num_wa = num if num.startswith("55") else f"55{num}"
        resultado.append({
            "usuario": num,
            "whatsapp_link": f"https://wa.me/{num_wa}",
            "plano": r["plano"],
            "valor_pago": float(r["valor_pago"]),
            "data_inicio": r["data_inicio"].strftime("%d/%m/%Y"),
            "data_expiracao": expira.strftime("%d/%m/%Y"),
            "dias_restantes": dias,
            "status": "ativo" if dias > 0 else "expirado",
            "alerta": "vence_em_breve" if 0 < dias <= 7 else ("expirado" if dias <= 0 else "ok")
        })
    return resultado


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

    # ── Guard de acesso ──
    if text_body and text_body.strip().upper().startswith("JOHNNY-"):
        token_enviado = text_body.strip().upper()
        resultado = await ativar_assinatura(remote_jid, token_enviado)

        if resultado["ok"]:
            plano = resultado["plano"]
            expira = resultado["expira"]
            dias = (expira - datetime.now(timezone.utc)).days
            dias_extras = resultado.get("dias_extras", 0)
            plano_label = "Anual 🎉" if plano == "anual" else "Mensal"
            extra_msg = f"\n⏭ _+{dias_extras} dias do ciclo anterior foram somados!_" if dias_extras > 0 else ""
            await _enviar_resposta(remote_jid,
                f"✅ Acesso ativado com sucesso!\n\n"
                f"📋 Plano: {plano_label}\n"
                f"📅 Válido por {dias} dias{extra_msg}\n\n"
                f"Seja bem-vindo ao Johnny 🐹\n"
                f"Digite *ajuda* para começar!"
            )
        elif resultado["motivo"] == "token_invalido":
            await _enviar_resposta(remote_jid,
                "❌ Token inválido. Verifique se digitou corretamente.\n"
                "O token tem o formato *JOHNNY-XXXXXXXX*"
            )
        elif resultado["motivo"] == "token_ja_usado":
            await _enviar_resposta(remote_jid,
                "❌ Este token já foi utilizado por outro número.\n"
                "Se você acredita que houve um erro, entre em contato."
            )
        return {"status": "ok"}

    _jid_num = remote_jid.replace("@s.whatsapp.net", "")
    _jid_sem55 = _jid_num[2:] if _jid_num.startswith("55") and len(_jid_num) > 11 else _jid_num

    def _variantes(n: str) -> set:
        v = {n}
        if n.startswith("55"):
            v.add(n[2:])
        else:
            v.add("55" + n)
        extras = set()
        for x in v:
            base = x[2:] if x.startswith("55") else x
            if len(base) == 11 and base[2] == "9":
                extras.add(x[:len(x)-9] + base[3:] if x.startswith("55") else base[:2] + base[3:])
                sem9 = base[:2] + base[3:]
                extras.add(sem9)
                extras.add("55" + sem9)
            elif len(base) == 10:
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
                    "👋 Olá! O *Johnny* é um assistente financeiro via WhatsApp.\n\n"
                    "Para usar, você precisa de uma assinatura:\n"
                    "• 💰 Mensal: R$ 19,90\n"
                    "• 🎉 Anual: R$ 67,00 _(economize 72%!)_\n\n"
                    "Acesse nossa página para assinar:\n"
                    "👉 meujohnny.com.br\n\n"
                    "_Após o pagamento, você receberá um token para ativar seu acesso aqui._"
                )
            elif acesso["motivo"] == "expirado":
                await _enviar_resposta(remote_jid,
                    f"⚠️ Sua assinatura expirou.\n\n"
                    f"Renove agora para continuar usando o Johnny 🐹:\n"
                    f"• 💰 Mensal: R$ 19,90\n"
                    f"• 🎉 Anual: R$ 67,00\n\n"
                    f"👉 https://www.meujohnny.com.br/"
                )
            return {"status": "ok"}

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


@app.get("/testar-email")
async def testar_email(email: str, senha: str = ""):
    if not senha or senha != os.environ.get("ADMIN_SECRET", ""):
        raise HTTPException(status_code=403, detail="Acesso negado")
    await _enviar_email_ativacao(
        email=email,
        token="JOHNNY-TESTE123",
        plano_label="Mensal",
        link_acesso="https://wa.me/5531984686982?text=JOHNNY-TESTE123"
    )
    return {"ok": True, "email": email}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
