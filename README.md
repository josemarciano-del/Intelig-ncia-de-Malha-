# Inteligência de Malha — Cordenonsi

Mapeia as **rotas tronco** da operação (trechos de estrada compartilhados por várias origens/destinos) a partir
do GPS realizado (Autotrac) e da rota planejada, e propõe a padronização **ponta de origem → troncos → ponta de destino**
para substituir o cadastro de rotas CNPJ → CNPJ.

## Como rodar

```bash
pip install h3 openpyxl
echo "CRD_TOKEN=<token da API>" > .env          # nunca versionar

python -m malha.coleta cargas 2022-01 2026-10   # cadastro de viagens (retomável)
python -m malha.coleta gps    2026-04 2026-09   # GPS histórico por placa x mês
python -m malha.coleta rotas  2026-04 2026-09   # traçado planejado de cada rota usada
python -m malha.processa      2026-04 2026-09   # troncos, nós, pontas, rotas padrão
python -m malha.dashboard                       # gera data/saida/dashboard_malha.html
```

Saídas em `data/saida/` (fora do git, contém dados confidenciais):
- `dashboard_malha.html` — dashboard único (abre no navegador; mapa usa OpenStreetMap)
- `malha.xlsx` — mesmas tabelas em Excel (Rotas padrão, Troncos, Nós, Pontas, Placas sem GPS)

## Como funciona (resumo)

1. Cada viagem vira uma trilha em hexágonos H3 (~36 km²): GPS realizado quando liga origem ao destino;
   senão, o traçado da rota planejada.
2. Hexágono é **tronco** quando passam ≥ 5 pares cidade→cidade distintos e ≥ 5 viagens/mês.
3. O grafo dos hexágonos-tronco é limpo (árvore geradora máxima + anéis reais) e quebrado em **trechos** entre
   **nós** (bifurcações e pontos onde ≥ 4 viagens/mês entram ou saem).
4. Cada viagem é decomposta em ponta de origem (cidade → nó de entrada), troncos e ponta de destino.
   A **rota padrão** de cada par cidade→cidade é a via que melhor representa as viagens do par.
5. Nomes vêm da base de municípios do IBGE (`malha/ref`).

Parâmetros no topo de `malha/processa.py`. Detalhes das APIs: [docs/apis.md](docs/apis.md).
