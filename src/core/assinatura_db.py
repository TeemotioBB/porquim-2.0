# src/core/assinatura_db.py
# ─────────────────────────────────────────────────────────────────────────────
# Todas as funções de banco relacionadas a assinaturas e tokens de acesso.
# ─────────────────────────────────────────────────────────────────────────────

import secrets
import string
from datetime import datetime, timedelta, timezone
from src.core.database import get_pool


# ── Criação das tabelas ───────────────────────────────────────────────────────
# Chame esta função dentro do _create_tables() que já existe em database.py
# (basta adicionar uma chamada a create_assinatura_tables() no lifespan do app)

async def create_assinatura_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            -- Tabela de tokens gerados após pagamento aprovado
            CREATE TABLE IF NOT EXISTS tokens (
                token       VARCHAR(20) PRIMARY KEY,
                plano       VARCHAR(20) NOT NULL,          -- 'mensal' ou 'anual'
                valor_pago  DECIMAL(10,2) NOT NULL,
                payment_id  VARCHAR(100) UNIQUE NOT NULL,  -- ID do pagamento no MP
                usado       BOOLEAN DEFAULT FALSE,
                criado_em   TIMESTAMP DEFAULT NOW()
            );

            -- Tabela de assinaturas ativas (vinculadas a um número de WhatsApp)
            CREATE TABLE IF NOT EXISTS assinaturas (
                usuario         VARCHAR(50) PRIMARY KEY,  -- remoteJid do WhatsApp
                token           VARCHAR(20) NOT NULL REFERENCES tokens(token),
                plano           VARCHAR(20) NOT NULL,
                data_inicio     TIMESTAMP NOT NULL DEFAULT NOW(),
                data_expiracao  TIMESTAMP NOT NULL,
                status          VARCHAR(20) DEFAULT 'ativo',  -- 'ativo' | 'expirado'
                criado_em       TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_assinaturas_status ON assinaturas(status);
            CREATE INDEX IF NOT EXISTS idx_assinaturas_expiracao ON assinaturas(data_expiracao);
        """)
    print("✅ Tabelas de assinatura criadas!")


# ── Geração de token ──────────────────────────────────────────────────────────

def gerar_token() -> str:
    """
    Gera um token único no formato PORQUIM-XXXXXXXX
    Exemplo: PORQUIM-A3F9K2BV
    """
    chars = string.ascii_uppercase + string.digits
    codigo = ''.join(secrets.choice(chars) for _ in range(8))
    return f"PORQUIM-{codigo}"


# ── Salvar token após pagamento aprovado ──────────────────────────────────────

async def salvar_token(token: str, plano: str, valor_pago: float, payment_id: str):
    """
    Salva o token gerado após confirmação do pagamento.
    Chamado pelo webhook do Mercado Pago.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tokens (token, plano, valor_pago, payment_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (payment_id) DO NOTHING
        """, token, plano, valor_pago, payment_id)


# ── Ativar assinatura quando usuário manda o token no WhatsApp ────────────────

async def ativar_assinatura(usuario: str, token: str) -> dict:
    """
    Vincula um token ao número de WhatsApp do usuário.
    
    Retornos possíveis:
      {"ok": True, "plano": "anual", "expira": <datetime>}
      {"ok": False, "motivo": "token_invalido"}
      {"ok": False, "motivo": "token_ja_usado"}
      {"ok": False, "motivo": "ja_tem_assinatura_ativa"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:

        # 1. Verifica se o usuário já tem assinatura ativa
        assinatura = await conn.fetchrow(
            "SELECT * FROM assinaturas WHERE usuario = $1", usuario
        )
        if assinatura and assinatura["status"] == "ativo":
            if assinatura["data_expiracao"] > datetime.now(timezone.utc):
                return {"ok": False, "motivo": "ja_tem_assinatura_ativa"}

        # 2. Busca o token
        tok = await conn.fetchrow(
            "SELECT * FROM tokens WHERE token = $1", token.upper().strip()
        )
        if not tok:
            return {"ok": False, "motivo": "token_invalido"}

        if tok["usado"]:
            return {"ok": False, "motivo": "token_ja_usado"}

        # 3. Calcula expiração
        agora = datetime.now(timezone.utc)
        dias = 365 if tok["plano"] == "anual" else 30
        expira = agora + timedelta(days=dias)

        # 4. Marca token como usado e cria assinatura (transação)
        async with conn.transaction():
            await conn.execute(
                "UPDATE tokens SET usado = TRUE WHERE token = $1", tok["token"]
            )
            await conn.execute("""
                INSERT INTO assinaturas (usuario, token, plano, data_inicio, data_expiracao, status)
                VALUES ($1, $2, $3, $4, $5, 'ativo')
                ON CONFLICT (usuario) DO UPDATE SET
                    token = $2,
                    plano = $3,
                    data_inicio = $4,
                    data_expiracao = $5,
                    status = 'ativo'
            """, usuario, tok["token"], tok["plano"], agora, expira)

        return {"ok": True, "plano": tok["plano"], "expira": expira}


# ── Verificar se usuário tem acesso ──────────────────────────────────────────

async def verificar_acesso(usuario: str) -> dict:
    """
    Verifica se o usuário pode usar o bot.

    Retornos:
      {"tem_acesso": True, "plano": "anual", "dias_restantes": 200}
      {"tem_acesso": False, "motivo": "sem_assinatura"}
      {"tem_acesso": False, "motivo": "expirado", "plano": "mensal"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assinaturas WHERE usuario = $1", usuario
        )

        if not row:
            return {"tem_acesso": False, "motivo": "sem_assinatura"}

        agora = datetime.now(timezone.utc)
        expira = row["data_expiracao"]

        # Garante que expira tem timezone para comparação
        if expira.tzinfo is None:
            expira = expira.replace(tzinfo=timezone.utc)

        if expira < agora:
            # Marca como expirado no banco
            await conn.execute(
                "UPDATE assinaturas SET status = 'expirado' WHERE usuario = $1", usuario
            )
            return {"tem_acesso": False, "motivo": "expirado", "plano": row["plano"]}

        dias_restantes = (expira - agora).days
        return {
            "tem_acesso": True,
            "plano": row["plano"],
            "dias_restantes": dias_restantes
        }


# ── Buscar assinatura (para admin) ───────────────────────────────────────────

async def buscar_assinatura(usuario: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM assinaturas WHERE usuario = $1", usuario
        )
        return dict(row) if row else None
