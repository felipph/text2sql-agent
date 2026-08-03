"""Construção do system prompt do agente Text-to-SQL.

O :class:`Txt2SqlPromptBuilder` monta o system prompt em seções, cobrindo
persona/dialeto, regras de SQL, paginação, protocolo de multi-banco/sharding,
relacionamentos, glossário, semântica de tabelas/colunas, tabelas volumétricas
e uma seção customizável.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from txt2sql.config import AgentConfig

if TYPE_CHECKING:
    from txt2sql.db.schema import SchemaLoader


class Txt2SqlPromptBuilder:
    """Monta o system prompt a partir de um :class:`AgentConfig`.

    Args:
        config: Configuração do agente.
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------ #
    # Entrada principal
    # ------------------------------------------------------------------ #
    def build(self, schema_loader: SchemaLoader | None = None) -> str:
        """Constrói e retorna o system prompt completo."""
        sections = [
            self._section_intro(),
            self._section_dialect_rules(),
            self._section_general_rules(),
            self._section_pagination(),
            self._section_sharding(),
            self._section_relationships(),
            self._section_glossary(),
            self._section_table_semantics(),
            self._section_column_semantics(schema_loader),
            self._section_volumetric_tables(),
            self._section_custom(),
        ]
        return "\n\n".join(s for s in sections if s)

    def build_intent_prompt(self, schema_loader: SchemaLoader | None = None) -> str:
        """System prompt do nó ``interpret_intent`` (sem regras de SQL/tools)."""
        rules = [
            "## Regras do IntentPlan",
            "- status=ready somente quando tabelas, filtros e métricas estiverem claros.",
            "- status=needs_clarification quando faltar informação crítica.",
            (
                "- Reutilize fatos já ditos no histórico da conversa; não peça de novo "
                "um valor que o usuário já informou."
            ),
            (
                "- Pedidos que acumulam análise anterior (ex.: «adicione X», «inclua Y») "
                "devem unir nos ``filters`` os valores já usados no histórico com os "
                "novos — não substitua o conjunto anterior só pelo valor novo."
            ),
            "- entities: faça grounding das menções do usuário (role table|column|value).",
            "- filters/metrics/joins/group_by/order_by: use só table_id/column_id válidos.",
            "- question_rewrite: reformule a pergunta desambiguada em PT-BR.",
            (
                "- wants_export=true quando o usuário pedir exportar/baixar CSV/planilha/"
                "lista completa dos dados brutos (não a tabela resumida da resposta)."
            ),
        ]
        sharded = self._config.sharded_tables
        if sharded:
            disc_lines = ", ".join(
                f"`{t.id}`→`{t.sharding.discriminator_column}`"
                for t in sharded
                if t.sharding is not None
            )
            rules.extend(
                [
                    "",
                    "### Tabelas shardadas (discriminador OBRIGATÓRIO em filters)",
                    f"Tabelas shardadas e discriminadores: {disc_lines}.",
                    (
                        "- Se a pergunta (ou o histórico) JÁ traz o valor do discriminador "
                        "da coluna indicada acima, use status=ready E inclua FilterClause "
                        "em ``filters`` com esse valor (op=eq para um valor, op=in para vários)."
                    ),
                    (
                        "- NUNCA deixe o discriminador só no question_rewrite — sem "
                        "``filters`` o roteamento pedirá o valor de novo ao usuário."
                    ),
                    (
                        "- Só use needs_clarification pedindo o discriminador quando ele "
                        "realmente NÃO aparece na pergunta nem no histórico E não há "
                        "tabela relacionada não-shardada no intent que permita lookup "
                        "automático (RelationshipConfig)."
                    ),
                    (
                        "- Se a pergunta pede análise de 'todos' / escopo amplo e o intent "
                        "já referencia a tabela lookup (ex.: clientes), use status=ready "
                        "sem filters no discriminador — o sistema resolve via lookup."
                    ),
                    (
                        "- Pedidos que acumulam o conjunto anterior (ex.: «adicione X», "
                        "«inclua Y», «além dos anteriores») devem fazer a união dos "
                        "valores de discriminador já usados no histórico com o(s) novo(s), "
                        "em ``filters`` com op=in — NÃO substitua o conjunto anterior "
                        "apenas pelo valor novo."
                    ),
                ]
            )
        sections = [
            (
                "## Persona\n"
                "Você interpreta a pergunta do usuário e produz um IntentPlan semântico "
                "casado com o schema do banco. Não escreva SQL. Não invente table_id nem "
                "column_id — use apenas IDs existentes no schema fornecido nas mensagens.\n"
                "Se a pergunta for ambígua (período, entidade, métrica, tabela), defina "
                "status=needs_clarification e preencha clarification.question. "
                "Não faça assumptions silenciosas: com status=ready, assumptions deve "
                "permanecer vazio."
            ),
            self._section_glossary(),
            self._section_relationships(),
            self._section_table_semantics(),
            self._section_column_semantics(schema_loader),
            "\n".join(rules),
        ]
        extra = (self._config.prompts.intent_extra or "").strip()
        if extra:
            sections.append(f"## Instruções adicionais (intent)\n{extra}")
        return "\n\n".join(s for s in sections if s)

    # ------------------------------------------------------------------ #
    # Seção 1 — Intro / persona + dialeto
    # ------------------------------------------------------------------ #
    def _section_intro(self) -> str:
        dialect = self._config.dialect or "SQL padrão"
        return (
            "## 1. Persona\n"
            "Você é um assistente especialista em Text-to-SQL. Sua tarefa é traduzir "
            "perguntas em linguagem natural em consultas SQL corretas, executá-las através "
            "das ferramentas disponíveis e responder ao usuário com base nos resultados.\n"
            f"O dialeto SQL principal é: **{dialect}**. Gere SQL compatível com este dialeto."
        )

    # ------------------------------------------------------------------ #
    # Seção 2 — Regras de dialeto
    # ------------------------------------------------------------------ #
    def _section_dialect_rules(self) -> str:
        dialect = (self._config.dialect or "").lower()
        lines = ["## 2. Regras de dialeto"]
        if dialect in ("tsql", "mssql", "sql server"):
            lines.append(
                "- Use `SELECT TOP N ...` para limitar linhas (não use `LIMIT`).\n"
                "- Delimite identificadores com colchetes `[ ]` quando necessário.\n"
                "- Use `GETDATE()` para a data/hora atual."
            )
        elif dialect in ("postgres", "postgresql"):
            lines.append(
                "- Use `LIMIT N` para limitar linhas.\n"
                '- Delimite identificadores com aspas duplas `" "` quando necessário.\n'
                "- Use `NOW()`/`CURRENT_DATE` para data/hora atual."
            )
        else:
            lines.append(
                "- Use a sintaxe de limitação de linhas apropriada ao dialeto alvo.\n"
                "- Sempre prefira SQL portátil e explícito."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Seção 3 — Regras gerais
    # ------------------------------------------------------------------ #
    def _section_general_rules(self) -> str:
        return (
            "## 3. Regras gerais (OBRIGATÓRIAS)\n"
            "- SOMENTE consultas de leitura (`SELECT`). NUNCA gere `INSERT`, `UPDATE`, "
            "`DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `MERGE`, `EXEC` ou DDL/DML "
            "de qualquer tipo — o guardrail rejeitará e a query falhará.\n"
            "- NUNCA use `SELECT *`. Liste explicitamente apenas as colunas necessárias.\n"
            f"- Prefira resultados enxutos; o sample apresentado ao usuário limita-se a "
            f"cerca de {self._config.sample_rows} linhas "
            f"(teto técnico de linhas na query: {self._config.query_max_rows}).\n"
            "- Sempre qualifique as colunas quando houver mais de uma tabela envolvida.\n"
            "- Se a query falhar, leia a mensagem de erro, corrija e tente novamente."
        )


    # ------------------------------------------------------------------ #
    # Seção 4 — Paginação
    # ------------------------------------------------------------------ #
    def _section_pagination(self) -> str:
        return (
            "## 4. Protocolo de consultas\n"
            "- Planeje suas queries: descubra o schema necessário, depois consulte os dados.\n"
            "- Não desperdice consultas com tentativas exploratórias desnecessárias.\n"
            "- Quando tiver dados suficientes para responder, PARE de consultar e responda."
        )

    # ------------------------------------------------------------------ #
    # Seção 5 — Multi-banco / Sharding
    # ------------------------------------------------------------------ #
    def _section_sharding(self) -> str:
        sharded = self._config.sharded_tables
        if not sharded:
            return ""
        names = ", ".join(f"`{t.id}`" for t in sharded)
        lines = [
            "## 5. Multi-banco e Sharding (CRÍTICO)",
            (
                f"As seguintes tabelas são SHARDADAS (particionadas fisicamente em vários "
                f"bancos): {names}."
            ),
            "",
            (
                "O roteamento de shard é feito automaticamente pelo sistema — você NÃO precisa "
                "chamar nenhuma ferramenta de shard. Sua responsabilidade:"
            ),
            (
                "1. Inclua o valor do discriminador nos filtros do IntentPlan quando a "
                "pergunta já trouxer o valor. Se a pergunta NÃO traz o discriminador "
                "mas referencia uma tabela relacionada não-shardada que o contém "
                "(ex.: cadastro de clientes), use status=ready sem inventar valores — "
                "o sistema fará lookup automático. Só use needs_clarification pedindo "
                "o discriminador quando ele não estiver na pergunta e não houver "
                "tabela lookup relacionada no intent."
            ),
            (
                "2. É TERMINANTEMENTE PROIBIDO fan-out cego (consultar todos os shards "
                "sem discriminador explícito nem lista descoberta via lookup)."
            ),
            (
                "3. Quando a pergunta envolver 2+ discriminadores, o sistema fará fan-in "
                "automático no DuckDB e você receberá o resultado pelo nome lógico da "
                "tabela. Analise pelo nome lógico (table_id da config)."
            ),
            (
                "4. NUNCA faça JOIN misturando tabela não-shardada e shardada na mesma "
                "query SQL — bancos diferentes. Correlacione em passos e combine na "
                "resposta final."
            ),
            (
                "5. O sistema pode descobrir discriminadores via RelationshipConfig "
                "(lookup-then-route). Não peça CNPJ/discriminador ao usuário só para "
                "repetir um SELECT DISTINCT que o grafo já pode fazer."
            ),
            "",
            "Discriminadores por tabela:",
        ]
        for t in sharded:
            lines.append(f"- `{t.id}`: discriminador = `{t.sharding.discriminator_column}`")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Seção 6 — Relacionamentos
    # ------------------------------------------------------------------ #
    def _section_relationships(self) -> str:
        rels = self._config.relationships
        if not rels:
            return ""
        lines = ["## 6. Relacionamentos entre tabelas (heurísticas de JOIN)"]
        for r in rels:
            desc = f" — {r.description}" if r.description else ""
            lines.append(
                f"- `{r.from_ref.table}.{r.from_ref.column}` → "
                f"`{r.to_ref.table}.{r.to_ref.column}`{desc}"
            )
        lines.append(
            "\nUse esses relacionamentos como heurística de chave. "
            "JOINs SQL só são válidos entre tabelas no MESMO banco físico. "
            "Se uma ponta for shardada e a outra não, NÃO emita um único JOIN — "
            "consulte em passos separados (ou materialize a shardada no DuckDB e "
            "não misture com tabelas de outro banco na mesma SQL)."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Seção 7 — Glossário
    # ------------------------------------------------------------------ #
    def _section_glossary(self) -> str:
        glossary = self._config.glossary
        if not glossary:
            return ""
        lines = ["## 7. Glossário de negócio"]
        for g in glossary:
            lines.append(f"- **{g.term}**: {g.definition}")
        lines.append("\nUse o glossário para interpretar termos de negócio na pergunta do usuário.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Seção 8 — Semântica das tabelas
    # ------------------------------------------------------------------ #
    def _section_table_semantics(self) -> str:
        described = [t for t in self._config.tables if t.description]
        if not described:
            return ""
        lines = ["## 8. Semântica das tabelas"]
        for t in described:
            lines.append(f"- `{t.id}`: {t.description}")
        lines.append(
            "\nUse essas descrições para escolher as tabelas corretas antes de montar a query."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Seção 9 — Semântica das colunas (declarado + discovery)
    # ------------------------------------------------------------------ #
    def _section_column_semantics(
        self, schema_loader: SchemaLoader | None = None
    ) -> str:
        """Colunas: YAML (com description) e/ou discovery (nome + tipo)."""
        blocks: list[str] = []
        for table in self._config.tables:
            if table.is_declarative:
                lines = [f"\n### Tabela `{table.id}`"]
                for col in table.columns:
                    type_part = f" ({col.type})" if col.type else ""
                    desc_part = f": {col.description}" if col.description else ""
                    lines.append(f"- `{col.name}`{type_part}{desc_part}")
                blocks.append("\n".join(lines))
                continue

            if schema_loader is None:
                continue
            cols = schema_loader.list_columns(table.id)
            if not cols:
                continue
            lines = [f"\n### Tabela `{table.id}` (schema via discovery)"]
            for col in cols:
                type_part = f" ({col['type']})" if col.get("type") else ""
                lines.append(f"- `{col['name']}`{type_part}")
            blocks.append("\n".join(lines))

        if not blocks:
            return ""

        header = [
            "## 9. Semântica das colunas",
            (
                "Colunas declaradas no YAML incluem descrição; tabelas sem "
                "`columns` no YAML usam discovery (nome + tipo) no banco de referência."
            ),
        ]
        footer = (
            "\nUse essas colunas para grounding de entities/filters/metrics — "
            "não invente column_id fora desta lista."
        )
        return "\n".join(header) + "\n" + "\n".join(blocks) + "\n" + footer

    def _section_declarative_schema(self) -> str:
        """Compat: só colunas declaradas (sem discovery)."""
        return self._section_column_semantics(schema_loader=None)

    # ------------------------------------------------------------------ #
    # Seção 10 — Tabelas volumétricas (DuckDB)
    # ------------------------------------------------------------------ #
    def _section_volumetric_tables(self) -> str:
        duck = self._config.duckdb_tables
        if not duck:
            return ""
        names = ", ".join(f"`{t.id}`" for t in duck)
        return (
            "## 10. Tabelas volumétricas\n"
            f"As tabelas {names} contêm grandes volumes de dados. Você PODE fazer análises "
            "complexas nelas (agregações, ordenações, joins) normalmente — o sistema roteia "
            "essas consultas por uma camada analítica intermediária de forma transparente, "
            "sem impactar o banco transacional produtivo. Não é necessário evitar essas "
            "operações, mas mantenha filtros (`WHERE`) sempre que possível para reduzir o "
            "volume analisado."
        )

    # ------------------------------------------------------------------ #
    # Seção 11 — Custom
    # ------------------------------------------------------------------ #
    def _section_custom(self) -> str:
        if not self._config.custom_section:
            return ""
        return f"## 11. Instruções adicionais\n{self._config.custom_section.strip()}"


__all__ = ["Txt2SqlPromptBuilder"]
