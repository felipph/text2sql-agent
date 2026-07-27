# Design: gerador paramétrico de seed + prompts

**Data:** 2026-07-27  
**Escopo:** `playground/seed_data.py`, `seed_params.yaml`, regeneração de
`prompts.yaml` / `seed/*.sql`  
**Status:** aprovado

## Objetivo

Gerar clientes/recebíveis a partir de parâmetros (`cnpjs`, `por_cnpj`) e
popular o banco; regenerar `prompts.yaml` com expected derivados dos dados.

## Decisões

| Tema | Escolha |
|------|---------|
| Organização | Um módulo (`seed_data.py`) gera dataset → SQL/apply → prompts |
| Params | `seed_params.yaml` + flags CLI que sobrescrevem |
| RNG | Determinístico com `seed` (default); `--random` ignora seed |
| Shards | Prefixo CNPJ aleatório 000–999 (sem forçar balanceamento) |
| Prompts | Sempre regenerados na geração |

## CLI

```bash
python playground/seed_data.py --apply
python playground/seed_data.py --cnpjs 20 --por-cnpj 10 --apply --dump-sql
python playground/seed_data.py --random --apply
```

## Dataset

- CNPJ 14 dígitos; razão `Cliente_{i:03d}`
- Recebíveis: valor, status ∈ {pago, pendente, vencido}, datas
- Garantir ≥1 recebível `vencido`

## Prompts gerados

- `single_sum`, `multi_sum` (se ≥2 CNPJs), `join_vencido`, `guardrail_delete`

## Não-objetivos

- UI Streamlit para gerar; DV de CNPJ; balanceamento forçado de shards.
