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
                token           VARCHAR(20) REFERENCES tokens(token),
                plano           VARCHAR(20) NOT NULL,
                data_inicio     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                data_expiracao  TIMESTAMPTZ NOT NULL,
                status          VARCHAR(20) DEFAULT 'ativo',  -- 'ativo' | 'expirado'
                criado_em       TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_assinaturas_status ON assinaturas(status);
            CREATE INDEX IF NOT EXISTS idx_assinaturas_expiracao ON assinaturas(data_expiracao);

            -- Tabela de quem já consumiu o teste gratuito (3 dias)
            -- Uma linha por número (qualquer variante de JID resolve antes
            -- de inserir, pra impedir reuso com/sem 9 dígito etc).
            CREATE TABLE IF NOT EXISTS testes_gratis (
                usuario     VARCHAR(50) PRIMARY KEY,
                criado_em   TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # IMPORTANTE: bancos antigos podem ter o NOT NULL em assinaturas.token,
        # mas agora teste cria assinatura sem token. Garantimos que é nullable.
        try:
            await conn.execute(
                "ALTER TABLE assinaturas ALTER COLUMN token DROP NOT NULL"
            )
        except Exception:
            pass  # se já estiver nullable, segue normal
    print("✅ Tabelas de assinatura criadas!")


# ── Geração de token ──────────────────────────────────────────────────────────

def gerar_token() -> str:
    """
    Gera um token único no formato JOHNNY-XXXXXXXX
    Exemplo: JOHNNY-A3F9K2BV
    """
    chars = string.ascii_uppercase + string.digits
    codigo = ''.join(secrets.choice(chars) for _ in range(8))
    return f"JOHNNY-{codigo}"


# ── Salvar token após pagamento aprovado ──────────────────────────────────────

async def buscar_token_por_payment_id(payment_id: str) -> str | None:
    """
    Retorna o token já salvo para um payment_id, ou None se não existir.
    Usado para garantir idempotência no webhook do Mercado Pago:
    se o webhook disparar múltiplas vezes, reutilizamos o token original.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token FROM tokens WHERE payment_id = $1", payment_id
        )
        return row["token"] if row else None


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
      {"ok": True, "plano": "anual", "expira": <datetime>, "dias_extras": 5}
      {"ok": False, "motivo": "token_invalido"}
      {"ok": False, "motivo": "token_ja_usado"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:

        # 1. Busca assinatura existente (ativa ou expirada)
        assinatura = await conn.fetchrow(
            "SELECT * FROM assinaturas WHERE usuario = $1", usuario
        )

        # 2. Busca o token
        tok = await conn.fetchrow(
            "SELECT * FROM tokens WHERE token = $1", token.upper().strip()
        )
        if not tok:
            return {"ok": False, "motivo": "token_invalido"}

        if tok["usado"]:
            return {"ok": False, "motivo": "token_ja_usado"}

        # 3. Calcula expiração — soma dias restantes APENAS se já tiver
        #    assinatura PAGA ativa (renovação). Não soma dias do teste grátis,
        #    senão quem fez teste pagaria menos do que quem nunca testou.
        agora = datetime.now(timezone.utc)
        dias_novos = 365 if tok["plano"] == "anual" else 30

        dias_restantes = 0
        if assinatura and assinatura["plano"] in ("mensal", "anual"):
            expira_atual = assinatura["data_expiracao"]
            if expira_atual.tzinfo is None:
                expira_atual = expira_atual.replace(tzinfo=timezone.utc)
            if expira_atual > agora:
                dias_restantes = (expira_atual - agora).days

        expira = agora + timedelta(days=dias_novos + dias_restantes)

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

        return {"ok": True, "plano": tok["plano"], "expira": expira, "dias_extras": dias_restantes}


# ── Verificar se usuário tem acesso ──────────────────────────────────────────

def _variantes_jid(usuario: str) -> list:
    """
    Gera todas as variações possíveis de um JID de WhatsApp.
    Resolve o problema do 9 dígito brasileiro e do DDI 55:
      553198739574  <->  5531998739574  <->  3198739574  <->  31998739574
    """
    sufixo = "@s.whatsapp.net"
    num = usuario.replace(sufixo, "")
    variantes = set()

    # Extrai número sem DDI 55
    if num.startswith("55") and len(num) > 11:
        sem55 = num[2:]
    else:
        sem55 = num

    # Gera com e sem o 9 dígito
    nums_locais = {sem55}
    if len(sem55) == 11 and sem55[2] == "9":    # tem o 9 -> adiciona sem
        nums_locais.add(sem55[:2] + sem55[3:])
    elif len(sem55) == 10:                       # sem o 9 -> adiciona com
        nums_locais.add(sem55[:2] + "9" + sem55[2:])

    # Gera com e sem DDI 55
    for n in nums_locais:
        variantes.add(n + sufixo)
        variantes.add("55" + n + sufixo)

    variantes.add(usuario)  # garante que o original sempre está
    return list(variantes)


async def verificar_acesso(usuario: str) -> dict:
    """
    Verifica se o usuário pode usar o bot.
    Tenta todas as variações do JID (com/sem DDI 55, com/sem 9 dígito)
    para não bloquear usuário que pagou por diferença de formato de número.

    Retornos:
      {"tem_acesso": True, "plano": "anual", "dias_restantes": 200}
      {"tem_acesso": False, "motivo": "sem_assinatura"}
      {"tem_acesso": False, "motivo": "expirado", "plano": "mensal"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        variantes = _variantes_jid(usuario)
        row = await conn.fetchrow(
            "SELECT * FROM assinaturas WHERE usuario = ANY($1::text[])", variantes
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
                "UPDATE assinaturas SET status = 'expirado' WHERE usuario = $1", row["usuario"]
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


# ── Teste grátis (3 dias) ────────────────────────────────────────────────────

async def ativar_teste_gratis(usuario: str, dias: int = 3) -> dict:
    """
    Ativa um período de teste de N dias para o usuário.

    Regras:
      - Cada número (considerando todas as variantes de JID com/sem 55, com/sem 9)
        só pode usar o teste UMA VEZ na vida — a tabela testes_gratis serve
        de registro permanente.
      - Se o usuário já tem assinatura paga ATIVA: não cria teste, retorna
        "ja_assinante" (não faz sentido, já tem acesso).
      - Se o usuário JÁ FOI cliente pago algum dia (mesmo expirado): nega o
        teste com motivo "ja_foi_cliente". Senão a pessoa cancela e pede
        teste de novo, viraria loophole.
      - Se já usou teste antes: nega com "ja_testou".
      - Caso contrário: insere em testes_gratis e cria/atualiza a linha de
        assinatura com plano="teste" e expiração em N dias.

    Retornos possíveis:
      {"ok": True, "expira": <datetime>, "dias": 3}
      {"ok": False, "motivo": "ja_testou"}                  # já usou teste antes (e ele expirou)
      {"ok": False, "motivo": "teste_ja_ativo", "dias_restantes": int}
      {"ok": False, "motivo": "ja_assinante", "dias_restantes": int}
      {"ok": False, "motivo": "ja_foi_cliente"}
    """
    variantes = _variantes_jid(usuario)
    pool = await get_pool()
    async with pool.acquire() as conn:

        # 1. Já tem assinatura paga ativa? (NÃO cria teste)
        ativa_paga = await conn.fetchrow(
            """
            SELECT * FROM assinaturas
             WHERE usuario = ANY($1::text[])
               AND plano IN ('mensal', 'anual')
               AND data_expiracao > NOW()
            """,
            variantes,
        )
        if ativa_paga:
            expira = ativa_paga["data_expiracao"]
            if expira.tzinfo is None:
                expira = expira.replace(tzinfo=timezone.utc)
            dias_restantes = (expira - datetime.now(timezone.utc)).days
            return {
                "ok": False,
                "motivo": "ja_assinante",
                "dias_restantes": dias_restantes,
            }

        # 1b. Já tem TESTE ativo agora? (mensagem amigável diferente)
        teste_ativo = await conn.fetchrow(
            """
            SELECT * FROM assinaturas
             WHERE usuario = ANY($1::text[])
               AND plano = 'teste'
               AND data_expiracao > NOW()
            """,
            variantes,
        )
        if teste_ativo:
            expira = teste_ativo["data_expiracao"]
            if expira.tzinfo is None:
                expira = expira.replace(tzinfo=timezone.utc)
            dias_restantes = (expira - datetime.now(timezone.utc)).days
            return {
                "ok": False,
                "motivo": "teste_ja_ativo",
                "dias_restantes": dias_restantes,
            }

        # 2. Já foi cliente pago algum dia? (também NÃO concede teste)
        ja_pagou = await conn.fetchrow(
            """
            SELECT 1 FROM assinaturas
             WHERE usuario = ANY($1::text[])
               AND plano IN ('mensal', 'anual')
            """,
            variantes,
        )
        if ja_pagou:
            return {"ok": False, "motivo": "ja_foi_cliente"}

        # 3. Já usou o teste antes (mas já expirou)?
        ja_testou = await conn.fetchrow(
            "SELECT 1 FROM testes_gratis WHERE usuario = ANY($1::text[])",
            variantes,
        )
        if ja_testou:
            return {"ok": False, "motivo": "ja_testou"}

        # 4. OK, libera o teste
        agora = datetime.now(timezone.utc)
        expira = agora + timedelta(days=dias)
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO testes_gratis (usuario) VALUES ($1) "
                "ON CONFLICT (usuario) DO NOTHING",
                usuario,
            )
            await conn.execute(
                """
                INSERT INTO assinaturas (usuario, token, plano, data_inicio, data_expiracao, status)
                VALUES ($1, NULL, 'teste', $2, $3, 'ativo')
                ON CONFLICT (usuario) DO UPDATE SET
                    token = NULL,
                    plano = 'teste',
                    data_inicio = $2,
                    data_expiracao = $3,
                    status = 'ativo'
                """,
                usuario, agora, expira,
            )
        return {"ok": True, "expira": expira, "dias": dias}
