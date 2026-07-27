"""Construção do system prompt do agente Text-to-SQL.

O :class:`Txt2SqlPromptBuilder` monta o system prompt em seções, cobrindo
persona/dialeto, regras de SQL, paginação, protocolo de multi-banco/sharding,
relacionamentos, glossário, semântica de tabelas/colunas, tabelas volumétricas
e uma seção customizável.
"""

from __future__ import annotations

from txt2sql.config import AgentConfig


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
    def build(self) -> str:
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
            self._section_declarative_schema(),
            self._section_volumetric_tables(),
            self._section_custom(),
        ]
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
            f"- Limite os resultados a no máximo {self._config.top_k} linhas, a menos que "
            "o usuário peça explicitamente mais.\n"
            "- Sempre qualifique as colunas quando houver mais de uma tabela envolvida.\n"
            "- Se a query falhar, leia a mensagem de erro, corrija e tente novamente."
        )

    # ------------------------------------------------------------------ #
    # Seção 4 — Paginação
    # ------------------------------------------------------------------ #
    def _section_pagination(self) -> str:
        return (
            "## 4. Protocolo de paginação\n"
            f"- Você pode executar no máximo **{self._config.max_pages} consultas** por turno.\n"
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
            "Protocolo OBRIGATÓRIO para tabelas shardadas:",
            (
                "1. Antes de consultar uma tabela shardada, você DEVE chamar a ferramenta "
                "`resolve_shard(table_id, discriminator_value)` passando o valor do "
                "discriminador extraído da pergunta do usuário."
            ),
            (
                "2. A ferramenta retorna `{database_id, table_name}` — use o `table_name` "
                "retornado (nome físico real) na sua query."
            ),
            "3. NUNCA assuma ou invente o shard/nome físico da tabela.",
            (
                "4. É TERMINANTEMENTE PROIBIDO fazer fan-out cego (consultar todos os "
                "shards sem lista de discriminadores). Se o usuário NÃO forneceu nem "
                "permitiu descobrir o valor do discriminador, você DEVE PARAR e PEDIR "
                "antes de continuar."
            ),
            (
                "5. Se a pergunta envolve 2 ou mais valores do discriminador (explícitos "
                "na pergunta ou descobertos via query em tabela NÃO shardada):"
            ),
            "   a. Obtenha a lista completa de valores.",
            (
                "   b. Chame `materialize_sharded_table(table_id, discriminator_values)` "
                "UMA vez (nunca com 0 ou 1 valor)."
            ),
            (
                "   c. Em seguida consulte com `sql_db_query` usando o NOME LÓGICO da "
                "tabela (ex.: `recebiveis`), NÃO os nomes físicos."
            ),
            (
                "   d. Se o retorno indicar `truncated=true`, avise o usuário na resposta "
                "final (análise parcial pelo limite configurado)."
            ),
            (
                "6. Com exatamente 1 discriminador, use o protocolo single "
                "(`resolve_shard` + query no nome físico) — não chame "
                "`materialize_sharded_table`."
            ),
            (
                "7. NUNCA faça JOIN (nem FROM com várias tabelas) misturando tabela "
                "não-shardada e tabela shardada na mesma query SQL — bancos "
                "diferentes. Correlacione em passos e combine na resposta final."
            ),
            "",
            (
                'Receita quando a pergunta NÃO traz o discriminador '
                '(ex.: "clientes com recebível vencido"):'
            ),
            (
                "A. `sql_db_query` na tabela NÃO shardada para listar os discriminadores "
                "(ex.: `SELECT cnpj FROM clientes`)."
            ),
            (
                "B. Com a lista: se 2+ valores → `materialize_sharded_table`; se 1 → "
                "`resolve_shard`."
            ),
            (
                "C. Só então `sql_db_query` na shardada (nome lógico após materialize, "
                "ou nome físico após resolve) filtrando o critério (ex.: status)."
            ),
            (
                "D. Se precisar da razão social, nova query só em `clientes` com os "
                "CNPJs encontrados — NUNCA JOIN cross-database."
            ),
            (
                "E. Se o roteador/guardrail rejeitar uma query, NÃO desista com dado "
                "parcial irrelevante: corrija seguindo A–D e responda com a evidência "
                "completa."
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
    # Seção 9 — Schema declarativo (descrições de colunas)
    # ------------------------------------------------------------------ #
    def _section_declarative_schema(self) -> str:
        declared = [t for t in self._config.tables if t.is_declarative]
        if not declared:
            return ""
        lines = ["## 9. Semântica das colunas (schema declarado)"]
        for t in declared:
            lines.append(f"\n### Tabela `{t.id}`")
            for col in t.columns:
                type_part = f" ({col.type})" if col.type else ""
                desc_part = f": {col.description}" if col.description else ""
                lines.append(f"- `{col.name}`{type_part}{desc_part}")
        lines.append(
            "\nUse essas descrições para escolher as colunas corretas e interpretar "
            "corretamente valores e filtros."
        )
        return "\n".join(lines)

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
